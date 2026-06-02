"""
Transaction override management for fintrack.

Overrides win over all classifiers. They are written by two paths:
  1. `fintrack review` -- interactive CLI review of low-confidence transactions
  2. Google Sheets roundtrip -- "Override Category" column in the Transactions tab

Both paths call set_override(), which updates both the overrides table and the
live transaction record immediately so reports reflect the correction at once.

The sync loop loads all overrides at the start of each item sync and applies
them to any incoming transaction that has one, preventing Plaid's classification
from overwriting a manual correction.
"""

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import get_all_overrides

# Plaid primary category values -- shown during interactive review
PLAID_CATEGORIES = [
    "FOOD_AND_DRINK",
    "SHOPPING",
    "TRANSPORTATION",
    "TRAVEL",
    "ENTERTAINMENT",
    "HEALTH",
    "UTILITIES",
    "TRANSFER_IN",
    "TRANSFER_OUT",
    "INCOME",
    "GENERAL_SERVICES",
    "GENERAL_MERCHANDISE",
    "LOAN_PAYMENTS",
    "BANK_FEES",
    "RENT_AND_UTILITIES",
    "HOME_IMPROVEMENT",
    "PERSONAL_CARE",
    "GOVERNMENT_AND_NON_PROFIT",
    "UNCATEGORIZED",
]


def get_review_candidates(
    conn: sqlite3.Connection,
    limit: int = 50,
    min_confidence: float = 0.60,
    exclude_pending: bool = True,
    exclude_transfers: bool = True,
) -> list[dict]:
    """
    Return transactions that are good candidates for manual review:
      - category_source = 'fallback'  (completely uncategorized)
      - OR confidence < min_confidence

    Excludes pending and internal transfers by default (they're noise).
    Sorted by confidence ascending (worst first).
    """
    pending_filter = "AND t.pending = 0" if exclude_pending else ""
    transfer_filter = (
        "AND t.category_primary NOT IN ('TRANSFER_IN', 'TRANSFER_OUT')"
        if exclude_transfers else ""
    )

    rows = conn.execute(
        f"""
        SELECT
            t.transaction_id,
            t.date,
            t.amount,
            COALESCE(t.merchant_name, t.raw_name, 'Unknown') AS merchant,
            t.category_primary,
            t.category_detailed,
            COALESCE(t.category_confidence, 0.0)             AS confidence,
            t.category_source,
            i.institution_name,
            a.name AS account_name
        FROM transactions t
        JOIN accounts a ON a.account_id = t.account_id
        JOIN items    i ON i.item_id    = a.item_id
        WHERE (t.category_source = 'fallback' OR t.category_confidence < ?)
              {pending_filter}
              {transfer_filter}
        ORDER BY t.category_confidence ASC NULLS FIRST, t.date DESC
        LIMIT ?
        """,
        (min_confidence, limit),
    ).fetchall()

    return [dict(r) for r in rows]
