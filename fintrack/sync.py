"""
Cursor-aware transaction sync using Plaid's /transactions/sync endpoint.

Design notes:
- The cursor is persisted to SQLite after *each page*, not just at the end
  of a full sync. This means a crash mid-sync loses at most one page worth
  of transaction metadata; the next run picks up exactly where the last
  successful commit left off.
- Accounts are upserted from every sync response, so the accounts table
  stays current without a separate fetch.
- amount follows Plaid's sign convention: positive = debit (money out),
  negative = credit (money in).
"""

import json
import sqlite3
from typing import TYPE_CHECKING

from plaid.model.transactions_sync_request import TransactionsSyncRequest

from .classification.base import ClassifierChain
from .db import delete_transaction, update_item_cursor, upsert_account, upsert_transaction

if TYPE_CHECKING:
    from plaid.api import plaid_api


def _fetch_page(
    client: "plaid_api.PlaidApi",
    access_token: str,
    cursor: str | None,
) -> object:
    kwargs: dict = {"access_token": access_token}
    if cursor:
        kwargs["cursor"] = cursor
    return client.transactions_sync(TransactionsSyncRequest(**kwargs))


def _process_transaction(
    conn: sqlite3.Connection,
    txn_dict: dict,
    classifier: ClassifierChain,
) -> None:
    result = classifier.classify(txn_dict)

    date = txn_dict.get("date")
    if hasattr(date, "isoformat"):
        date = date.isoformat()

    upsert_transaction(
        conn=conn,
        transaction_id=txn_dict["transaction_id"],
        account_id=txn_dict["account_id"],
        date=str(date),
        amount=txn_dict.get("amount", 0.0),
        merchant_name=txn_dict.get("merchant_name"),
        raw_name=txn_dict.get("name"),
        category_primary=result.category,
        category_detailed=result.subcategory,
        category_confidence=result.confidence,
        category_source=result.source,
        pending=bool(txn_dict.get("pending", False)),
        raw_json=json.dumps(txn_dict, default=str),
    )


def _process_account(
    conn: sqlite3.Connection,
    account_dict: dict,
    item_id: str,
) -> None:
    upsert_account(
        conn=conn,
        account_id=account_dict["account_id"],
        item_id=item_id,
        name=account_dict.get("name", ""),
        account_type=str(account_dict.get("type", "")) or None,
        subtype=str(account_dict.get("subtype", "")) or None,
    )


def sync_item(
    client: "plaid_api.PlaidApi",
    conn: sqlite3.Connection,
    item: dict,
    classifier: ClassifierChain,
) -> dict:
    """
    Sync one Item to completion, paginating through all pages.

    Returns a stats dict: {"added": int, "modified": int, "removed": int}.
    """
    item_id = item["item_id"]
    access_token = item["access_token"]
    cursor: str | None = item.get("cursor") or None

    stats = {"added": 0, "modified": 0, "removed": 0}
    has_more = True

    while has_more:
        response = _fetch_page(client, access_token, cursor)

        for account in response.accounts:
            _process_account(conn, account.to_dict(), item_id)

        for txn in response.added:
            _process_transaction(conn, txn.to_dict(), classifier)
            stats["added"] += 1

        for txn in response.modified:
            _process_transaction(conn, txn.to_dict(), classifier)
            stats["modified"] += 1

        for removed in response.removed:
            delete_transaction(conn, removed.transaction_id)
            stats["removed"] += 1

        cursor = response.next_cursor
        has_more = response.has_more

        # Commit after each page — cursor is durable even if next page fails.
        update_item_cursor(conn, item_id, cursor)
        conn.commit()

    return stats


def sync_all_items(
    client: "plaid_api.PlaidApi",
    conn: sqlite3.Connection,
    classifier: ClassifierChain,
) -> dict[str, dict]:
    """
    Sync every Item in the database.
    Returns {item_id: stats_dict}.
    """
    from .db import get_all_items

    items = get_all_items(conn)
    results: dict[str, dict] = {}

    for item in items:
        results[item["item_id"]] = sync_item(client, conn, item, classifier)

    return results
