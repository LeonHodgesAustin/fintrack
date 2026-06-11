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
from fintrack.db import (
    get_connection, insert_balance_snapshot, insert_item, migrate, upsert_account,
    add_flag, get_all_flags, FLAG_TYPES,
    TAX_CATEGORIES, TAX_DOC_TYPES,
    add_tax_tag, remove_tax_tag, get_tax_tags,
    upsert_tax_document, mark_tax_document_received, get_tax_documents,
    get_tax_document, set_tax_document_file_path,
)
from fintrack.reports import monthly_summary, flagged_in_period


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


# ── Transaction flag tests ────────────────────────────────────────────────────

@pytest.fixture()
def txn_db(seeded_db):
    """seeded_db with two transactions for flag/report tests."""
    from fintrack.db import upsert_transaction
    upsert_transaction(
        seeded_db, "txn-flight", "acct-test-001", "2026-06-01", 450.0,
        "Delta Airlines", "DELTA AIR", "TRAVEL", "", 0.9, "rules", False, "{}",
    )
    upsert_transaction(
        seeded_db, "txn-groceries", "acct-test-001", "2026-06-05", 120.0,
        "Whole Foods", "WHOLE FOODS", "FOOD_AND_DRINK", "", 0.9, "rules", False, "{}",
    )
    seeded_db.commit()
    return seeded_db


class TestTransactionFlags:
    def test_flag_types_constant(self):
        assert set(FLAG_TYPES) == {"one-time", "reimbursable", "gift", "transfer", "other"}

    def test_add_flag_returns_id(self, txn_db):
        fid = add_flag(txn_db, "txn-flight", "one-time", "uncle 70th birthday")
        txn_db.commit()
        assert isinstance(fid, int) and fid > 0

    def test_get_all_flags_joins_transaction(self, txn_db):
        add_flag(txn_db, "txn-flight", "one-time", "birthday trip")
        txn_db.commit()
        flags = get_all_flags(txn_db)
        assert len(flags) == 1
        assert flags[0]["merchant"] == "Delta Airlines"
        assert flags[0]["flag_type"] == "one-time"
        assert flags[0]["note"] == "birthday trip"
        assert flags[0]["amount"] == 450.0

    def test_check_constraint_rejects_bad_type(self, txn_db):
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            txn_db.execute(
                "INSERT INTO transaction_flags (transaction_id, flag_type) VALUES (?, ?)",
                ("txn-flight", "invalid-type"),
            )

    def test_monthly_summary_exclude_flagged(self, txn_db):
        add_flag(txn_db, "txn-flight", "one-time")
        txn_db.commit()

        full = monthly_summary(txn_db, 2026, 6)
        norm = monthly_summary(txn_db, 2026, 6, exclude_flagged=True)

        full_total = sum(r["total_amount"] for r in full)
        norm_total = sum(r["total_amount"] for r in norm)

        assert full_total == pytest.approx(570.0)
        assert norm_total == pytest.approx(120.0)

    def test_monthly_summary_unflagged_unchanged(self, txn_db):
        # exclude_flagged=False (default) returns same totals regardless of flags
        add_flag(txn_db, "txn-flight", "gift")
        txn_db.commit()
        without = monthly_summary(txn_db, 2026, 6, exclude_flagged=False)
        assert sum(r["total_amount"] for r in without) == pytest.approx(570.0)

    def test_flagged_in_period_count_and_sum(self, txn_db):
        add_flag(txn_db, "txn-flight", "reimbursable", "expensed to work")
        txn_db.commit()
        fi = flagged_in_period(txn_db, 2026, 6)
        assert fi["count"] == 1
        assert fi["total_amount"] == pytest.approx(450.0)

    def test_flagged_in_period_zero_when_no_flags(self, txn_db):
        fi = flagged_in_period(txn_db, 2026, 6)
        assert fi["count"] == 0
        assert fi["total_amount"] == pytest.approx(0.0)

    def test_flagged_in_period_ignores_other_months(self, txn_db):
        add_flag(txn_db, "txn-flight", "one-time")
        txn_db.commit()
        # query a different month — should see nothing
        fi = flagged_in_period(txn_db, 2026, 5)
        assert fi["count"] == 0


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


# ── Tax tag tests ─────────────────────────────────────────────────────────────

