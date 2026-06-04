"""
Sandbox smoke tests for fintrack.

Unit tests run everywhere.
Integration tests (marked sandbox) require real Plaid sandbox credentials in .env
and are skipped by default — run them with:

    pytest -m sandbox
"""

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from fintrack.classification import build_chain
from fintrack.classification.base import ClassificationResult
from fintrack.db import get_connection, insert_balance_snapshot, insert_item, migrate, upsert_account


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "test.db")
    migrate(path)
    conn = get_connection(path)
    yield conn
    conn.close()


@pytest.fixture()
def seeded_db(db):
    insert_item(db, "item-test-001", "access-sandbox-xxx", "Test Bank")
    upsert_account(db, "acct-test-001", "item-test-001", "Checking", "depository", "checking")
    db.commit()
    return db


# ── Classifier tests ──────────────────────────────────────────────────────────

class TestRulesClassifier:
    def test_amazon_match(self):
        chain = build_chain(["rules"])
        result = chain.classify({"merchant_name": "Amazon.com", "name": "AMAZON AMZN.COM"})
        assert result.category == "SHOPPING"
        assert result.source == "rules"
        assert result.confidence > 0.8

    def test_starbucks_match(self):
        chain = build_chain(["rules"])
        result = chain.classify({"merchant_name": "Starbucks", "name": "STARBUCKS #12345"})
        assert result.category == "FOOD_AND_DRINK"
        assert result.subcategory == "COFFEE_SHOP"

    def test_uber_not_uber_eats(self):
        chain = build_chain(["rules"])
        r_ride = chain.classify({"merchant_name": "Uber", "name": "UBER * TRIP"})
        r_eats = chain.classify({"merchant_name": "Uber Eats", "name": "UBER* EATS"})
        assert r_ride.subcategory == "RIDESHARE"
        assert r_eats.subcategory == "FOOD_DELIVERY"

    def test_unknown_merchant_returns_none(self):
        from fintrack.classification.rules import RulesClassifier
        classifier = RulesClassifier()
        result = classifier.classify({"merchant_name": "ZXYQ RANDOM 99999", "name": "ZXYQ"})
        assert result is None


class TestPlaidClassifier:
    def test_uses_personal_finance_category(self):
        chain = build_chain(["plaid"])
        result = chain.classify({
            "merchant_name": "Anywhere",
            "personal_finance_category": {
                "primary": "FOOD_AND_DRINK",
                "detailed": "FOOD_AND_DRINK_RESTAURANTS",
                "confidence_level": "HIGH",
            },
        })
        assert result.category == "FOOD_AND_DRINK"
        assert result.source == "plaid"
        assert result.confidence == 0.80

    def test_missing_pfc_returns_none(self):
        from fintrack.classification.plaid import PlaidClassifier
        result = PlaidClassifier().classify({"merchant_name": "Foo", "personal_finance_category": None})
        assert result is None


class TestClassifierChain:
    def test_rules_takes_precedence_over_plaid(self):
        chain = build_chain(["rules", "plaid"])
        txn = {
            "merchant_name": "Starbucks",
            "name": "STARBUCKS",
            "personal_finance_category": {
                "primary": "FOOD_AND_DRINK",
                "detailed": "FOOD_AND_DRINK_COFFEE",
                "confidence_level": "VERY_HIGH",
            },
        }
        result = chain.classify(txn)
        assert result.source == "rules"

    def test_falls_back_to_plaid(self):
        chain = build_chain(["rules", "plaid"])
        txn = {
            "merchant_name": "ZXYQ Unknwon Place 99",
            "name": "ZXYQ",
            "personal_finance_category": {
                "primary": "GENERAL_MERCHANDISE",
                "detailed": "GENERAL_MERCHANDISE_OTHER",
                "confidence_level": "LOW",
            },
        }
        result = chain.classify(txn)
        assert result.source == "plaid"

    def test_fallback_when_no_classifiers_match(self):
        chain = build_chain(["rules"])
        result = chain.classify({"merchant_name": "ZXYQ 99999", "name": "ZXYQ", "personal_finance_category": None})
        assert result.category == "UNCATEGORIZED"
        assert result.source == "fallback"

    def test_unknown_classifier_name_warns(self):
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            build_chain(["nonexistent"])
        assert any("nonexistent" in str(warning.message) for warning in w)


