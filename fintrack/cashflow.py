"""
Net cashflow computation for fintrack.

Strategy (combination approach):

1. Category-based exclusion: transactions in transfer categories are excluded
   from income/expense totals by default and shown separately. The set of
   excluded categories is configurable (CASHFLOW_TRANSFER_CATEGORIES in .env).

2. Internal-transfer auto-detection: finds debit/credit pairs across accounts
   within a short date window whose amounts cancel (within a small tolerance).
   These are flagged as internal transfers even if Plaid mis-categorized them.
   Useful for catching BofA->Stash transfers that land in the wrong category.

Sign convention (Plaid):
  positive amount = money OUT (debit / expense)
  negative amount = money IN  (credit / income)
"""

import sqlite3
from datetime import date, timedelta


_DEFAULT_TRANSFER_CATS = frozenset(["TRANSFER_IN", "TRANSFER_OUT"])


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start.isoformat(), end.isoformat()


def detect_internal_transfers(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    window_days: int = 2,
    amount_tolerance: float = 0.02,
) -> list[dict]:
    """
    Find debit/credit pairs across different accounts within window_days days
    whose absolute amounts differ by less than amount_tolerance (fraction).

    These are almost certainly internal account transfers and should be
    excluded from cashflow to avoid double-counting.

    Returns list of dicts with keys: debit_id, credit_id, amount, date.
    """
    start, end = _month_bounds(year, month)

    # Fetch all non-pending, non-trivial transactions for the month
    rows = conn.execute(
        """
        SELECT t.transaction_id, t.date, t.amount, t.account_id
        FROM transactions t
        WHERE t.date >= ? AND t.date < ?
          AND t.pending = 0
          AND ABS(t.amount) > 1.0
        ORDER BY t.date, ABS(t.amount)
        """,
        (start, end),
    ).fetchall()

    txns = [dict(r) for r in rows]
    debits  = [t for t in txns if t["amount"] > 0]
    credits = [t for t in txns if t["amount"] < 0]

    pairs = []
    used_credit_ids: set[str] = set()

    for d in debits:
        for c in credits:
            if c["transaction_id"] in used_credit_ids:
                continue
            if d["account_id"] == c["account_id"]:
                continue  # same account, not an internal transfer

            date_d = date.fromisoformat(d["date"])
            date_c = date.fromisoformat(c["date"])
            if abs((date_d - date_c).days) > window_days:
                continue

            # Amounts cancel?
            if abs(d["amount"] + c["amount"]) / d["amount"] <= amount_tolerance:
                pairs.append({
                    "debit_id":  d["transaction_id"],
                    "credit_id": c["transaction_id"],
                    "amount":    d["amount"],
                    "date":      d["date"],
                })
                used_credit_ids.add(c["transaction_id"])
                break

    return pairs


def cashflow_summary(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    transfer_categories: frozenset | None = None,
    detect_transfers: bool = True,
    transfer_window_days: int = 2,
    transfer_amount_tolerance: float = 0.02,
    exclude_pending: bool = True,
) -> dict:
    """
    Compute net cashflow for a month.

    Returns:
        {
            "income":              float,   # credits, excl. transfers
            "expenses":            float,   # debits, excl. transfers
            "net":                 float,   # income - expenses
            "transfers_in":        float,   # sum of credit-side transfers
            "transfers_out":       float,   # sum of debit-side transfers
            "internal_pairs":      int,     # auto-detected internal transfer pairs
            "income_txns":         int,
            "expense_txns":        int,
            "by_income_category":  list[dict],  # [{category, amount, count}]
        }
    """
    if transfer_categories is None:
        transfer_categories = _DEFAULT_TRANSFER_CATS

    start, end = _month_bounds(year, month)
    pending_filter = "AND pending = 0" if exclude_pending else ""

    # IDs to exclude as detected internal transfers
    internal_ids: set[str] = set()
    internal_pairs = 0
    if detect_transfers:
        pairs = detect_internal_transfers(
            conn, year, month, transfer_window_days, transfer_amount_tolerance
        )
        for p in pairs:
            internal_ids.add(p["debit_id"])
            internal_ids.add(p["credit_id"])
        internal_pairs = len(pairs)

    # Build exclusion placeholders
    cat_placeholders = ",".join("?" for _ in transfer_categories)
    id_placeholders  = ",".join("?" for _ in internal_ids) if internal_ids else "NULL"

    rows = conn.execute(
        f"""
        SELECT
            transaction_id,
            amount,
            COALESCE(category_primary, 'UNCATEGORIZED') AS category
        FROM transactions
        WHERE date >= ? AND date < ?
              {pending_filter}
        """,
        (start, end),
    ).fetchall()

    income = 0.0
    expenses = 0.0
    transfers_in = 0.0
    transfers_out = 0.0
    income_txns = 0
    expense_txns = 0
    income_by_cat: dict[str, float] = {}
    income_count_by_cat: dict[str, int] = {}

    for r in rows:
        txn_id  = r["transaction_id"]
        amount  = r["amount"]
        cat     = r["category"]
        is_transfer_cat = cat in transfer_categories
        is_internal     = txn_id in internal_ids

        if is_transfer_cat or is_internal:
            if amount < 0:
                transfers_in += abs(amount)
            else:
                transfers_out += amount
            continue

        if amount < 0:
            # Credit = income
            income += abs(amount)
            income_txns += 1
            income_by_cat[cat] = income_by_cat.get(cat, 0.0) + abs(amount)
            income_count_by_cat[cat] = income_count_by_cat.get(cat, 0) + 1
        else:
            expenses += amount
            expense_txns += 1

    by_income_category = sorted(
        [
            {"category": cat, "amount": amt, "count": income_count_by_cat[cat]}
            for cat, amt in income_by_cat.items()
        ],
        key=lambda r: r["amount"],
        reverse=True,
    )

    return {
        "income":             round(income, 2),
        "expenses":           round(expenses, 2),
        "net":                round(income - expenses, 2),
        "transfers_in":       round(transfers_in, 2),
        "transfers_out":      round(transfers_out, 2),
        "internal_pairs":     internal_pairs,
        "income_txns":        income_txns,
        "expense_txns":       expense_txns,
        "by_income_category": by_income_category,
    }


def cashflow_trend(
    conn: sqlite3.Connection,
    months: int = 6,
    transfer_categories: frozenset | None = None,
    exclude_pending: bool = True,
) -> list[dict]:
    """
    Net cashflow for each of the last N months, oldest first.
    Returns [{year, month, income, expenses, net}, ...]
    """
    today = date.today()
    results = []
    for i in range(months - 1, -1, -1):
        # Walk backwards: i months ago
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        cf = cashflow_summary(conn, y, m, transfer_categories, exclude_pending=exclude_pending)
        results.append({"year": y, "month": m, **cf})
    return results
