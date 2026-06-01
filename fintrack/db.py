import sqlite3
from datetime import datetime, timezone


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(db_path: str) -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    conn = get_connection(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS items (
                item_id          TEXT PRIMARY KEY,
                access_token     TEXT NOT NULL,
                institution_name TEXT NOT NULL,
                cursor           TEXT,
                last_synced      TEXT
            );

            CREATE TABLE IF NOT EXISTS accounts (
                account_id  TEXT PRIMARY KEY,
                item_id     TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
                name        TEXT NOT NULL,
                type        TEXT,
                subtype     TEXT
            );

            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id      TEXT PRIMARY KEY,
                account_id          TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                date                TEXT NOT NULL,
                amount              REAL NOT NULL,
                merchant_name       TEXT,
                raw_name            TEXT,
                category_primary    TEXT,
                category_detailed   TEXT,
                category_confidence REAL,
                category_source     TEXT,
                pending             INTEGER NOT NULL DEFAULT 0,
                raw_json            TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_txn_date        ON transactions(date);
            CREATE INDEX IF NOT EXISTS idx_txn_account     ON transactions(account_id);
            CREATE INDEX IF NOT EXISTS idx_txn_category    ON transactions(category_primary);
            CREATE INDEX IF NOT EXISTS idx_txn_pending     ON transactions(pending);
        """)
        conn.commit()
    finally:
        conn.close()


# ── Item helpers ──────────────────────────────────────────────────────────────

def insert_item(
    conn: sqlite3.Connection,
    item_id: str,
    access_token: str,
    institution_name: str,
) -> None:
    conn.execute(
        """
        INSERT INTO items (item_id, access_token, institution_name, cursor, last_synced)
        VALUES (?, ?, ?, NULL, NULL)
        ON CONFLICT(item_id) DO UPDATE SET
            access_token = excluded.access_token,
            institution_name = excluded.institution_name
        """,
        (item_id, access_token, institution_name),
    )


def update_item_cursor(conn: sqlite3.Connection, item_id: str, cursor: str) -> None:
    conn.execute(
        "UPDATE items SET cursor = ?, last_synced = ? WHERE item_id = ?",
        (cursor, datetime.now(timezone.utc).isoformat(), item_id),
    )


def get_all_items(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM items").fetchall()
    return [dict(r) for r in rows]


def get_item(conn: sqlite3.Connection, item_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def update_item_access_token(
    conn: sqlite3.Connection, item_id: str, access_token: str
) -> None:
    conn.execute(
        "UPDATE items SET access_token = ? WHERE item_id = ?",
        (access_token, item_id),
    )


# ── Account helpers ───────────────────────────────────────────────────────────

def upsert_account(
    conn: sqlite3.Connection,
    account_id: str,
    item_id: str,
    name: str,
    account_type: str | None,
    subtype: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO accounts (account_id, item_id, name, type, subtype)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            name = excluded.name,
            type = excluded.type,
            subtype = excluded.subtype
        """,
        (account_id, item_id, name, account_type, subtype),
    )


def get_accounts_for_item(conn: sqlite3.Connection, item_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM accounts WHERE item_id = ?", (item_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Transaction helpers ───────────────────────────────────────────────────────

def upsert_transaction(
    conn: sqlite3.Connection,
    transaction_id: str,
    account_id: str,
    date: str,
    amount: float,
    merchant_name: str | None,
    raw_name: str | None,
    category_primary: str | None,
    category_detailed: str | None,
    category_confidence: float | None,
    category_source: str | None,
    pending: bool,
    raw_json: str,
) -> None:
    conn.execute(
        """
        INSERT INTO transactions (
            transaction_id, account_id, date, amount, merchant_name, raw_name,
            category_primary, category_detailed, category_confidence, category_source,
            pending, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(transaction_id) DO UPDATE SET
            date                = excluded.date,
            amount              = excluded.amount,
            merchant_name       = excluded.merchant_name,
            raw_name            = excluded.raw_name,
            category_primary    = excluded.category_primary,
            category_detailed   = excluded.category_detailed,
            category_confidence = excluded.category_confidence,
            category_source     = excluded.category_source,
            pending             = excluded.pending,
            raw_json            = excluded.raw_json
        """,
        (
            transaction_id, account_id, date, amount, merchant_name, raw_name,
            category_primary, category_detailed, category_confidence, category_source,
            1 if pending else 0, raw_json,
        ),
    )


def delete_transaction(conn: sqlite3.Connection, transaction_id: str) -> None:
    conn.execute(
        "DELETE FROM transactions WHERE transaction_id = ?", (transaction_id,)
    )
