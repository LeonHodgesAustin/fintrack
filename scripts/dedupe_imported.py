"""
scripts/dedupe_imported.py
--------------------------
Remove bofa_* import transactions that fall on or after the date Plaid started
covering the same account.

Strategy: find the earliest transaction date among all non-bofa_ (Plaid-synced)
transactions for each institution, then delete any bofa_ transactions on or
after that cutoff. Plaid is the authoritative source for that window; the CSV
import is authoritative for everything older.

This is simpler and more reliable than fuzzy merchant matching.

Run:
    python scripts/dedupe_imported.py --dry-run     # preview only
    python scripts/dedupe_imported.py               # actually delete
    python scripts/dedupe_imported.py --db-path fintrack.db
"""

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)


def find_plaid_cutoff(conn) -> str | None:
    """
    Return the earliest date of any Plaid-synced transaction (non-bofa_).
    Returns None if there are no Plaid transactions yet.
    """
    row = conn.execute(
        """
        SELECT MIN(date) AS earliest
        FROM transactions
        WHERE transaction_id NOT LIKE 'bofa_%'
        """
    ).fetchone()
    return row["earliest"] if row else None


def find_overlap(conn, cutoff: str) -> list[str]:
    """
    Return transaction_ids of bofa_ records on or after the cutoff date.
    These are in the window Plaid covers and should be removed.
    """
    rows = conn.execute(
        """
        SELECT transaction_id
        FROM transactions
        WHERE transaction_id LIKE 'bofa_%'
          AND date >= ?
        ORDER BY date
        """,
        (cutoff,),
    ).fetchall()
    return [r["transaction_id"] for r in rows]


def main():
    parser = argparse.ArgumentParser(
        description="Remove bofa_ import transactions that overlap with Plaid coverage."
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without deleting")
    parser.add_argument("--db-path", default="fintrack.db")
    args = parser.parse_args()

    from fintrack.config import get_settings
    from fintrack.db import configure_encryption, get_connection, migrate

    s = get_settings()
    configure_encryption(s.fernet_key)
    migrate(args.db_path)
    conn = get_connection(args.db_path)

    cutoff = find_plaid_cutoff(conn)
    if not cutoff:
        print("No Plaid-synced transactions found. Run `fintrack sync` first.")
        conn.close()
        return

    print(f"Plaid coverage starts: {cutoff}")

    overlap_ids = find_overlap(conn, cutoff)
    if not overlap_ids:
        print("No bofa_ transactions fall within Plaid's coverage window. Nothing to do.")
        conn.close()
        return

    # Summary counts
    count_row = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE transaction_id LIKE 'bofa_%'"
    ).fetchone()
    total_bofa = count_row["n"]

    kept = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE transaction_id LIKE 'bofa_%' AND date < ?",
        (cutoff,),
    ).fetchone()["n"]

    print(f"Total bofa_ transactions:  {total_bofa}")
    print(f"  Older than {cutoff} (kept):    {kept}")
    print(f"  On/after  {cutoff} (overlap):  {len(overlap_ids)}")

    # Show a sample of what would be removed
    sample = conn.execute(
        f"""
        SELECT date, amount, COALESCE(merchant_name, raw_name, '') AS name
        FROM transactions
        WHERE transaction_id IN ({','.join('?' for _ in overlap_ids[:20])})
        ORDER BY date
        LIMIT 15
        """,
        overlap_ids[:20],
    ).fetchall()

    print(f"\nSample of {min(15, len(overlap_ids))} overlap transactions to remove:\n")
    print(f"  {'Date':<12} {'Amount':>10}  Description")
    print(f"  {'-'*12} {'-'*10}  {'-'*40}")
    for r in sample:
        print(f"  {r['date']:<12} ${r['amount']:>9.2f}  {r['name'][:40]}")

    if args.dry_run:
        print(f"\nDry run: {len(overlap_ids)} bofa_ transactions would be removed.")
        print(f"         {kept} pre-Plaid bofa_ transactions would be kept.")
        conn.close()
        return

    print(f"\nRemoving {len(overlap_ids)} overlap transactions...")
    conn.execute(
        f"""
        DELETE FROM transactions
        WHERE transaction_id IN ({','.join('?' for _ in overlap_ids)})
        """,
        overlap_ids,
    )
    conn.commit()
    conn.close()
    print(f"Done. Removed {len(overlap_ids)} transactions.")
    print(f"     {kept} pre-Plaid bofa_ transactions retained.")
    print("\nRun `fintrack report` to verify results look correct.")


if __name__ == "__main__":
    main()