class TestTaxTags:
    def test_categories_constant_has_expected_values(self):
        for cat in ("medical", "hsa_fsa", "charitable", "dependent_care", "education",
                    "self_employed", "home_office", "energy_credit", "mortgage_interest",
                    "investment", "alimony_paid", "state_local_tax", "estimated_tax", "other"):
            assert cat in TAX_CATEGORIES, f"Missing category: {cat}"

    def test_add_and_retrieve(self, txn_db):
        tag_id = add_tax_tag(txn_db, "txn-flight", "medical", 2026, "eye exam")
        txn_db.commit()
        tags = get_tax_tags(txn_db)
        assert len(tags) == 1
        assert tags[0]["tag_id"] == tag_id
        assert tags[0]["tax_category"] == "medical"
        assert tags[0]["tax_year"] == 2026
        assert tags[0]["note"] == "eye exam"
        assert tags[0]["amount"] == pytest.approx(450.0)
        assert tags[0]["merchant"] == "Delta Airlines"

    def test_filter_by_year(self, txn_db):
        add_tax_tag(txn_db, "txn-flight",    "medical",    2026)
        add_tax_tag(txn_db, "txn-groceries", "charitable", 2025)
        txn_db.commit()
        tags_2026 = get_tax_tags(txn_db, year=2026)
        assert len(tags_2026) == 1
        assert tags_2026[0]["tax_category"] == "medical"

    def test_filter_by_category(self, txn_db):
        add_tax_tag(txn_db, "txn-flight",    "medical",    2026)
        add_tax_tag(txn_db, "txn-groceries", "charitable", 2026)
        txn_db.commit()
        med = get_tax_tags(txn_db, tax_category="medical")
        assert len(med) == 1
        char = get_tax_tags(txn_db, tax_category="charitable")
        assert len(char) == 1

    def test_filter_by_year_and_category(self, txn_db):
        add_tax_tag(txn_db, "txn-flight",    "medical",    2026)
        add_tax_tag(txn_db, "txn-groceries", "medical",    2025)
        txn_db.commit()
        results = get_tax_tags(txn_db, year=2026, tax_category="medical")
        assert len(results) == 1

    def test_remove_tag(self, txn_db):
        tag_id = add_tax_tag(txn_db, "txn-flight", "dependent_care", 2026)
        txn_db.commit()
        ok = remove_tax_tag(txn_db, tag_id)
        txn_db.commit()
        assert ok is True
        assert get_tax_tags(txn_db) == []

    def test_remove_nonexistent_returns_false(self, txn_db):
        assert remove_tax_tag(txn_db, 9999) is False

    def test_tag_deleted_when_transaction_deleted(self, txn_db):
        add_tax_tag(txn_db, "txn-flight", "medical", 2026)
        txn_db.commit()
        txn_db.execute("DELETE FROM transactions WHERE transaction_id = 'txn-flight'")
        txn_db.commit()
        assert get_tax_tags(txn_db) == []


# ── Tax document tests ────────────────────────────────────────────────────────

