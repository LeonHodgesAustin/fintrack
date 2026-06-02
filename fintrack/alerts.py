"""
Alert delivery and financial health checks for fintrack.

Delivery backends (configured via .env):
  ntfy.sh  -- NTFY_TOPIC + optional NTFY_SERVER (default: https://ntfy.sh)
  Log file -- always written to alerts.log in the project root

All alert checks return list[dict] so the CLI can display them even without
ntfy configured. The send_alert() function handles delivery independently.

ntfy.sh priority mapping:
  info    -> low      (blue bell on phone)
  warning -> default  (standard notification)
  urgent  -> high     (loud, bypasses do-not-disturb)

ntfy.sh setup: just pick any topic name (your-fintrack-abc123), subscribe in
the ntfy app on your phone, set NTFY_TOPIC in .env. No account needed.
"""

import sqlite3
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


_LOG_FILE = Path("alerts.log")

# ntfy priority strings
P_INFO    = "low"
P_WARNING = "default"
P_URGENT  = "high"


# -- Delivery ------------------------------------------------------------------

def send_alert(
    title: str,
    body: str,
    priority: str = P_WARNING,
    ntfy_topic: str = "",
    ntfy_server: str = "https://ntfy.sh",
    conn: sqlite3.Connection | None = None,
) -> bool:
    """
    Log the alert and optionally push via ntfy.sh.

    Returns True if ntfy delivery succeeded (or no topic configured),
    False if ntfy was configured but delivery failed.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    full_body = f"[{timestamp}]\n{body}"

    # Always write to log
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"\n{'=' * 60}\n[{timestamp}] [{priority.upper()}] {title}\n{body}\n")
    except OSError as err:
        print(f"  [WARN] Could not write alerts.log: {err}")

    # Log to DB if connection provided
    delivered = False
    if conn is not None:
        from .db import log_alert
        log_alert(conn, title, body, delivered=False)

    if not ntfy_topic:
        return True  # no ntfy configured, log-only is fine

    url = f"{ntfy_server.rstrip('/')}/{ntfy_topic}"
    data = full_body.encode("utf-8")
    headers = {
        "Title":    title,
        "Priority": priority,
        "Tags":     "money_with_wings",
        "Content-Type": "text/plain",
    }

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            delivered = True
        if conn is not None:
            conn.execute(
                "UPDATE alert_log SET delivered = 1 WHERE id = (SELECT MAX(id) FROM alert_log)"
            )
            conn.commit()
        return True
    except (urllib.error.URLError, OSError) as err:
        print(f"  [WARN] ntfy.sh delivery failed: {err}")
        return False


def send_batch(
    alerts: list[dict],
    ntfy_topic: str = "",
    ntfy_server: str = "https://ntfy.sh",
    conn: sqlite3.Connection | None = None,
) -> int:
    """
    Send a list of alert dicts (each with 'title', 'body', 'priority').
    Returns count of successfully delivered alerts.
    """
    sent = 0
    for alert in alerts:
        ok = send_alert(
            title=alert["title"],
            body=alert["body"],
            priority=alert.get("priority", P_WARNING),
            ntfy_topic=ntfy_topic,
            ntfy_server=ntfy_server,
            conn=conn,
        )
        if ok:
            sent += 1
    return sent


# -- Alert checks --------------------------------------------------------------

def check_large_transactions(
    conn: sqlite3.Connection,
    threshold: float = 500.0,
    lookback_days: int = 1,
) -> list[dict]:
    """
    Transactions above threshold posted in the last lookback_days days.
    Excludes transfers (usually payroll, rent) to reduce noise -- those
    are large by design and shouldn't trigger surprise alerts.
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()

    rows = conn.execute(
        """
        SELECT
            t.transaction_id,
            t.date,
            t.amount,
            COALESCE(t.merchant_name, t.raw_name, 'Unknown') AS merchant,
            t.category_primary,
            i.institution_name
        FROM transactions t
        JOIN accounts a ON a.account_id = t.account_id
        JOIN items    i ON i.item_id    = a.item_id
        WHERE t.date >= ?
          AND t.amount >= ?
          AND t.pending = 0
          AND t.category_primary NOT IN ('TRANSFER_IN', 'TRANSFER_OUT')
        ORDER BY t.amount DESC
        """,
        (cutoff, threshold),
    ).fetchall()

    alerts = []
    for r in rows:
        alerts.append({
            "title":    f"Large transaction: ${r['amount']:,.2f} at {r['merchant']}",
            "body":     (
                f"Amount:      ${r['amount']:,.2f}\n"
                f"Merchant:    {r['merchant']}\n"
                f"Category:    {r['category_primary'] or 'Unknown'}\n"
                f"Date:        {r['date']}\n"
                f"Account:     {r['institution_name']}"
            ),
            "priority": P_URGENT,
            "type":     "large_transaction",
            "data":     dict(r),
        })
    return alerts


