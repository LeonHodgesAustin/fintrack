"""
Asset DB schema and CRUD helpers.

Tables:
  loans               — mortgage / auto / personal loan terms
  vehicles            — owned vehicles for depreciation tracking
  properties          — real estate with current market value
  equity_grants       — RSU grants and ESPP plans
  equity_transactions — vest / sell / ESPP purchase events
  stock_price_cache   — daily closing prices (avoid re-fetching)
"""

import json
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate(db_path: str) -> None:
    """Create asset tables if they don't exist. Safe to call on every startup."""
    from fintrack.db import get_connection
    conn = get_connection(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS loans (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                loan_type       TEXT NOT NULL DEFAULT 'mortgage',
                principal       REAL NOT NULL,
                annual_rate     REAL NOT NULL,
                term_months     INTEGER NOT NULL,
                start_date      TEXT NOT NULL,
                monthly_payment REAL,
                extra_json      TEXT NOT NULL DEFAULT '{}',
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS vehicles (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                name                 TEXT NOT NULL,
                purchase_price       REAL NOT NULL,
                purchase_date        TEXT NOT NULL,
                annual_depreciation  REAL NOT NULL DEFAULT 0.18,
                extra_json           TEXT NOT NULL DEFAULT '{}',
                created_at           TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS properties (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT NOT NULL,
                address           TEXT NOT NULL,
                purchase_price    REAL NOT NULL,
                purchase_date     TEXT NOT NULL,
                current_value     REAL,
                value_range_low   REAL,
                value_range_high  REAL,
                value_updated_at  TEXT,
                extra_json        TEXT NOT NULL DEFAULT '{}',
                created_at        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS equity_grants (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker                 TEXT NOT NULL,
                grant_type             TEXT NOT NULL,
                grant_date             TEXT NOT NULL,
                -- RSU fields
                total_shares           REAL,
                cliff_months           INTEGER,
                vest_months            INTEGER,
                vest_frequency         TEXT DEFAULT 'monthly',
                -- ESPP fields
                contribution_rate      REAL,
                discount_rate          REAL DEFAULT 0.15,
                purchase_period_months INTEGER DEFAULT 6,
                lookback_months        INTEGER DEFAULT 24,
                extra_json             TEXT NOT NULL DEFAULT '{}',
                created_at             TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS equity_transactions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                grant_id        INTEGER REFERENCES equity_grants(id),
                txn_type        TEXT NOT NULL,
                txn_date        TEXT NOT NULL,
                shares          REAL NOT NULL,
                price_per_share REAL NOT NULL,
                gross_amount    REAL NOT NULL,
                notes           TEXT,
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stock_price_cache (
                ticker      TEXT NOT NULL,
                price_date  TEXT NOT NULL,
                close_price REAL NOT NULL,
                fetched_at  TEXT NOT NULL,
                PRIMARY KEY (ticker, price_date)
            );

            CREATE TABLE IF NOT EXISTS manual_accounts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                name                TEXT NOT NULL,
                account_type        TEXT NOT NULL DEFAULT 'other',
                institution         TEXT,
                balance             REAL,
                balance_updated_at  TEXT,
                notes               TEXT,
                created_at          TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stock_holdings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT NOT NULL,
                shares          REAL NOT NULL,
                cost_basis      REAL,           -- average cost per share (optional)
                as_of_date      TEXT NOT NULL,  -- when this snapshot was recorded
                notes           TEXT,           -- e.g. "Schwab brokerage", "Inherited"
                created_at      TEXT NOT NULL
            );
        """)
        conn.commit()

        # Additive migrations for existing DBs
        for stmt in [
            "ALTER TABLE loans ADD COLUMN actual_balance REAL",
            "ALTER TABLE loans ADD COLUMN balance_updated_at TEXT",
        ]:
            try:
                conn.execute(stmt)
                conn.commit()
            except Exception:
                pass  # column already exists

    finally:
        conn.close()


# ── Properties ───────────────────────────────────────────────────────────────

def add_property(
    conn: sqlite3.Connection,
    name: str,
    address: str,
    purchase_price: float,
    purchase_date: str,
    current_value: float | None = None,
    extra: dict | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO properties
            (name, address, purchase_price, purchase_date, current_value,
             extra_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (name, address, purchase_price, purchase_date, current_value,
         json.dumps(extra or {}), _now()),
    )
    return cur.lastrowid


def get_properties(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM properties ORDER BY purchase_date").fetchall()
    return [dict(r) for r in rows]


def get_property(conn: sqlite3.Connection, property_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    return dict(row) if row else None


def set_property_value(
    conn: sqlite3.Connection,
    property_id: int,
    current_value: float,
    range_low: float | None = None,
    range_high: float | None = None,
) -> None:
    conn.execute(
        """
        UPDATE properties
        SET current_value    = ?,
            value_range_low  = ?,
            value_range_high = ?,
            value_updated_at = ?
        WHERE id = ?
        """,
        (current_value, range_low, range_high, _now(), property_id),
    )


def delete_property(conn: sqlite3.Connection, property_id: int) -> None:
    conn.execute("DELETE FROM properties WHERE id = ?", (property_id,))


# ── Loans ─────────────────────────────────────────────────────────────────────

def add_loan(
    conn: sqlite3.Connection,
    name: str,
    loan_type: str,
    principal: float,
    annual_rate: float,
    term_months: int,
    start_date: str,
    monthly_payment: float | None = None,
    extra: dict | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO loans
            (name, loan_type, principal, annual_rate, term_months, start_date,
             monthly_payment, extra_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (name, loan_type, principal, annual_rate, term_months, start_date,
         monthly_payment, json.dumps(extra or {}), _now()),
    )
    return cur.lastrowid


def get_loans(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM loans ORDER BY loan_type, start_date").fetchall()
    return [dict(r) for r in rows]


def update_loan(
    conn: sqlite3.Connection,
    loan_id: int,
    principal: float | None = None,
    annual_rate: float | None = None,
    term_months: int | None = None,
    start_date: str | None = None,
    monthly_payment: float | None = None,
) -> bool:
    """Update specific fields on a loan. Returns True if a row was found."""
    fields, values = [], []
    if principal is not None:
        fields.append("principal = ?");       values.append(principal)
    if annual_rate is not None:
        fields.append("annual_rate = ?");     values.append(annual_rate)
    if term_months is not None:
        fields.append("term_months = ?");     values.append(term_months)
    if start_date is not None:
        fields.append("start_date = ?");      values.append(start_date)
    if monthly_payment is not None:
        fields.append("monthly_payment = ?"); values.append(monthly_payment)
    if not fields:
        return False
    values.append(loan_id)
    cur = conn.execute(
        f"UPDATE loans SET {', '.join(fields)} WHERE id = ?", values
    )
    return cur.rowcount > 0


def set_loan_balance(
    conn: sqlite3.Connection,
    loan_id: int,
    actual_balance: float,
) -> None:
    """Override the calculated balance with a servicer-confirmed figure."""
    conn.execute(
        "UPDATE loans SET actual_balance = ?, balance_updated_at = ? WHERE id = ?",
        (actual_balance, _now(), loan_id),
    )


def clear_loan_balance(conn: sqlite3.Connection, loan_id: int) -> None:
    """Remove the balance override — revert to calculated amortization balance."""
    conn.execute(
        "UPDATE loans SET actual_balance = NULL, balance_updated_at = NULL WHERE id = ?",
        (loan_id,),
    )


def delete_loan(conn: sqlite3.Connection, loan_id: int) -> None:
    conn.execute("DELETE FROM loans WHERE id = ?", (loan_id,))


# ── Vehicles ──────────────────────────────────────────────────────────────────

def add_vehicle(
    conn: sqlite3.Connection,
    name: str,
    purchase_price: float,
    purchase_date: str,
    annual_depreciation: float = 0.18,
    extra: dict | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO vehicles
            (name, purchase_price, purchase_date, annual_depreciation, extra_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name, purchase_price, purchase_date, annual_depreciation,
         json.dumps(extra or {}), _now()),
    )
    return cur.lastrowid


def get_vehicles(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM vehicles ORDER BY purchase_date").fetchall()
    return [dict(r) for r in rows]


def delete_vehicle(conn: sqlite3.Connection, vehicle_id: int) -> None:
    conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))


# ── Equity grants ─────────────────────────────────────────────────────────────

def add_rsu_grant(
    conn: sqlite3.Connection,
    ticker: str,
    grant_date: str,
    total_shares: float,
    cliff_months: int,
    vest_months: int,
    vest_frequency: str = "monthly",
    extra: dict | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO equity_grants
            (ticker, grant_type, grant_date, total_shares, cliff_months,
             vest_months, vest_frequency, extra_json, created_at)
        VALUES (?, 'rsu', ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticker.upper(), grant_date, total_shares, cliff_months, vest_months,
         vest_frequency, json.dumps(extra or {}), _now()),
    )
    return cur.lastrowid


def add_espp_plan(
    conn: sqlite3.Connection,
    ticker: str,
    offering_start_date: str,
    contribution_rate: float,
    discount_rate: float = 0.15,
    purchase_period_months: int = 6,
    lookback_months: int = 24,
    extra: dict | None = None,
) -> int:
    """
    Add an ESPP plan.

    lookback_months: how far back the "lower of" price comparison reaches.
      24 = standard Cisco ESPP (offering period start vs. purchase date)
       6 = simpler interpretation (purchase period start vs. purchase date)
    Verify against your plan documents before changing this.
    """
    cur = conn.execute(
        """
        INSERT INTO equity_grants
            (ticker, grant_type, grant_date, contribution_rate, discount_rate,
             purchase_period_months, lookback_months, extra_json, created_at)
        VALUES (?, 'espp', ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticker.upper(), offering_start_date, contribution_rate, discount_rate,
         purchase_period_months, lookback_months, json.dumps(extra or {}), _now()),
    )
    return cur.lastrowid


def get_equity_grants(conn: sqlite3.Connection, grant_type: str | None = None) -> list[dict]:
    if grant_type:
        rows = conn.execute(
            "SELECT * FROM equity_grants WHERE grant_type = ? ORDER BY grant_date",
            (grant_type,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM equity_grants ORDER BY grant_type, grant_date"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_equity_grant(conn: sqlite3.Connection, grant_id: int) -> None:
    conn.execute("DELETE FROM equity_transactions WHERE grant_id = ?", (grant_id,))
    conn.execute("DELETE FROM equity_grants WHERE id = ?", (grant_id,))


# ── Equity transactions ───────────────────────────────────────────────────────

def record_equity_transaction(
    conn: sqlite3.Connection,
    grant_id: int | None,
    txn_type: str,
    txn_date: str,
    shares: float,
    price_per_share: float,
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO equity_transactions
            (grant_id, txn_type, txn_date, shares, price_per_share, gross_amount,
             notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (grant_id, txn_type, txn_date, shares, price_per_share,
         round(shares * price_per_share, 2), notes, _now()),
    )
    return cur.lastrowid


def get_equity_transactions(
    conn: sqlite3.Connection,
    grant_id: int | None = None,
    ticker: str | None = None,
) -> list[dict]:
    if grant_id is not None:
        rows = conn.execute(
            "SELECT * FROM equity_transactions WHERE grant_id = ? ORDER BY txn_date",
            (grant_id,),
        ).fetchall()
    elif ticker:
        rows = conn.execute(
            """
            SELECT et.* FROM equity_transactions et
            JOIN equity_grants eg ON eg.id = et.grant_id
            WHERE eg.ticker = ?
            ORDER BY et.txn_date
            """,
            (ticker.upper(),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM equity_transactions ORDER BY txn_date"
        ).fetchall()
    return [dict(r) for r in rows]


def get_total_shares_sold(conn: sqlite3.Connection, ticker: str) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(et.shares), 0) AS total
        FROM equity_transactions et
        JOIN equity_grants eg ON eg.id = et.grant_id
        WHERE eg.ticker = ? AND et.txn_type = 'sell'
        """,
        (ticker.upper(),),
    ).fetchone()
    return float(row["total"])


# ── Stock holdings (direct positions, any source) ────────────────────────────

def add_holding(
    conn: sqlite3.Connection,
    ticker: str,
    shares: float,
    as_of_date: str,
    cost_basis: float | None = None,
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO stock_holdings (ticker, shares, cost_basis, as_of_date, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ticker.upper(), shares, cost_basis, as_of_date, notes, _now()),
    )
    return cur.lastrowid


def update_holding(
    conn: sqlite3.Connection,
    holding_id: int,
    shares: float | None = None,
    cost_basis: float | None = None,
    as_of_date: str | None = None,
    notes: str | None = None,
) -> None:
    fields, values = [], []
    if shares is not None:
        fields.append("shares = ?");     values.append(shares)
    if cost_basis is not None:
        fields.append("cost_basis = ?"); values.append(cost_basis)
    if as_of_date is not None:
        fields.append("as_of_date = ?"); values.append(as_of_date)
    if notes is not None:
        fields.append("notes = ?");      values.append(notes)
    if not fields:
        return
    values.append(holding_id)
    conn.execute(f"UPDATE stock_holdings SET {', '.join(fields)} WHERE id = ?", values)


def get_holdings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM stock_holdings ORDER BY ticker, as_of_date DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_holdings_by_ticker(conn: sqlite3.Connection, ticker: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM stock_holdings WHERE ticker = ? ORDER BY as_of_date DESC",
        (ticker.upper(),),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_holding(conn: sqlite3.Connection, holding_id: int) -> None:
    conn.execute("DELETE FROM stock_holdings WHERE id = ?", (holding_id,))


# ── Manual accounts (401k, IRA, HSA, pension, etc.) ──────────────────────────

ACCOUNT_TYPES = ("401k", "roth_ira", "traditional_ira", "hsa", "pension", "brokerage", "other")


def add_manual_account(
    conn: sqlite3.Connection,
    name: str,
    account_type: str,
    institution: str | None = None,
    balance: float | None = None,
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO manual_accounts
            (name, account_type, institution, balance, balance_updated_at, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (name, account_type, institution, balance,
         _now() if balance is not None else None, notes, _now()),
    )
    return cur.lastrowid


def set_account_balance(
    conn: sqlite3.Connection,
    account_id: int,
    balance: float,
) -> None:
    conn.execute(
        "UPDATE manual_accounts SET balance = ?, balance_updated_at = ? WHERE id = ?",
        (balance, _now(), account_id),
    )


def get_manual_accounts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM manual_accounts ORDER BY account_type, name"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_manual_account(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute("DELETE FROM manual_accounts WHERE id = ?", (account_id,))


# ── Price cache ───────────────────────────────────────────────────────────────

def cache_price(
    conn: sqlite3.Connection, ticker: str, price_date: str, close_price: float
) -> None:
    conn.execute(
        """
        INSERT INTO stock_price_cache (ticker, price_date, close_price, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker, price_date) DO UPDATE SET
            close_price = excluded.close_price,
            fetched_at  = excluded.fetched_at
        """,
        (ticker.upper(), price_date, close_price, _now()),
    )


def get_cached_price(
    conn: sqlite3.Connection, ticker: str, price_date: str
) -> float | None:
    row = conn.execute(
        "SELECT close_price FROM stock_price_cache WHERE ticker = ? AND price_date = ?",
        (ticker.upper(), price_date),
    ).fetchone()
    return float(row["close_price"]) if row else None


def get_cached_range(
    conn: sqlite3.Connection, ticker: str, start_date: str, end_date: str
) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT price_date, close_price FROM stock_price_cache
        WHERE ticker = ? AND price_date BETWEEN ? AND ?
        ORDER BY price_date
        """,
        (ticker.upper(), start_date, end_date),
    ).fetchall()
    return {r["price_date"]: float(r["close_price"]) for r in rows}