# ── DB schema tests ───────────────────────────────────────────────────────────

class TestDB:
    def test_migrate_creates_tables(self, db):
        tables = {
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"items", "accounts", "transactions", "balance_snapshots"}.issubset(tables)

    def test_migrate_creates_net_worth_view(self, db):
        views = {
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            ).fetchall()
        }
        assert "net_worth_snapshots" in views

    def test_insert_item_and_retrieve(self, db):
        insert_item(db, "item-abc", "access-token-xyz", "Bank of America")
        db.commit()
        row = db.execute("SELECT * FROM items WHERE item_id = 'item-abc'").fetchone()
        assert row is not None
        assert row["institution_name"] == "Bank of America"
        assert row["cursor"] is None

    def test_upsert_item_updates_token(self, db):
        insert_item(db, "item-abc", "token-v1", "Bank of America")
        insert_item(db, "item-abc", "token-v2", "Bank of America")
        db.commit()
        row = db.execute("SELECT * FROM items WHERE item_id = 'item-abc'").fetchone()
        assert row["access_token"] == "token-v2"

    def test_cursor_persistence(self, seeded_db):
        from fintrack.db import update_item_cursor
        update_item_cursor(seeded_db, "item-test-001", "cursor-abc-123")
        seeded_db.commit()
        row = seeded_db.execute("SELECT cursor FROM items WHERE item_id = 'item-test-001'").fetchone()
        assert row["cursor"] == "cursor-abc-123"


# ── Balance snapshot tests ────────────────────────────────────────────────────

class TestBalanceSnapshots:
    def test_insert_and_retrieve(self, seeded_db):
        insert_balance_snapshot(
            seeded_db, "acct-test-001", "2026-06-04T10:30:00",
            current_balance=5000.0, available_balance=4800.0,
            limit_amount=None, iso_currency_code="USD",
        )
        seeded_db.commit()
        rows = seeded_db.execute("SELECT * FROM balance_snapshots").fetchall()
        assert len(rows) == 1
        assert rows[0]["current_balance"] == 5000.0
        assert rows[0]["iso_currency_code"] == "USD"

    def test_net_worth_view_assets_minus_liabilities(self, seeded_db):
        upsert_account(seeded_db, "acct-credit-001", "item-test-001", "Visa", "credit", "credit card")
        seeded_db.commit()

        ts = "2026-06-04T10:30:00"
        insert_balance_snapshot(seeded_db, "acct-test-001", ts, 5000.0, 4800.0, None, "USD")
        insert_balance_snapshot(seeded_db, "acct-credit-001", ts, 1200.0, None, 5000.0, "USD")
        seeded_db.commit()

        row = seeded_db.execute("SELECT * FROM net_worth_snapshots").fetchone()
        assert row is not None
        assert row["total_assets"] == 5000.0
        assert row["total_liabilities"] == 1200.0
        assert row["net_worth"] == pytest.approx(3800.0)

    def test_net_worth_view_hour_bucketing(self, seeded_db):
        # Two snapshots in the same hour should collapse into one view row
        insert_balance_snapshot(seeded_db, "acct-test-001", "2026-06-04T10:05:00", 5000.0, None, None)
        insert_balance_snapshot(seeded_db, "acct-test-001", "2026-06-04T10:50:00", 5200.0, None, None)
        seeded_db.commit()

        rows = seeded_db.execute("SELECT * FROM net_worth_snapshots").fetchall()
        assert len(rows) == 1
        assert rows[0]["snapshot_hour"] == "2026-06-04T10:00:00"

    def test_net_worth_view_separate_hours(self, seeded_db):
        insert_balance_snapshot(seeded_db, "acct-test-001", "2026-06-04T10:30:00", 5000.0, None, None)
        insert_balance_snapshot(seeded_db, "acct-test-001", "2026-06-05T11:30:00", 5200.0, None, None)
        seeded_db.commit()

        rows = seeded_db.execute(
            "SELECT * FROM net_worth_snapshots ORDER BY snapshot_hour"
        ).fetchall()
        assert len(rows) == 2
        assert rows[1]["net_worth"] > rows[0]["net_worth"]


