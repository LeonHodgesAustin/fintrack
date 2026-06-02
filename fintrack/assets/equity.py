"""
RSU and ESPP equity tracking.

RSU vesting math
----------------
Calculates vested shares from grant terms (cliff + monthly/quarterly schedule)
without any external data. Sold shares are tracked in equity_transactions.

ESPP purchase price calculation
--------------------------------
Standard "lower of" formula with lookback:

  purchase_price = min(price_at_lookback_start, price_at_purchase_date) * (1 - discount)

Where lookback_start = purchase_date - lookback_months.

For standard Cisco ESPP:
  lookback_months = 24  (offering period, with reset provision)
  discount_rate   = 0.15

IMPORTANT: Verify your exact plan details against your Cisco ESPP plan document
or the E*Trade portal before locking in lookback_months. The standard is 24,
but some plans use 6. The difference is significant when the stock has moved.

Sell detection
--------------
`find_potential_sales()` scans Plaid transactions from linked brokerage accounts
for credits (negative amount = money in) that may represent stock sales.
These are surfaced as candidates for the user to confirm.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from math import floor


@dataclass
class RSUGrant:
    id: int
    ticker: str
    grant_date: date
    total_shares: float
    cliff_months: int       # months until first vest (e.g. 12)
    vest_months: int        # total vesting period in months (e.g. 48)
    vest_frequency: str     # "monthly" | "quarterly" | "annual"


@dataclass
class ESPPPlan:
    id: int
    ticker: str
    offering_start_date: date
    contribution_rate: float      # e.g. 0.10
    discount_rate: float          # e.g. 0.15
    purchase_period_months: int   # e.g. 6
    lookback_months: int          # e.g. 24 — verify against plan docs


# ── RSU math ──────────────────────────────────────────────────────────────────

def _frequency_months(freq: str) -> int:
    return {"monthly": 1, "quarterly": 3, "annual": 12}.get(freq, 1)


def vested_shares(grant: RSUGrant, as_of: date | None = None) -> float:
    """
    Shares vested from this grant as of as_of (defaults to today).

    Schedule:
      - 0 shares until cliff
      - At cliff: cliff tranche vests
      - Then: (total - cliff_shares) / remaining_periods vest each period
    """
    as_of = as_of or date.today()

    # Months elapsed since grant
    months_elapsed = (
        (as_of.year - grant.grant_date.year) * 12
        + (as_of.month - grant.grant_date.month)
    )
    if months_elapsed < grant.cliff_months:
        return 0.0

    freq = _frequency_months(grant.vest_frequency)
    total_periods = (grant.vest_months - grant.cliff_months) // freq

    # Cliff tranche: shares that vest at the cliff date
    cliff_tranche = grant.total_shares * (grant.cliff_months / grant.vest_months)

    # Post-cliff periods completed
    post_cliff_months = months_elapsed - grant.cliff_months
    periods_completed = min(post_cliff_months // freq, total_periods)

    per_period = (grant.total_shares - cliff_tranche) / total_periods if total_periods else 0
    total_vested = cliff_tranche + periods_completed * per_period

    return round(min(total_vested, grant.total_shares), 4)


def unvested_shares(grant: RSUGrant, as_of: date | None = None) -> float:
    return round(grant.total_shares - vested_shares(grant, as_of), 4)


def next_vest_date(grant: RSUGrant, as_of: date | None = None) -> date | None:
    """Next vesting event date, or None if fully vested."""
    as_of = as_of or date.today()
    freq = _frequency_months(grant.vest_frequency)

    months_elapsed = (
        (as_of.year - grant.grant_date.year) * 12
        + (as_of.month - grant.grant_date.month)
    )

    if months_elapsed < grant.cliff_months:
        # Next event is the cliff
        m = grant.grant_date.month + grant.cliff_months
        y = grant.grant_date.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        return date(y, m, grant.grant_date.day)

    if vested_shares(grant, as_of) >= grant.total_shares:
        return None  # fully vested

    # Find next frequency boundary after as_of
    post_cliff = months_elapsed - grant.cliff_months
    next_period = (post_cliff // freq + 1) * freq + grant.cliff_months
    if next_period > grant.vest_months:
        return None

    m = grant.grant_date.month + next_period
    y = grant.grant_date.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    return date(y, m, grant.grant_date.day)


def shares_at_next_vest(grant: RSUGrant, as_of: date | None = None) -> float:
    """Shares vesting at the next event."""
    as_of = as_of or date.today()
    nv = next_vest_date(grant, as_of)
    if nv is None:
        return 0.0
    # Vested on the day of the event vs. day before
    return round(
        vested_shares(grant, nv) - vested_shares(grant, nv - timedelta(days=1)),
        4,
    )


# ── ESPP math ─────────────────────────────────────────────────────────────────

def espp_purchase_price(
    plan: ESPPPlan,
    purchase_date: date,
    conn: sqlite3.Connection | None = None,
) -> float:
    """
    Calculate the ESPP purchase price for a given purchase date.

    purchase_price = min(price_at_lookback_start, price_at_purchase_date)
                     * (1 - discount_rate)

    Fetches historical CSCO prices via yfinance (cached in DB).
    """
    from .prices import price_on_date

    lookback_start = date(
        purchase_date.year - plan.lookback_months // 12,
        purchase_date.month - plan.lookback_months % 12
        if purchase_date.month > plan.lookback_months % 12
        else purchase_date.month - plan.lookback_months % 12 + 12,
        purchase_date.day,
    )
    # Simpler month arithmetic
    total_months = (purchase_date.year * 12 + purchase_date.month) - plan.lookback_months
    lb_year = (total_months - 1) // 12
    lb_month = (total_months - 1) % 12 + 1
    try:
        lookback_start = date(lb_year, lb_month, purchase_date.day)
    except ValueError:
        import calendar
        last_day = calendar.monthrange(lb_year, lb_month)[1]
        lookback_start = date(lb_year, lb_month, last_day)

    price_at_start = price_on_date(plan.ticker, lookback_start.isoformat(), conn)
    price_at_end = price_on_date(plan.ticker, purchase_date.isoformat(), conn)

    lower_price = min(price_at_start, price_at_end)
    purchase_price = round(lower_price * (1 - plan.discount_rate), 4)

    return purchase_price


def espp_shares_purchased(
    plan: ESPPPlan,
    purchase_date: date,
    total_contributions: float,
    conn: sqlite3.Connection | None = None,
) -> tuple[float, float]:
    """
    Calculate shares purchased and purchase price for one ESPP purchase period.

    Returns (shares_purchased, purchase_price_per_share).
    """
    price = espp_purchase_price(plan, purchase_date, conn)
    shares = round(total_contributions / price, 4)
    return shares, price


def estimate_espp_contributions(
    plan: ESPPPlan,
    period_start: date,
    period_end: date,
    conn: sqlite3.Connection,
) -> float:
    """
    Estimate ESPP contributions for a period by looking at payroll deposits
    in the transaction history and applying the contribution rate.

    Payroll is identified as: large negative amounts (credits) with
    'payroll', 'direct dep', 'ach', or 'cisco' in the raw description.
    """
    rows = conn.execute(
        """
        SELECT SUM(ABS(amount)) AS total_payroll
        FROM transactions
        WHERE date BETWEEN ? AND ?
          AND amount < 0
          AND pending = 0
          AND (
            LOWER(COALESCE(raw_name, '')) LIKE '%payroll%'
            OR LOWER(COALESCE(raw_name, '')) LIKE '%direct dep%'
            OR LOWER(COALESCE(raw_name, '')) LIKE '%cisco%'
          )
        """,
        (period_start.isoformat(), period_end.isoformat()),
    ).fetchone()

    total_payroll = float(rows["total_payroll"] or 0)
    return round(total_payroll * plan.contribution_rate, 2)


# ── Sell detection ────────────────────────────────────────────────────────────

def find_potential_sales(
    conn: sqlite3.Connection,
    tickers: list[str],
    lookback_days: int = 30,
    min_amount: float = 500.0,
) -> list[dict]:
    """
    Scan recent brokerage transactions for credits that might be stock sales.

    Matches on:
      - Negative amount (credit = money coming in)
      - Above min_amount
      - From a brokerage account (Schwab, E*Trade, Stash, Fidelity, etc.)
      - OR raw_name contains the company name / ticker

    Returns a list of candidates for the user to review and confirm.
    """
    from datetime import date as _date
    cutoff = (_date.today() - timedelta(days=lookback_days)).isoformat()

    ticker_set = {t.upper() for t in tickers}

    # Company names associated with known tickers — extend as needed
    _ticker_company = {
        "CSCO": "cisco",
        "AAPL": "apple",
        "MSFT": "microsoft",
        "GOOGL": "google",
        "AMZN": "amazon",
        "META": "meta",
        "NVDA": "nvidia",
    }

    rows = conn.execute(
        """
        SELECT
            t.transaction_id,
            t.date,
            ABS(t.amount)                                  AS amount,
            COALESCE(t.merchant_name, t.raw_name, '')      AS name,
            t.raw_name,
            i.institution_name,
            a.name                                         AS account_name,
            t.category_primary
        FROM transactions t
        JOIN accounts a ON a.account_id = t.account_id
        JOIN items    i ON i.item_id    = a.item_id
        WHERE t.date >= ?
          AND t.amount < 0
          AND ABS(t.amount) >= ?
          AND t.pending = 0
          AND (
            LOWER(i.institution_name) LIKE '%schwab%'
            OR LOWER(i.institution_name) LIKE '%etrade%'
            OR LOWER(i.institution_name) LIKE '%fidelity%'
            OR LOWER(i.institution_name) LIKE '%stash%'
            OR LOWER(i.institution_name) LIKE '%robinhood%'
            OR t.category_primary IN ('TRANSFER_IN', 'INCOME')
          )
        ORDER BY t.date DESC
        """,
        (cutoff, min_amount),
    ).fetchall()

    candidates = []
    for r in rows:
        raw = (r["raw_name"] or "").lower()
        name = (r["name"] or "").lower()
        combined = raw + " " + name

        # Check if any tracked ticker or company name appears in the description
        matched_ticker = None
        for ticker in ticker_set:
            if ticker.lower() in combined:
                matched_ticker = ticker
                break
            company = _ticker_company.get(ticker, "").lower()
            if company and company in combined:
                matched_ticker = ticker
                break

        candidates.append({
            "transaction_id": r["transaction_id"],
            "date": r["date"],
            "amount": float(r["amount"]),
            "description": r["name"],
            "institution": r["institution_name"],
            "account": r["account_name"],
            "matched_ticker": matched_ticker,
            "confidence": "high" if matched_ticker else "low",
        })

    return candidates


# ── DB row → dataclass ────────────────────────────────────────────────────────

def rsu_from_db(row: dict) -> RSUGrant:
    return RSUGrant(
        id=row["id"],
        ticker=row["ticker"],
        grant_date=date.fromisoformat(row["grant_date"]),
        total_shares=row["total_shares"],
        cliff_months=row["cliff_months"],
        vest_months=row["vest_months"],
        vest_frequency=row["vest_frequency"] or "monthly",
    )


def espp_from_db(row: dict) -> ESPPPlan:
    return ESPPPlan(
        id=row["id"],
        ticker=row["ticker"],
        offering_start_date=date.fromisoformat(row["grant_date"]),
        contribution_rate=row["contribution_rate"],
        discount_rate=row["discount_rate"] or 0.15,
        purchase_period_months=row["purchase_period_months"] or 6,
        lookback_months=row["lookback_months"] or 24,
    )