class TestTaxDocuments:
    def test_doc_types_constant_has_expected_values(self):
        for dt in ("W-2", "1099-INT", "1099-DIV", "1099-B", "1099-SA", "1098", "1098-T"):
            assert dt in TAX_DOC_TYPES, f"Missing doc type: {dt}"

    def test_upsert_and_retrieve(self, db):
        doc_id = upsert_tax_document(db, 2025, "Bank of America", "1099-INT")
        db.commit()
        docs = get_tax_documents(db, year=2025)
        assert len(docs) == 1
        assert docs[0]["id"] == doc_id
        assert docs[0]["institution"] == "Bank of America"
        assert docs[0]["doc_type"] == "1099-INT"
        assert docs[0]["received"] == 0
        assert docs[0]["file_path"] is None

    def test_mark_received(self, db):
        doc_id = upsert_tax_document(db, 2025, "Schwab", "1099-B")
        db.commit()
        ok = mark_tax_document_received(db, doc_id, True, "2026-02-15")
        db.commit()
        assert ok is True
        doc = get_tax_document(db, doc_id)
        assert doc["received"] == 1
        assert doc["received_date"] == "2026-02-15"

    def test_mark_not_received(self, db):
        doc_id = upsert_tax_document(db, 2025, "Schwab", "1099-B")
        mark_tax_document_received(db, doc_id, True, "2026-01-31")
        mark_tax_document_received(db, doc_id, False, None)
        db.commit()
        doc = get_tax_document(db, doc_id)
        assert doc["received"] == 0

    def test_upsert_idempotent(self, db):
        upsert_tax_document(db, 2025, "Schwab", "1099-DIV")
        upsert_tax_document(db, 2025, "Schwab", "1099-DIV")
        db.commit()
        docs = get_tax_documents(db, year=2025)
        assert len(docs) == 1

    def test_filter_by_year(self, db):
        upsert_tax_document(db, 2025, "BofA", "1099-INT")
        upsert_tax_document(db, 2024, "BofA", "1099-INT")
        db.commit()
        assert len(get_tax_documents(db, year=2025)) == 1
        assert len(get_tax_documents(db, year=2024)) == 1
        assert len(get_tax_documents(db)) == 2

    def test_file_path_column_exists(self, db):
        doc_id = upsert_tax_document(db, 2025, "IRS", "W-2")
        db.commit()
        row = db.execute("SELECT file_path FROM tax_documents WHERE id=?", (doc_id,)).fetchone()
        assert row is not None
        assert row["file_path"] is None

    def test_set_file_path(self, db):
        doc_id = upsert_tax_document(db, 2025, "Employer", "W-2")
        db.commit()
        ok = set_tax_document_file_path(db, doc_id, "/docs/tax/W2_2025.pdf")
        db.commit()
        assert ok is True
        doc = get_tax_document(db, doc_id)
        assert doc["file_path"] == "/docs/tax/W2_2025.pdf"

    def test_set_file_path_nonexistent_returns_false(self, db):
        assert set_tax_document_file_path(db, 9999, "/foo.pdf") is False


# ── Doc scan filename-matching tests ─────────────────────────────────────────

class TestDocScanMatching:
    """Tests for the _match_doc_type filename-matching helper."""

    def _match(self, filename: str) -> str | None:
        from fintrack.cli import _match_doc_type
        return _match_doc_type(filename)

    def test_w2_hyphenated(self):
        assert self._match("W-2_Cisco_2025.pdf") == "W-2"

    def test_w2_no_hyphen(self):
        assert self._match("W2_Employer_2025.pdf") == "W-2"

    def test_w2_uppercase(self):
        assert self._match("2025_W2.PDF") == "W-2"

    def test_1099_int(self):
        assert self._match("1099-INT_BofA_2025.pdf") == "1099-INT"
        assert self._match("1099int_bofa.pdf") == "1099-INT"

    def test_1099_div(self):
        assert self._match("1099-DIV_Schwab.pdf") == "1099-DIV"
        assert self._match("schwab_1099div_2025.pdf") == "1099-DIV"

    def test_1099_b(self):
        assert self._match("1099-B Proceeds 2025.pdf") == "1099-B"
        assert self._match("consolidated_1099b.pdf") == "1099-B"

    def test_1099_nec(self):
        assert self._match("1099-NEC_freelance.pdf") == "1099-NEC"

    def test_1099_r(self):
        assert self._match("1099-R_retirement.pdf") == "1099-R"

    def test_1099_sa(self):
        assert self._match("1099-SA_HSA_2025.pdf") == "1099-SA"

    def test_ssa_1099(self):
        assert self._match("SSA-1099_2025.pdf") == "SSA-1099"
        assert self._match("ssa1099.pdf") == "SSA-1099"

    def test_1098_mortgage(self):
        assert self._match("1098_mortgage_interest.pdf") == "1098"
        assert self._match("MidIsland_1098_2025.PDF") == "1098"

    def test_1098_e_before_1098(self):
        assert self._match("1098-E Student Loan.pdf") == "1098-E"
        assert self._match("1098e_sallie_mae.pdf") == "1098-E"

    def test_1098_t_before_1098(self):
        assert self._match("1098-T_University.pdf") == "1098-T"
        assert self._match("1098t_tuition.pdf") == "1098-T"

    def test_no_match_returns_none(self):
        assert self._match("random_document.pdf") is None
        assert self._match("prior_year_return_2024.pdf") is None
        assert self._match("receipt_dentist.jpg") is None

    def test_case_insensitive(self):
        assert self._match("FORM_W-2_2025.PDF") == "W-2"
        assert self._match("1099-INT_BOFA.PDF") == "1099-INT"


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
