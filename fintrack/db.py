import sqlite3
from datetime import datetime, timezone


# -- Encryption (optional) -----------------------------------------------------
# Call configure_encryption(settings.fernet_key) once at startup.
# If no key is configured, tokens are stored as plaintext.
# Decrypt silently falls back to plaintext to handle pre-encryption tokens.

_fernet_key: str = ""


def configure_encryption(key: str) -> None:
    global _fernet_key
    _fernet_key = key


def _encrypt(value: str) -> str:
    if not _fernet_key:
        return value
    from cryptography.fernet import Fernet
    return Fernet(_fernet_key.encode()).encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    if not _fernet_key:
        return value
    from cryptography.fernet import Fernet
    try:
        return Fernet(_fernet_key.encode()).decrypt(value.encode()).decode()
    except Exception:
        return value  # plaintext token from before encryption was enabled


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
                last_synced      TEXT,
                error_state      TEXT
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
            CREATE INDEX IF NOT EXISTS idx_txn_source      ON transactions(category_source);
            CREATE INDEX IF NOT EXISTS idx_txn_confidence  ON transactions(category_confidence);

            CREATE TABLE IF NOT EXISTS transaction_overrides (
                transaction_id  TEXT PRIMARY KEY,
                category        TEXT NOT NULL,
                subcategory     TEXT NOT NULL DEFAULT '',
                note            TEXT,
                overridden_at   TEXT NOT NULL,
                override_source TEXT NOT NULL DEFAULT 'cli'
            );

            CREATE TABLE IF NOT EXISTS recurring_excludes (
                merchant_pattern TEXT PRIMARY KEY,
                added_at         TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS alert_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type  TEXT NOT NULL,
                message     TEXT NOT NULL,
                sent_at     TEXT NOT NULL,
                delivered   INTEGER NOT NULL DEFAULT 0
            );

            -- Saved budget normalizations: annual-to-monthly conversions,
            -- known one-time items, etc.  These auto-apply on every budget run
            -- so you do not have to re-type --also flags each time.
            CREATE TABLE IF NOT EXISTS budget_adjustments (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                label          TEXT NOT NULL,
                monthly_amount REAL NOT NULL,
                category       TEXT,
                notes          TEXT,
                active         INTEGER NOT NULL DEFAULT 1,
                created_at     TEXT NOT NULL DEFAULT (date('now'))
            );

            -- Out-of-band expense flags: one-time, reimbursable, gift, etc.
            -- flag_type is validated at the application layer (FLAG_TYPES constant)
            -- and by the CHECK constraint here as a second line of defence.
            CREATE TABLE IF NOT EXISTS transaction_flags (
                flag_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id TEXT    NOT NULL REFERENCES transactions(transaction_id) ON DELETE CASCADE,
                flag_type      TEXT    NOT NULL CHECK(flag_type IN ('one-time','reimbursable','gift','transfer','other')),
                note           TEXT,
                created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_flag_txn ON transaction_flags(transaction_id);

            -- Per-category spending targets shown alongside actuals.
            CREATE TABLE IF NOT EXISTS budget_targets (
                category      TEXT PRIMARY KEY,
                target_amount REAL NOT NULL,
                notes         TEXT,
                updated_at    TEXT NOT NULL DEFAULT (date('now'))
            );

            -- One row per account per sync run.  Provides the time-series
            -- needed for net worth trending without calling /accounts/get
            -- separately -- the balances come from the /transactions/sync
            -- response which already includes account data.
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                snapshot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id        TEXT    NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                captured_at       TEXT    NOT NULL,
                current_balance   REAL,
                available_balance REAL,
                limit_amount      REAL,
                iso_currency_code TEXT    NOT NULL DEFAULT 'USD'
            );

            CREATE INDEX IF NOT EXISTS idx_snap_account ON balance_snapshots(account_id);
            CREATE INDEX IF NOT EXISTS idx_snap_date    ON balance_snapshots(captured_at);

            -- Tax-prep: tag a transaction with a tax category (medical, charitable, etc.)
            -- Tax year defaults to the calendar year of the transaction date.
            CREATE TABLE IF NOT EXISTS tax_tags (
                tag_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id TEXT    NOT NULL REFERENCES transactions(transaction_id) ON DELETE CASCADE,
                tax_category   TEXT    NOT NULL,
                note           TEXT,
                tax_year       INTEGER NOT NULL,
                created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_tax_tag_txn  ON tax_tags(transaction_id);
            CREATE INDEX IF NOT EXISTS idx_tax_tag_year ON tax_tags(tax_year);
            CREATE INDEX IF NOT EXISTS idx_tax_tag_cat  ON tax_tags(tax_category);

            -- Tax-prep: track expected and received tax documents per year.
            CREATE TABLE IF NOT EXISTS tax_documents (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                year          INTEGER NOT NULL,
                institution   TEXT    NOT NULL,
                doc_type      TEXT    NOT NULL,
                received      INTEGER NOT NULL DEFAULT 0,
                received_date TEXT,
                notes         TEXT,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(year, institution, doc_type)
            );

            CREATE INDEX IF NOT EXISTS idx_tax_doc_year ON tax_documents(year);

            -- Tax-prep: key/value store for static reference info (EIN, prior-year AGI, etc.)
            -- WARNING: do NOT store full SSNs or full account numbers here.
            -- Last 4 digits only is acceptable for identification purposes.
            CREATE TABLE IF NOT EXISTS tax_info (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                notes      TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- One row per hour-bucket showing aggregated asset/liability totals.
            -- Rounds each snapshot's captured_at to the nearest hour so that a
            -- single sync run (all accounts captured within seconds) collapses to
            -- one net-worth data point rather than N per-account points.
            CREATE VIEW IF NOT EXISTS net_worth_snapshots AS
            SELECT
                strftime('%Y-%m-%dT%H:00:00', captured_at)       AS snapshot_hour,
                SUM(CASE WHEN COALESCE(a.type, '') NOT IN ('credit', 'loan')
                    THEN COALESCE(s.current_balance, 0) ELSE 0 END) AS total_assets,
                SUM(CASE WHEN COALESCE(a.type, '') IN ('credit', 'loan')
                    THEN COALESCE(s.current_balance, 0) ELSE 0 END) AS total_liabilities,
                SUM(CASE WHEN COALESCE(a.type, '') NOT IN ('credit', 'loan')
                    THEN COALESCE(s.current_balance, 0) ELSE 0 END) -
                SUM(CASE WHEN COALESCE(a.type, '') IN ('credit', 'loan')
                    THEN COALESCE(s.current_balance, 0) ELSE 0 END) AS net_worth
            FROM balance_snapshots s
            JOIN accounts a ON a.account_id = s.account_id
            GROUP BY snapshot_hour
            ORDER BY snapshot_hour;
        """)
        conn.commit()

        # Additive migrations for existing DBs
        for stmt in [
            "ALTER TABLE items ADD COLUMN error_state TEXT",
            "ALTER TABLE tax_documents ADD COLUMN file_path TEXT",
        ]:
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

    finally:
        conn.close()


# -- Item helpers --------------------------------------------------------------

def _decrypt_item(row: dict) -> dict:
    row["access_token"] = _decrypt(row["access_token"])
    return row


def insert_item(conn, item_id, access_token, institution_name):
    conn.execute(
        """
        INSERT INTO items (item_id, access_token, institution_name, cursor, last_synced)
        VALUES (?, ?, ?, NULL, NULL)
        ON CONFLICT(item_id) DO UPDATE SET
            access_token = excluded.access_token,
            institution_name = excluded.institution_name
        """,
        (item_id, _encrypt(access_token), institution_name),
    )


def update_item_cursor(conn, item_id, cursor):
    conn.execute(
        "UPDATE items SET cursor = ?, last_synced = ? WHERE item_id = ?",
        (cursor, datetime.now(timezone.utc).isoformat(), item_id),
    )


def get_all_items(conn):
    rows = conn.execute("SELECT * FROM items").fetchall()
    return [_decrypt_item(dict(r)) for r in rows]


def get_item(conn, item_id):
    row = conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
    return _decrypt_item(dict(row)) if row else None


def update_item_access_token(conn, item_id, access_token):
    conn.execute(
        "UPDATE items SET access_token = ? WHERE item_id = ?",
        (_encrypt(access_token), item_id),
    )


def set_item_error(conn, item_id, error_code):
    conn.execute("UPDATE items SET error_state = ? WHERE item_id = ?", (error_code, item_id))


def clear_item_error(conn, item_id):
    conn.execute("UPDATE items SET error_state = NULL WHERE item_id = ?", (item_id,))


# -- Account helpers ----------------------------------------------------------

def upsert_account(conn, account_id, item_id, name, account_type, subtype):
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


def get_accounts_for_item(conn, item_id):
    rows = conn.execute("SELECT * FROM accounts WHERE item_id = ?", (item_id,)).fetchall()
    return [dict(r) for r in rows]


# -- Transaction helpers ------------------------------------------------------

def upsert_transaction(
    conn, transaction_id, account_id, date, amount,
    merchant_name, raw_name, category_primary, category_detailed,
    category_confidence, category_source, pending, raw_json,
):
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


def delete_transaction(conn, transaction_id):
    conn.execute("DELETE FROM transactions WHERE transaction_id = ?", (transaction_id,))


# -- Override helpers ---------------------------------------------------------

def get_all_overrides(conn) -> dict:
    """Return {transaction_id: {category, subcategory, note, source}} for all overrides."""
    rows = conn.execute("SELECT * FROM transaction_overrides").fetchall()
    return {r["transaction_id"]: dict(r) for r in rows}


def get_override(conn, transaction_id) -> dict | None:
    row = conn.execute(
        "SELECT * FROM transaction_overrides WHERE transaction_id = ?", (transaction_id,)
    ).fetchone()
    return dict(row) if row else None


def set_override(conn, transaction_id, category, subcategory="", note=None, source="cli"):
    conn.execute(
        """
        INSERT INTO transaction_overrides
            (transaction_id, category, subcategory, note, overridden_at, override_source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(transaction_id) DO UPDATE SET
            category        = excluded.category,
            subcategory     = excluded.subcategory,
            note            = excluded.note,
            overridden_at   = excluded.overridden_at,
            override_source = excluded.override_source
        """,
        (transaction_id, category, subcategory, note, datetime.now(timezone.utc).isoformat(), source),
    )
    # Also update the live transaction record so reports reflect the override immediately
    conn.execute(
        """
        UPDATE transactions
        SET category_primary = ?, category_detailed = ?, category_source = 'override'
        WHERE transaction_id = ?
        """,
        (category, subcategory, transaction_id),
    )


def delete_override(conn, transaction_id):
    conn.execute(
        "DELETE FROM transaction_overrides WHERE transaction_id = ?", (transaction_id,)
    )


def apply_overrides_to_transactions(conn) -> int:
    """
    Re-apply all stored overrides to the transactions table.
    Useful after a sync that may have reset category fields.
    Returns number of overrides applied.
    """
    overrides = get_all_overrides(conn)
    count = 0
    for txn_id, ov in overrides.items():
        conn.execute(
            """
            UPDATE transactions
            SET category_primary = ?, category_detailed = ?, category_source = 'override'
            WHERE transaction_id = ?
            """,
            (ov["category"], ov["subcategory"], txn_id),
        )
        count += 1
    return count


# -- Recurring exclude helpers ------------------------------------------------

def get_recurring_excludes(conn) -> set:
    rows = conn.execute("SELECT merchant_pattern FROM recurring_excludes").fetchall()
    return {r["merchant_pattern"].lower() for r in rows}


def add_recurring_exclude(conn, pattern):
    conn.execute(
        "INSERT OR REPLACE INTO recurring_excludes (merchant_pattern, added_at) VALUES (?, ?)",
        (pattern.lower(), datetime.now(timezone.utc).isoformat()),
    )


def remove_recurring_exclude(conn, pattern):
    conn.execute(
        "DELETE FROM recurring_excludes WHERE merchant_pattern = ?", (pattern.lower(),)
    )


# -- Alert log helpers --------------------------------------------------------

def log_alert(conn, alert_type, message, delivered=False):
    conn.execute(
        "INSERT INTO alert_log (alert_type, message, sent_at, delivered) VALUES (?, ?, ?, ?)",
        (alert_type, message, datetime.now(timezone.utc).isoformat(), 1 if delivered else 0),
    )


# -- Balance snapshot helpers -------------------------------------------------

def insert_balance_snapshot(
    conn,
    account_id: str,
    captured_at: str,
    current_balance: float | None,
    available_balance: float | None,
    limit_amount: float | None,
    iso_currency_code: str = "USD",
) -> None:
    conn.execute(
        """
        INSERT INTO balance_snapshots
            (account_id, captured_at, current_balance, available_balance,
             limit_amount, iso_currency_code)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (account_id, captured_at, current_balance, available_balance,
         limit_amount, iso_currency_code),
    )


# -- Budget adjustment helpers ------------------------------------------------

def add_budget_adjustment(
    conn,
    label: str,
    monthly_amount: float,
    category: str | None = None,
    notes: str | None = None,
) -> int:
    """
    Save a named normalization adjustment.
    monthly_amount < 0 reduces the expense total (e.g. -1000 for "annual item ÷12").
    Returns the new row id.
    """
    cur = conn.execute(
        """
        INSERT INTO budget_adjustments (label, monthly_amount, category, notes)
        VALUES (?, ?, ?, ?)
        """,
        (label, monthly_amount, category, notes),
    )
    return cur.lastrowid


def get_budget_adjustments(conn, active_only: bool = True) -> list[dict]:
    where = "WHERE active = 1" if active_only else ""
    rows = conn.execute(
        f"SELECT * FROM budget_adjustments {where} ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def remove_budget_adjustment(conn, adjustment_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM budget_adjustments WHERE id = ?", (adjustment_id,)
    )
    return cur.rowcount > 0


def toggle_budget_adjustment(conn, adjustment_id: int, active: bool) -> bool:
    cur = conn.execute(
        "UPDATE budget_adjustments SET active = ? WHERE id = ?",
        (1 if active else 0, adjustment_id),
    )
    return cur.rowcount > 0


# -- Budget target helpers ----------------------------------------------------

def set_budget_target(
    conn,
    category: str,
    target_amount: float,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO budget_targets (category, target_amount, notes, updated_at)
        VALUES (?, ?, ?, date('now'))
        ON CONFLICT(category) DO UPDATE SET
            target_amount = excluded.target_amount,
            notes         = excluded.notes,
            updated_at    = excluded.updated_at
        """,
        (category, target_amount, notes),
    )


def get_budget_targets(conn) -> dict[str, dict]:
    """Return {category: {target_amount, notes, updated_at}} for all targets."""
    rows = conn.execute(
        "SELECT * FROM budget_targets ORDER BY category"
    ).fetchall()
    return {r["category"]: dict(r) for r in rows}


def remove_budget_target(conn, category: str) -> bool:
    cur = conn.execute(
        "DELETE FROM budget_targets WHERE category = ?", (category,)
    )
    return cur.rowcount > 0


# -- Transaction flag helpers -------------------------------------------------

FLAG_TYPES: tuple[str, ...] = ("one-time", "reimbursable", "gift", "transfer", "other")


def add_flag(
    conn,
    transaction_id: str,
    flag_type: str,
    note: str | None = None,
) -> int:
    """Flag a transaction. Returns the new flag_id."""
    cur = conn.execute(
        """
        INSERT INTO transaction_flags (transaction_id, flag_type, note)
        VALUES (?, ?, ?)
        """,
        (transaction_id, flag_type, note),
    )
    return cur.lastrowid


def remove_flag(conn, flag_id: int) -> bool:
    cur = conn.execute("DELETE FROM transaction_flags WHERE flag_id = ?", (flag_id,))
    return cur.rowcount > 0


TAX_CATEGORIES: tuple[str, ...] = (
    "medical",         # medical/dental/vision, prescriptions, health insurance premiums
    "hsa_fsa",         # HSA/FSA contributions and qualified expenses paid from those accounts
    "charitable",      # charitable donations (cash, goods, or mileage)
    "dependent_care",  # childcare, summer camp, before/after-school (Form 2441)
    "education",       # tuition, course fees, books, 529 contributions, student loan interest
    "self_employed",   # Schedule C expenses: 1099/freelance/side-work income & business costs
    "home_office",     # home office deduction (primarily for self-employed; see tax_notes.md)
    "energy_credit",   # energy-efficient improvements: heat pump, solar, EV charger, insulation
    "mortgage_interest", # mortgage interest paid (Form 1098 deduction if itemizing)
    "investment",      # investment advisory fees, related deductible expenses
    "alimony_paid",    # alimony paid (deductible only for divorces finalized before 2019)
    "state_local_tax", # state/local income or property tax payments (SALT deduction)
    "estimated_tax",   # federal or state quarterly estimated tax payments made
    "other",           # anything else flagged for tax review
)

TAX_DOC_TYPES: tuple[str, ...] = (
    "W-2",
    "1099-INT",
    "1099-DIV",
    "1099-B",
    "1099-NEC",
    "1099-MISC",
    "1099-R",
    "1099-SA",   # HSA distributions
    "1098",
    "1098-E",
    "1098-T",    # tuition statement
    "SSA-1099",
    "other",
)


def get_all_flags(conn) -> list[dict]:
    """Return all flags joined with transaction data, newest-first by transaction date."""
    rows = conn.execute(
        """
        SELECT
            f.flag_id,
            f.transaction_id,
            f.flag_type,
            f.note,
            f.created_at,
            t.date,
            t.amount,
            COALESCE(t.merchant_name, t.raw_name, 'Unknown') AS merchant,
            t.category_primary
        FROM transaction_flags f
        JOIN transactions t ON t.transaction_id = f.transaction_id
        ORDER BY t.date DESC, f.created_at DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


# -- Tax tag helpers ----------------------------------------------------------

def add_tax_tag(
    conn,
    transaction_id: str,
    tax_category: str,
    tax_year: int,
    note: str | None = None,
) -> int:
    """Tag a transaction with a tax category. Returns the new tag_id."""
    cur = conn.execute(
        """
        INSERT INTO tax_tags (transaction_id, tax_category, tax_year, note)
        VALUES (?, ?, ?, ?)
        """,
        (transaction_id, tax_category, tax_year, note),
    )
    return cur.lastrowid


def remove_tax_tag(conn, tag_id: int) -> bool:
    cur = conn.execute("DELETE FROM tax_tags WHERE tag_id = ?", (tag_id,))
    return cur.rowcount > 0


def get_tax_tags(conn, year: int | None = None, tax_category: str | None = None) -> list[dict]:
    """Return tax-tagged transactions, joined with transaction data.

    Filters by year and/or category when provided.
    """
    filters = []
    params: list = []
    if year is not None:
        filters.append("tt.tax_year = ?")
        params.append(year)
    if tax_category is not None:
        filters.append("tt.tax_category = ?")
        params.append(tax_category)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    rows = conn.execute(
        f"""
        SELECT
            tt.tag_id,
            tt.transaction_id,
            tt.tax_category,
            tt.tax_year,
            tt.note,
            tt.created_at,
            t.date,
            t.amount,
            COALESCE(t.merchant_name, t.raw_name, 'Unknown') AS merchant,
            t.category_primary
        FROM tax_tags tt
        JOIN transactions t ON t.transaction_id = tt.transaction_id
        {where}
        ORDER BY tt.tax_year DESC, t.date DESC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


# -- Tax document helpers -----------------------------------------------------

def add_tax_document(
    conn,
    year: int,
    institution: str,
    doc_type: str,
    notes: str | None = None,
) -> int | None:
    """Insert a new expected document. Returns tag_id, or None if already exists."""
    try:
        cur = conn.execute(
            """
            INSERT INTO tax_documents (year, institution, doc_type, notes)
            VALUES (?, ?, ?, ?)
            """,
            (year, institution, doc_type, notes),
        )
        return cur.lastrowid
    except Exception:
        return None


def upsert_tax_document(
    conn,
    year: int,
    institution: str,
    doc_type: str,
    notes: str | None = None,
) -> int:
    """Insert or update a tax document entry. Returns the row id."""
    conn.execute(
        """
        INSERT INTO tax_documents (year, institution, doc_type, notes)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(year, institution, doc_type) DO UPDATE SET
            notes = COALESCE(excluded.notes, notes)
        """,
        (year, institution, doc_type, notes),
    )
    row = conn.execute(
        "SELECT id FROM tax_documents WHERE year=? AND institution=? AND doc_type=?",
        (year, institution, doc_type),
    ).fetchone()
    return row["id"]


def mark_tax_document_received(
    conn,
    doc_id: int,
    received: bool,
    received_date: str | None = None,
) -> bool:
    cur = conn.execute(
        "UPDATE tax_documents SET received=?, received_date=? WHERE id=?",
        (1 if received else 0, received_date, doc_id),
    )
    return cur.rowcount > 0


def get_tax_documents(conn, year: int | None = None) -> list[dict]:
    where = "WHERE year = ?" if year is not None else ""
    params = [year] if year is not None else []
    rows = conn.execute(
        f"""
        SELECT id, year, institution, doc_type, received, received_date,
               notes, file_path, created_at
        FROM tax_documents
        {where}
        ORDER BY year DESC, institution, doc_type
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def set_tax_document_file_path(conn, doc_id: int, file_path: str | None) -> bool:
    cur = conn.execute(
        "UPDATE tax_documents SET file_path = ? WHERE id = ?", (file_path, doc_id)
    )
    return cur.rowcount > 0


def get_tax_document(conn, doc_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM tax_documents WHERE id = ?", (doc_id,)
    ).fetchone()
    return dict(row) if row else None


def delete_tax_document(conn, doc_id: int) -> bool:
    cur = conn.execute("DELETE FROM tax_documents WHERE id = ?", (doc_id,))
    return cur.rowcount > 0


# -- Tax info helpers ---------------------------------------------------------

def set_tax_info(conn, key: str, value: str, notes: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO tax_info (key, value, notes, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            value      = excluded.value,
            notes      = COALESCE(excluded.notes, notes),
            updated_at = excluded.updated_at
        """,
        (key, value, notes),
    )


def get_tax_info(conn, key: str) -> dict | None:
    row = conn.execute("SELECT * FROM tax_info WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def get_all_tax_info(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT key, value, notes, updated_at FROM tax_info ORDER BY key"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_tax_info(conn, key: str) -> bool:
    cur = conn.execute("DELETE FROM tax_info WHERE key = ?", (key,))
    return cur.rowcount > 0
