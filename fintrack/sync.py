"""
Cursor-aware transaction sync using Plaid's /transactions/sync endpoint.

Design notes:
- The cursor is persisted to SQLite after each page, not just at the end of a
  full sync. A crash mid-sync loses at most one page; the next run picks up
  exactly where the last successful commit left off.
- Accounts are upserted from every sync response so the accounts table stays
  current without a separate fetch.
- Overrides are loaded once per sync_item call. Any incoming transaction that
  has an override uses the override category unconditionally -- Plaid's own
  classification never overwrites a manual correction.
- amount follows Plaid's sign convention: positive = debit (money out),
  negative = credit (money in).
"""

import json
import sqlite3
from typing import TYPE_CHECKING

import plaid
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from .classification.base import ClassificationResult, ClassifierChain
from .db import (
    apply_overrides_to_transactions,
    clear_item_error,
    delete_transaction,
    get_all_overrides,
    set_item_error,
    update_item_cursor,
    upsert_account,
    upsert_transaction,
)

if TYPE_CHECKING:
    from plaid.api import plaid_api


class ItemAuthError(Exception):
    """Raised when Plaid reports ITEM_LOGIN_REQUIRED or similar auth failures."""

    def __init__(self, item_id: str, institution: str, error_code: str = "ITEM_LOGIN_REQUIRED"):
        self.item_id = item_id
        self.institution = institution
        self.error_code = error_code
        super().__init__(
            f"{institution} requires re-authentication (error_code: {error_code})"
        )


def _fetch_page(client, access_token: str, cursor: str | None):
    kwargs: dict = {"access_token": access_token}
    if cursor:
        kwargs["cursor"] = cursor
    return client.transactions_sync(TransactionsSyncRequest(**kwargs))


def _parse_plaid_error_code(exc: plaid.ApiException) -> str:
    try:
        body = json.loads(exc.body) if isinstance(exc.body, str) else (exc.body or {})
        return body.get("error_code", "UNKNOWN")
    except Exception:
        return "UNKNOWN"


def _process_transaction(
    conn: sqlite3.Connection,
    txn_dict: dict,
    classifier: ClassifierChain,
    overrides: dict,
) -> None:
    txn_id = txn_dict["transaction_id"]

    # Overrides always win -- never let a Plaid re-classification clobber a
    # manual correction.
    override = overrides.get(txn_id)
    if override:
        result = ClassificationResult(
            category=override["category"],
            subcategory=override["subcategory"],
            confidence=1.0,
            source="override",
        )
    else:
        result = classifier.classify(txn_dict)

    date = txn_dict.get("date")
    if hasattr(date, "isoformat"):
        date = date.isoformat()

    upsert_transaction(
        conn=conn,
        transaction_id=txn_id,
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


def _process_account(conn: sqlite3.Connection, account_dict: dict, item_id: str) -> None:
    upsert_account(
        conn=conn,
        account_id=account_dict["account_id"],
        item_id=item_id,
        name=account_dict.get("name", ""),
        account_type=str(account_dict.get("type", "")) or None,
        subtype=str(account_dict.get("subtype", "")) or None,
    )


def sync_item(
    client,
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

    # Load overrides once -- cheap dict lookup beats a DB hit per transaction
    overrides = get_all_overrides(conn)

    stats = {"added": 0, "modified": 0, "removed": 0}
    has_more = True

    while has_more:
        try:
            response = _fetch_page(client, access_token, cursor)
        except plaid.ApiException as exc:
            error_code = _parse_plaid_error_code(exc)
            set_item_error(conn, item_id, error_code)
            conn.commit()
            raise ItemAuthError(
                item_id=item_id,
                institution=item.get("institution_name", item_id),
                error_code=error_code,
            ) from exc

        for account in response.accounts:
            _process_account(conn, account.to_dict(), item_id)

        for txn in response.added:
            _process_transaction(conn, txn.to_dict(), classifier, overrides)
            stats["added"] += 1

        for txn in response.modified:
            _process_transaction(conn, txn.to_dict(), classifier, overrides)
            stats["modified"] += 1

        for removed in response.removed:
            delete_transaction(conn, removed.transaction_id)
            stats["removed"] += 1

        cursor = response.next_cursor
        has_more = response.has_more

        # Commit after each page -- cursor is durable even if next page fails
        update_item_cursor(conn, item_id, cursor)
        conn.commit()

    # Re-apply overrides in case any modified transactions reset their category
    apply_overrides_to_transactions(conn)
    conn.commit()

    # Clear any previous error state on success
    clear_item_error(conn, item_id)
    conn.commit()

    return stats


def sync_all_items(client, conn: sqlite3.Connection, classifier: ClassifierChain) -> dict:
    """Sync every Item in the database. Returns {item_id: stats_dict}."""
    from .db import get_all_items

    items = get_all_items(conn)
    results: dict[str, dict] = {}

    for item in items:
        results[item["item_id"]] = sync_item(client, conn, item, classifier)

    return results
