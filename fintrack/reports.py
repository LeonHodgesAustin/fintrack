"""
Reporting utilities — all queries operate over the local SQLite DB.
Amounts follow Plaid's sign convention: positive = spending (debit),
negative = income/credit. Reports treat positive amounts as expenses.
"""

import sqlite3
from collections import defaultdict
from datetime import date


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
) -> list[dict]:
    """
    Spending by category for a given month.

    Returns a list of dicts sorted by total_amount descending:
      [{category, total_amount, transaction_count}, ...]
    """
    start, end = _month_bounds(year, month)
    pending_filter = "AND pending = 0" if exclude_pending else ""

    rows = conn.execute(
        f"""
        SELECT
            COALESCE(category_primary, 'UNCATEGORIZED') AS category,
            SUM(amount)                                  AS total_amount,
            COUNT(*)                                     AS transaction_count
        FROM transactions
        WHERE date >= ? AND date < ? AND amount > 0
              {pending_filter}
        GROUP BY category_primary
        ORDER BY total_amount DESC
        """,
        (start, end),
    ).fetchall()

    return [dict(r) for r in rows]


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
