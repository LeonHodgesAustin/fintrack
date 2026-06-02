"""
scripts/import_bofa_csv.py
--------------------------
Import historical Bank of America transaction exports into fintrack.

Supports two formats (auto-detected):

  TEXT (fixed-width):
    BofA's "Account Activity" text export.
    Columns: Date  Description  Amount  Running Bal.
    Sign:    negative = debit (money OUT), positive = credit (money IN)
    Strategy: scan each line for the last two X.XX-format numbers;
              second-to-last = amount, last = running balance.
    Negate amount to match Plaid sign convention (positive = out).

  CSV:
    Columns: Date, Description, Amount, Running Bal.  (checking/savings)
         OR: Posted Date, Reference Number, Payee, Address, Amount  (credit card)
    Sign on checking: same as text -- negate for Plaid.
    Sign on credit:   positive = charge (out) -- no negation needed.

Deduplication:
  Uses a deterministic ID = sha1(date|description[:60]|amount).
  Also does a fuzzy check against existing Plaid transactions on the same
  date/amount with overlapping description words, to avoid double-counting
  if Plaid already imported the same transactions.

Usage:
  python scripts/import_bofa_csv.py transactions.txt
  python scripts/import_bofa_csv.py transactions.txt --account-name "BofA Checking"
  python scripts/import_bofa_csv.py transactions.txt --dry-run
  python scripts/import_bofa_csv.py cc.csv --type credit --account-name "BofA Visa"

All arguments:
  FILE              Path to BofA text or CSV export
  --account-name    Label stored in DB (default: "BofA Import")
  --type            auto | checking | credit  (default: auto)
  --dry-run         Parse and classify, print results, do NOT write to DB
  --db-path         Override DB path (default: fintrack.db from .env)
  --no-classify     Store all transactions as UNCATEGORIZED
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

# ---- Parsing helpers ---------------------------------------------------------

MONEY_RE  = re.compile(r'(-?[\d,]+\.\d{2})')
DATE_RE   = re.compile(r'^(\d{2}/\d{2}/\d{4})\s{2,}')
DATE_FMTS = ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y")


def _parse_date(raw: str) -> str:
    raw = raw.strip().strip('"')
    for fmt in DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {raw!r}")


def _parse_amount(raw: str) -> float:
    raw = raw.strip().replace("$", "").replace(",", "").replace('"', "")
    if raw.startswith("(") and raw.endswith(")"):
        return -float(raw[1:-1])
    return float(raw)


def _make_txn_id(date_str: str, description: str, amount: float) -> str:
    key = f"bofa_import|{date_str}|{description[:60]}|{amount:.2f}"
    return "bofa_" + hashlib.sha1(key.encode()).hexdigest()[:24]


# ---- Text format parser (fixed-width) ----------------------------------------

def read_txt(path: Path) -> list[dict]:
    """
    Parse BofA fixed-width text export.
    Finds each line that starts with a date, then extracts the
    transaction amount as the second-to-last X.XX number on the line.
    """
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    # Find the "Date  Description  Amount  Running Bal." header line
    header_idx = None
    for i, line in enumerate(lines):
        if re.match(r'\s*Date\s+Description', line, re.IGNORECASE):
            header_idx = i
            break

    if header_idx is None:
        raise ValueError(
            "Could not find 'Date  Description  Amount' header line.\n"
            "Make sure this is a BofA Account Activity text export."
        )

    results = []
    skip_descriptions = {"beginning balance", "ending balance", "total credits", "total debits"}

    for line in lines[header_idx + 1:]:
        line = line.rstrip()
        if not line:
            continue

        # Must start with MM/DD/YYYY
        date_match = DATE_RE.match(line)
        if not date_match:
            continue

        date_str = date_match.group(1)
        rest = line[date_match.end():]

        # Find all money-format numbers
        money_matches = list(MONEY_RE.finditer(rest))

        if len(money_matches) < 2:
            # Only running balance (balance summary lines) -- skip
            continue

        # Second-to-last = transaction amount, last = running balance
        amount_match  = money_matches[-2]
        description   = rest[:amount_match.start()].strip()

        # Skip balance marker lines
        if any(s in description.lower() for s in skip_descriptions):
            continue

        # BofA txt sign: negative=debit(out), positive=credit(in)
        # Plaid sign:    positive=debit(out), negative=credit(in)  -> negate
        bofa_amount   = float(amount_match.group(1).replace(",", ""))
        plaid_amount  = -bofa_amount

        try:
            iso_date = _parse_date(date_str)
        except ValueError as e:
            print(f"  [SKIP] {e}", file=sys.stderr)
            continue

        results.append({
            "date":         iso_date,
            "description":  description,
            "amount":       plaid_amount,
            "account_type": "checking",
        })

    return results


# ---- CSV format parser -------------------------------------------------------

def _detect_csv_format(header: list[str]) -> str:
    h = [c.lower().strip().strip('"') for c in header]
    if "posted date" in h or "reference number" in h or "payee" in h:
        return "credit"
    return "checking"


def read_csv(path: Path, account_type: str = "auto") -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        lines = f.readlines()

    # Find header row (first row with >= 3 commas)
    header_idx = 0
    for i, line in enumerate(lines):
        if line.count(",") >= 2:
            header_idx = i
            break

    content = "".join(lines[header_idx:])
    reader  = csv.DictReader(content.splitlines())
    header  = reader.fieldnames or []
    if account_type == "auto":
        account_type = _detect_csv_format(header)

    results = []
    for row in reader:
        if not any(v.strip() for v in row.values()):
            continue
        try:
            if account_type == "credit":
                date_str    = _parse_date(row.get("Posted Date", row.get("Transaction Date", "")))
                description = (row.get("Payee") or row.get("Description") or "").strip()
                amount      = _parse_amount(row.get("Amount", "0"))
            else:
                date_str    = _parse_date(row.get("Date", ""))
                description = (row.get("Description") or "").strip()
                amount      = -_parse_amount(row.get("Amount", "0"))  # negate for Plaid

            if not date_str or not description:
                continue

            results.append({
                "date":         date_str,
                "description":  description,
                "amount":       amount,
                "account_type": account_type,
            })
        except (ValueError, KeyError) as e:
            print(f"  [SKIP] {e}", file=sys.stderr)

    return results


# ---- Auto-detect format ------------------------------------------------------

def read_file(path: Path, account_type: str = "auto") -> list[dict]:
    """Try text format first; fall back to CSV."""
    if path.suffix.lower() in (".txt", ".text"):
        return read_txt(path)
    # Try CSV
    try:
        return read_csv(path, account_type)
    except Exception:
        # Last resort: try text
        return read_txt(path)


# ---- Deduplication ----------------------------------------------------------

# Words that appear constantly in BofA raw descriptions but carry no identity
_BOFA_NOISE = {
    "", "purchase", "the", "of", "and", "or", "inc", "llc", "corp",
    "ca", "wa", "md", "ny", "tx", "fl", "va", "pa", "co", "ga", "oh",
    "com", "www", "http", "https",
}


def _fuzzy_duplicate_exists(conn, date_str: str, amount: float, description: str) -> bool:
    """
    Return True if a Plaid-synced transaction with the same amount already
    exists within 2 days of date_str and appears to be the same transaction.

    Two-day window: Plaid and BofA can post the same transaction on different
    calendar days (e.g., weekend processing, timezone differences).

    Matching logic (any one is sufficient):
      1. merchant_name is a non-trivial substring of the BofA description.
      2. raw_name is a non-trivial substring of the BofA description.
      3. At least 1 meaningful word overlaps between the BofA description and
         the Plaid merchant_name/raw_name (after stripping noise words).
    """
    from datetime import date as _date, timedelta
    d      = _date.fromisoformat(date_str)
    start  = (d - timedelta(days=2)).isoformat()
    end    = (d + timedelta(days=2)).isoformat()

    rows = conn.execute(
        """
        SELECT merchant_name, raw_name
        FROM transactions
        WHERE date BETWEEN ? AND ?
          AND ABS(amount - ?) < 0.02
          AND transaction_id NOT LIKE 'bofa_%'
        """,
        (start, end, amount),
    ).fetchall()

    if not rows:
        return False

    desc_lower = description.lower()
    # Meaningful words from the BofA description (strip noise and short tokens)
    desc_words = {
        w for w in re.split(r"\W+", desc_lower)
        if w not in _BOFA_NOISE and len(w) > 2
    }

    for r in rows:
        merchant = (r["merchant_name"] or "").strip()
        raw      = (r["raw_name"]      or "").strip()

        # 1. Substring match: Plaid's clean name inside the messy BofA string
        if merchant and len(merchant) > 3 and merchant.lower() in desc_lower:
            return True
        if raw and len(raw) > 3 and raw.lower() in desc_lower:
            return True

        # 2. Word-overlap fallback (1 meaningful word is enough given amount+date already match)
        existing_words = {
            w for w in re.split(r"\W+", (merchant + " " + raw).lower())
            if w not in _BOFA_NOISE and len(w) > 2
        }
        if desc_words & existing_words:
            return True

    return False


# ---- Main import function ----------------------------------------------------

def import_transactions(
    file_path: Path,
    account_name: str = "BofA Import",
    account_type: str = "auto",
    dry_run: bool = False,
    db_path: str = "fintrack.db",
    classify: bool = True,
) -> dict:
    from fintrack.config import get_settings
    from fintrack.db import configure_encryption, get_connection, migrate, upsert_account, upsert_transaction
    from fintrack.classification import build_chain

    s = get_settings()
    configure_encryption(s.fernet_key)
    migrate(db_path)
    conn  = get_connection(db_path)
    chain = build_chain(s.get_classifier_chain()) if classify else None

    rows = read_file(file_path, account_type)
    if not rows:
        print("No transactions parsed -- check the file format.")
        return {"imported": 0, "skipped_duplicate": 0, "skipped_fuzzy": 0}

    detected_type = rows[0]["account_type"]
    print(f"Parsed {len(rows)} transactions from {file_path.name} (type: {detected_type})")
    print(f"Date range: {rows[-1]['date']} to {rows[0]['date']}")

    IMPORT_ITEM_ID    = "bofa_csv_import"
    IMPORT_ACCOUNT_ID = f"bofa_csv_{account_name.lower().replace(' ', '_').replace('/', '_')}"

    if not dry_run:
        conn.execute(
            """
            INSERT OR IGNORE INTO items
                (item_id, access_token, institution_name, cursor, last_synced)
            VALUES (?, ?, ?, NULL, ?)
            """,
            (IMPORT_ITEM_ID, "csv_import_no_token",
             "Bank of America (CSV Import)", date.today().isoformat()),
        )
        upsert_account(
            conn,
            account_id=IMPORT_ACCOUNT_ID,
            item_id=IMPORT_ITEM_ID,
            name=account_name,
            account_type="depository" if detected_type in ("checking", "savings") else "credit",
            subtype=detected_type,
        )
        conn.commit()

    stats = {"imported": 0, "skipped_duplicate": 0, "skipped_fuzzy": 0, "dry_run": 0}

    for row in rows:
        txn_id = _make_txn_id(row["date"], row["description"], row["amount"])

        if not dry_run:
            if conn.execute("SELECT 1 FROM transactions WHERE transaction_id = ?", (txn_id,)).fetchone():
                stats["skipped_duplicate"] += 1
                continue
            if _fuzzy_duplicate_exists(conn, row["date"], row["amount"], row["description"]):
                stats["skipped_fuzzy"] += 1
                continue

        txn_dict = {
            "transaction_id": txn_id,
            "account_id":     IMPORT_ACCOUNT_ID,
            "date":           row["date"],
            "amount":         row["amount"],
            "merchant_name":  None,
            "name":           row["description"],
            "pending":        False,
            "personal_finance_category": None,
        }

        if chain:
            result = chain.classify(txn_dict)
        else:
            from fintrack.classification.base import ClassificationResult
            result = ClassificationResult("UNCATEGORIZED", "", 0.0, "fallback")

        if dry_run:
            sign = "+" if row["amount"] < 0 else "-"
            print(f"  {row['date']}  {sign}${abs(row['amount']):>8.2f}  "
                  f"{row['description'][:50]:<50}  -> {result.category}")
            stats["dry_run"] += 1
            continue

        upsert_transaction(
            conn=conn,
            transaction_id=txn_id,
            account_id=IMPORT_ACCOUNT_ID,
            date=row["date"],
            amount=row["amount"],
            merchant_name=None,
            raw_name=row["description"],
            category_primary=result.category,
            category_detailed=result.subcategory,
            category_confidence=result.confidence,
            category_source=result.source,
            pending=False,
            raw_json=json.dumps(row),
        )
        stats["imported"] += 1

        if stats["imported"] % 200 == 0:
            conn.commit()
            print(f"  ... {stats['imported']} imported")

    if not dry_run:
        conn.commit()
        conn.close()

    return stats


# ---- CLI --------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Import BofA transactions into fintrack.")
    parser.add_argument("file", help="Path to BofA text or CSV export")
    parser.add_argument("--account-name", default="BofA Import")
    parser.add_argument("--type", dest="account_type", default="auto",
                        choices=["auto", "checking", "savings", "credit"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db-path", default="fintrack.db")
    parser.add_argument("--no-classify", action="store_true")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"Error: not found: {path}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("DRY RUN -- nothing will be written.\n")

    stats = import_transactions(
        file_path=path,
        account_name=args.account_name,
        account_type=args.account_type,
        dry_run=args.dry_run,
        db_path=args.db_path,
        classify=not args.no_classify,
    )

    print()
    if args.dry_run:
        print(f"Dry run: {stats['dry_run']} rows would be imported.")
    else:
        print("Import complete:")
        print(f"  Imported:        {stats['imported']}")
        print(f"  Skipped (exact): {stats['skipped_duplicate']}")
        print(f"  Skipped (fuzzy): {stats['skipped_fuzzy']}")
        print()
        print("Next: run `fintrack report` or `fintrack review` to check the results.")


if __name__ == "__main__":
    main()