def check_spending_spikes(
    conn: sqlite3.Connection,
    spike_pct: float = 50.0,
    min_baseline: float = 20.0,
) -> list[dict]:
    """
    Categories where this month's spend is more than spike_pct% above last
    month's spend, as long as the baseline is >= min_baseline (avoids noise
    from tiny categories going from $2 to $3).

    Excludes transfer categories.
    """
    today = date.today()
    this_month  = today.month
    this_year   = today.year
    last_month  = 12 if this_month == 1 else this_month - 1
    last_year   = today.year - 1 if this_month == 1 else today.year

    def month_spend_by_cat(year, month) -> dict[str, float]:
        from .reports import monthly_summary
        rows = monthly_summary(conn, year, month)
        return {r["category"]: r["total_amount"] for r in rows
                if r["category"] not in ("TRANSFER_IN", "TRANSFER_OUT")}

    this_spend = month_spend_by_cat(this_year, this_month)
    last_spend = month_spend_by_cat(last_year, last_month)

    alerts = []
    for cat, current in this_spend.items():
        baseline = last_spend.get(cat, 0.0)
        if baseline < min_baseline:
            continue
        pct_change = (current - baseline) / baseline * 100
        if pct_change >= spike_pct:
            alerts.append({
                "title":    f"Spending spike: {cat} up {pct_change:.0f}%",
                "body":     (
                    f"Category:    {cat}\n"
                    f"This month:  ${current:,.2f}\n"
                    f"Last month:  ${baseline:,.2f}\n"
                    f"Change:      +{pct_change:.1f}%"
                ),
                "priority": P_WARNING,
                "type":     "spending_spike",
                "data":     {"category": cat, "current": current,
                             "baseline": baseline, "pct_change": pct_change},
            })
    return alerts


def check_upcoming_recurring(
    charges: list,
    days: int = 7,
) -> list[dict]:
    """Recurring charges due within the next N days."""
    from .recurring import get_upcoming
    upcoming = get_upcoming(charges, days=days)
    alerts = []
    for c in upcoming:
        days_left = c.days_until()
        label = "today" if days_left == 0 else f"in {days_left} day(s)"
        alerts.append({
            "title":    f"Upcoming: {c.merchant} ${c.expected_amount:.2f} due {label}",
            "body":     (
                f"Merchant:    {c.merchant}\n"
                f"Amount:      ${c.expected_amount:.2f}\n"
                f"Due:         {c.next_expected} ({label})\n"
                f"Source:      {c.source}"
            ),
            "priority": P_INFO,
            "type":     "upcoming_recurring",
            "data":     {"merchant": c.merchant, "amount": c.expected_amount,
                         "due": c.next_expected.isoformat()},
        })
    return alerts


def check_missing_recurring(
    charges: list,
    conn: sqlite3.Connection,
    grace_days: int = 5,
) -> list[dict]:
    """Expected recurring charges that haven't appeared this month."""
    from .recurring import get_missing
    missing = get_missing(charges, conn, grace_days=grace_days)
    alerts = []
    for c in missing:
        alerts.append({
            "title":    f"Missing charge: {c.merchant} expected ${c.expected_amount:.2f}",
            "body":     (
                f"Merchant:    {c.merchant}\n"
                f"Expected:    ${c.expected_amount:.2f} around {c.next_expected}\n"
                f"Status:      Not seen within {grace_days} days of expected date\n"
                f"Action:      Check if subscription was cancelled or payment failed"
            ),
            "priority": P_WARNING,
            "type":     "missing_recurring",
            "data":     {"merchant": c.merchant, "amount": c.expected_amount,
                         "expected": c.next_expected.isoformat()},
        })
    return alerts


def check_auth_errors(conn: sqlite3.Connection) -> list[dict]:
    """Linked institutions with error_state set (require re-authentication)."""
    rows = conn.execute(
        "SELECT item_id, institution_name, error_state FROM items WHERE error_state IS NOT NULL"
    ).fetchall()
    alerts = []
    for r in rows:
        alerts.append({
            "title":    f"Re-auth required: {r['institution_name']}",
            "body":     (
                f"Institution: {r['institution_name']}\n"
                f"Error:       {r['error_state']}\n"
                f"Fix:         fintrack reauth --item {r['item_id']}"
            ),
            "priority": P_URGENT,
            "type":     "auth_error",
            "data":     dict(r),
        })
    return alerts


def run_all_checks(
    conn: sqlite3.Connection,
    recurring_charges: list,
    large_txn_threshold: float = 500.0,
    large_txn_lookback_days: int = 1,
    spending_spike_pct: float = 50.0,
    upcoming_days: int = 7,
    missing_grace_days: int = 5,
) -> list[dict]:
    """Run all alert checks and return the combined list."""
    alerts = []
    alerts.extend(check_auth_errors(conn))
    alerts.extend(check_large_transactions(conn, large_txn_threshold, large_txn_lookback_days))
    alerts.extend(check_spending_spikes(conn, spending_spike_pct))
    alerts.extend(check_upcoming_recurring(recurring_charges, upcoming_days))
    alerts.extend(check_missing_recurring(recurring_charges, conn, missing_grace_days))
    return alerts