# ── Sync unit tests (mocked Plaid client) ─────────────────────────────────────

def _make_mock_txn(txn_id: str, account_id: str = "acct-test-001") -> MagicMock:
    m = MagicMock()
    m.to_dict.return_value = {
        "transaction_id": txn_id,
        "account_id": account_id,
        "date": "2024-03-15",
        "amount": 12.50,
        "merchant_name": "Starbucks",
        "name": "STARBUCKS #123",
        "pending": False,
        "personal_finance_category": {
            "primary": "FOOD_AND_DRINK",
            "detailed": "FOOD_AND_DRINK_COFFEE",
            "confidence_level": "VERY_HIGH",
        },
    }
    return m


def _make_mock_account(
    account_id: str = "acct-test-001",
    balance_current: float = 5000.0,
    balance_available: float = 4800.0,
    account_type: str = "depository",
) -> MagicMock:
    m = MagicMock()
    m.to_dict.return_value = {
        "account_id": account_id,
        "name": "Checking",
        "type": account_type,
        "subtype": "checking",
        "balances": {
            "current": balance_current,
            "available": balance_available,
            "limit": None,
            "iso_currency_code": "USD",
        },
    }
    return m


class TestSyncUnit:
    def test_sync_item_adds_transactions(self, seeded_db):
        from fintrack.sync import sync_item

        page = MagicMock()
        page.accounts = [_make_mock_account()]
        page.added = [_make_mock_txn("txn-001"), _make_mock_txn("txn-002")]
        page.modified = []
        page.removed = []
        page.next_cursor = "cursor-after-page-1"
        page.has_more = False

        mock_client = MagicMock()
        mock_client.transactions_sync.return_value = page

        chain = build_chain(["rules", "plaid"])
        item = {
            "item_id": "item-test-001",
            "access_token": "access-sandbox-xxx",
            "cursor": None,
        }

        stats = sync_item(mock_client, seeded_db, item, chain)
        assert stats["added"] == 2
        assert stats["modified"] == 0
        assert stats["removed"] == 0

        rows = seeded_db.execute("SELECT * FROM transactions").fetchall()
        assert len(rows) == 2

    def test_sync_persists_cursor_per_page(self, seeded_db):
        from fintrack.sync import sync_item

        page1 = MagicMock()
        page1.accounts = [_make_mock_account()]
        page1.added = [_make_mock_txn("txn-p1")]
        page1.modified = []
        page1.removed = []
        page1.next_cursor = "cursor-after-page-1"
        page1.has_more = True

        page2 = MagicMock()
        page2.accounts = [_make_mock_account()]
        page2.added = [_make_mock_txn("txn-p2")]
        page2.modified = []
        page2.removed = []
        page2.next_cursor = "cursor-after-page-2"
        page2.has_more = False

        mock_client = MagicMock()
        mock_client.transactions_sync.side_effect = [page1, page2]

        chain = build_chain(["rules"])
        item = {"item_id": "item-test-001", "access_token": "access-sandbox-xxx", "cursor": None}

        sync_item(mock_client, seeded_db, item, chain)

        cursor_row = seeded_db.execute(
            "SELECT cursor FROM items WHERE item_id = 'item-test-001'"
        ).fetchone()
        assert cursor_row["cursor"] == "cursor-after-page-2"

    def test_sync_writes_balance_snapshots(self, seeded_db):
        from fintrack.sync import sync_item

        page = MagicMock()
        page.accounts = [_make_mock_account(balance_current=7500.0, balance_available=7000.0)]
        page.added = []
        page.modified = []
        page.removed = []
        page.next_cursor = "cursor-snap"
        page.has_more = False

        mock_client = MagicMock()
        mock_client.transactions_sync.return_value = page

        chain = build_chain(["rules"])
        item = {"item_id": "item-test-001", "access_token": "access-sandbox-xxx", "cursor": None}

        sync_item(mock_client, seeded_db, item, chain)

        snaps = seeded_db.execute("SELECT * FROM balance_snapshots").fetchall()
        assert len(snaps) == 1
        assert snaps[0]["account_id"] == "acct-test-001"
        assert snaps[0]["current_balance"] == 7500.0
        assert snaps[0]["available_balance"] == 7000.0
        assert snaps[0]["iso_currency_code"] == "USD"

    def test_sync_multi_page_uses_last_balance(self, seeded_db):
        """Later pages overwrite earlier balance data; final snapshot reflects last-seen balance."""
        from fintrack.sync import sync_item

        page1 = MagicMock()
        page1.accounts = [_make_mock_account(balance_current=1000.0)]
        page1.added = [_make_mock_txn("txn-p1")]
        page1.modified = []
        page1.removed = []
        page1.next_cursor = "cursor-p1"
        page1.has_more = True

        page2 = MagicMock()
        page2.accounts = [_make_mock_account(balance_current=2000.0)]
        page2.added = []
        page2.modified = []
        page2.removed = []
        page2.next_cursor = "cursor-p2"
        page2.has_more = False

        mock_client = MagicMock()
        mock_client.transactions_sync.side_effect = [page1, page2]

        chain = build_chain(["rules"])
        item = {"item_id": "item-test-001", "access_token": "access-sandbox-xxx", "cursor": None}
        sync_item(mock_client, seeded_db, item, chain)

        snaps = seeded_db.execute("SELECT * FROM balance_snapshots").fetchall()
        assert len(snaps) == 1
        assert snaps[0]["current_balance"] == 2000.0

    def test_sync_removes_transactions(self, seeded_db):
        from fintrack.db import upsert_transaction
        from fintrack.sync import sync_item

        upsert_transaction(
            seeded_db, "txn-to-delete", "acct-test-001", "2024-03-10",
            5.0, "Old Merchant", "OLD MERCHANT", "FOOD_AND_DRINK", "", 0.5, "rules",
            False, json.dumps({}),
        )
        seeded_db.commit()

        removal = MagicMock()
        removal.transaction_id = "txn-to-delete"

        page = MagicMock()
        page.accounts = [_make_mock_account()]
        page.added = []
        page.modified = []
        page.removed = [removal]
        page.next_cursor = "cursor-x"
        page.has_more = False

        mock_client = MagicMock()
        mock_client.transactions_sync.return_value = page

        chain = build_chain(["rules"])
        item = {"item_id": "item-test-001", "access_token": "access-sandbox-xxx", "cursor": None}

        stats = sync_item(mock_client, seeded_db, item, chain)
        assert stats["removed"] == 1
        row = seeded_db.execute(
            "SELECT * FROM transactions WHERE transaction_id = 'txn-to-delete'"
        ).fetchone()
        assert row is None


# ── Sandbox integration tests (require real credentials) ──────────────────────

@pytest.mark.sandbox
class TestSandboxIntegration:
    """
    These tests hit the real Plaid sandbox API.
    Run with: pytest -m sandbox

    They require PLAID_CLIENT_ID and PLAID_SECRET to be set in .env,
    and a pre-linked sandbox item in the database.
    """

    def test_sync_with_sandbox_credentials(self, seeded_db):
        pytest.skip("Set up a sandbox item first via `fintrack link`, then remove this skip.")

        from fintrack.config import get_settings
        from fintrack.plaid_client import create_client
        from fintrack.sync import sync_item

        s = get_settings()
        client = create_client(s.plaid_client_id, s.plaid_secret, s.plaid_env)
        chain = build_chain(s.get_classifier_chain())

        # Replace with a real item from your sandbox DB
        item = {
            "item_id": "YOUR_SANDBOX_ITEM_ID",
            "access_token": "YOUR_SANDBOX_ACCESS_TOKEN",
            "cursor": None,
        }

        stats = sync_item(client, seeded_db, item, chain)
        assert stats["added"] >= 0, "Sync should return without error"
