"""
Reporting utilities — all queries operate over the local SQLite DB.
Amounts follow Plaid's sign convention: positive = spending (debit),
negative = income/credit. Reports treat positive amounts as expenses.
"""

import sqlite3
from collections import defaultdict
from datetime import date, timedelta


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """Return (first_day_iso, last_day_iso) for the given month."""
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return start.isoformat(), end.isoformat()


def monthly_summary(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    exclude_pending: bool = True,
    exclude_flagged: bool = False,
) -> list[dict]:
    """
    Spending by category for a given month.

    Returns a list of dicts sorted by total_amount descending:
      [{category, total_amount, transaction_count}, ...]

    When exclude_flagged=True, transactions with a row in transaction_flags
    are omitted from all totals.
    """
    start, end = _month_bounds(year, month)
    pending_filter = "AND pending = 0" if exclude_pending else ""
    flagged_filter = (
        "AND transaction_id NOT IN (SELECT transaction_id FROM transaction_flags)"
        if exclude_flagged else ""
    )

    rows = conn.execute(
        f"""
        SELECT
            COALESCE(category_primary, 'UNCATEGORIZED') AS category,
            SUM(amount)                                  AS total_amount,
            COUNT(*)                                     AS transaction_count
        FROM transactions
        WHERE date >= ? AND date < ? AND amount > 0
              {pending_filter}
              {flagged_filter}
        GROUP BY category_primary
        ORDER BY total_amount DESC
        """,
        (start, end),
    ).fetchall()

    return [dict(r) for r in rows]


def flagged_in_period(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    exclude_pending: bool = True,
) -> dict:
    """
    Count and sum of flagged transactions with positive amounts in the period.

    Returns {count, total_amount} — both are 0 when no flagged transactions exist.
    """
    start, end = _month_bounds(year, month)
    pending_filter = "AND t.pending = 0" if exclude_pending else ""

    row = conn.execute(
        f"""
        SELECT
            COUNT(*)                        AS count,
            COALESCE(SUM(t.amount), 0.0)    AS total_amount
        FROM transactions t
        JOIN transaction_flags f ON f.transaction_id = t.transaction_id
        WHERE t.date >= ? AND t.date < ? AND t.amount > 0
              {pending_filter}
        """,
        (start, end),
    ).fetchone()
    return dict(row)


def top_merchants(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    limit: int = 10,
    exclude_pending: bool = True,
) -> list[dict]:
    """
    Top merchants by spend for a given month.

    Returns [{merchant, total_amount, transaction_count}, ...]
    """
    start, end = _month_bounds(year, month)
    pending_filter = "AND pending = 0" if exclude_pending else ""

    rows = conn.execute(
        f"""
        SELECT
            COALESCE(merchant_name, raw_name, 'Unknown') AS merchant,
            SUM(amount)                                   AS total_amount,
            COUNT(*)                                      AS transaction_count
        FROM transactions
        WHERE date >= ? AND date < ? AND amount > 0
              {pending_filter}
        GROUP BY COALESCE(merchant_name, raw_name)
        ORDER BY total_amount DESC
        LIMIT ?
        """,
        (start, end, limit),
    ).fetchall()

    return [dict(r) for r in rows]


def mom_trends(
    conn: sqlite3.Connection,
    months: int = 6,
    exclude_pending: bool = True,
) -> list[dict]:
    """
    Month-over-month spending totals for the last N months.

    Returns [{year, month, total_amount, transaction_count}, ...] oldest-first.
    """
    pending_filter = "AND pending = 0" if exclude_pending else ""

    rows = conn.execute(
        f"""
        SELECT
            CAST(strftime('%Y', date) AS INTEGER) AS year,
            CAST(strftime('%m', date) AS INTEGER) AS month,
            SUM(amount)                           AS total_amount,
            COUNT(*)                              AS transaction_count
        FROM transactions
        WHERE amount > 0
              {pending_filter}
        GROUP BY strftime('%Y-%m', date)
        ORDER BY strftime('%Y-%m', date) DESC
        LIMIT ?
        """,
        (months,),
    ).fetchall()

    return list(reversed([dict(r) for r in rows]))


def recent_transactions(
    conn: sqlite3.Connection,
    days: int = 90,
    exclude_pending: bool = False,
) -> list[dict]:
    """
    Transactions from the last N days, joined with account and institution info.
    Returns rows sorted by date descending, then amount descending.

    Each dict includes: date, institution_name, account_name, merchant_name,
    raw_name, category_primary, category_detailed, category_source, amount, pending.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    pending_filter = "AND t.pending = 0" if exclude_pending else ""

    rows = conn.execute(
        f"""
        SELECT
            t.transaction_id,
            t.date,
            i.institution_name,
            a.name              AS account_name,
            t.merchant_name,
            t.raw_name,
            t.category_primary,
            t.category_detailed,
            t.category_source,
            t.amount,
            t.pending
        FROM transactions t
        JOIN accounts a ON a.account_id = t.account_id
        JOIN items    i ON i.item_id    = a.item_id
        WHERE t.date >= ? {pending_filter}
        ORDER BY t.date DESC, t.amount DESC
        """,
        (cutoff,),
    ).fetchall()

    return [dict(r) for r in rows]


def tax_summary(
    conn: sqlite3.Connection,
    year: int,
) -> list[dict]:
    """
    Spending totals grouped by tax_category for a given tax year.

    Returns [{tax_category, total_amount, transaction_count,
              transactions: [{date, merchant, amount, note}, ...]}, ...]
    sorted by total_amount descending.
    """
    rows = conn.execute(
        """
        SELECT
            tt.tax_category,
            SUM(t.amount)  AS total_amount,
            COUNT(*)       AS transaction_count
        FROM tax_tags tt
        JOIN transactions t ON t.transaction_id = tt.transaction_id
        WHERE tt.tax_year = ?
        GROUP BY tt.tax_category
        ORDER BY total_amount DESC
        """,
        (year,),
    ).fetchall()

    summary = []
    for r in rows:
        cat = r["tax_category"]
        txn_rows = conn.execute(
            """
            SELECT
                t.date,
                COALESCE(t.merchant_name, t.raw_name, 'Unknown') AS merchant,
                t.amount,
                tt.note,
                tt.tag_id
            FROM tax_tags tt
            JOIN transactions t ON t.transaction_id = tt.transaction_id
            WHERE tt.tax_year = ? AND tt.tax_category = ?
            ORDER BY t.date DESC
            """,
            (year, cat),
        ).fetchall()
        summary.append({
            "tax_category": cat,
            "total_amount": r["total_amount"],
            "transaction_count": r["transaction_count"],
            "transactions": [dict(tr) for tr in txn_rows],
        })

    return summary


def category_trends(
    conn: sqlite3.Connection,
    months: int = 6,
    exclude_pending: bool = True,
) -> dict[str, list[dict]]:
    """
    Per-category spending over the last N months.

    Returns {category: [{year, month, total_amount}, ...]} oldest-first.
    """
    pending_filter = "AND pending = 0" if exclude_pending else ""

    rows = conn.execute(
        f"""
        SELECT
            COALESCE(category_primary, 'UNCATEGORIZED')   AS category,
            CAST(strftime('%Y', date) AS INTEGER)          AS year,
            CAST(strftime('%m', date) AS INTEGER)          AS month,
            SUM(amount)                                    AS total_amount
        FROM transactions
        WHERE amount > 0
              {pending_filter}
        GROUP BY category_primary, strftime('%Y-%m', date)
        ORDER BY strftime('%Y-%m', date) ASC
        """,
    ).fetchall()

    result: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        d = dict(row)
        category = d.pop("category")
        result[category].append(d)

    return dict(result)
