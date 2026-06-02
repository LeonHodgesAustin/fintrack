"""
Recurring expense detection and management for fintrack.

Two sources of recurring charges are merged:
  1. Auto-detected: merchants that appear at roughly monthly intervals in the
     transaction history, detected via autocorrelation / gap analysis.
  2. Manual: defined in RECURRING_EXPENSES config (name|amount|day-of-month).

An exclude list (RECURRING_EXCLUDE_MERCHANTS config + DB table) suppresses
known false positives from the auto-detector (e.g. your payroll direct deposit
showing up as a "recurring charge").

Detection algorithm:
  For each merchant with >= min_occurrences transactions in the lookback window:
    - Compute gaps between consecutive transaction dates.
    - If the median gap is within the monthly range (25-35 days), classify as
      monthly recurring and estimate the expected day-of-month.
    - Amount is checked for stability: stddev / mean < amount_cv_threshold.
"""

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional


@dataclass
class RecurringCharge:
    merchant: str
    expected_amount: float
    expected_day: int           # day of month (1-28)
    last_seen: Optional[date]
    next_expected: date
    frequency: str              # "monthly" currently; "annual" planned
    source: str                 # "auto" | "manual"
    confidence: float           # 0-1, for auto-detected charges

    def days_until(self) -> int:
        return (self.next_expected - date.today()).days

    def is_overdue(self, grace_days: int = 5) -> bool:
        return date.today() > self.next_expected + timedelta(days=grace_days)


def _next_occurrence(day_of_month: int) -> date:
    """Return the next future date with the given day-of-month."""
    today = date.today()
    # Try this month first
    try:
        candidate = today.replace(day=day_of_month)
    except ValueError:
        # Day doesn't exist this month (e.g. Feb 30) -- use last day
        import calendar
        last = calendar.monthrange(today.year, today.month)[1]
        candidate = today.replace(day=last)

    if candidate > today:
        return candidate

    # Move to next month
    if today.month == 12:
        return date(today.year + 1, 1, min(day_of_month, 28))
    else:
        next_month = today.month + 1
        import calendar
        last = calendar.monthrange(today.year, next_month)[1]
        return date(today.year, next_month, min(day_of_month, last))


def detect_recurring(
    conn: sqlite3.Connection,
    lookback_days: int = 180,
    min_occurrences: int = 2,
    window_days: int = 5,
    amount_cv_threshold: float = 0.20,
    exclude_merchants: Optional[set] = None,
    min_amount: float = 1.0,
) -> list[RecurringCharge]:
    """
    Auto-detect recurring monthly charges from transaction history.

    Parameters
    ----------
    lookback_days       : how far back to look for transaction history
    min_occurrences     : minimum times merchant must appear to be considered
    window_days         : tolerance around expected day-of-month (days)
    amount_cv_threshold : max coefficient of variation for amount stability
                          (stddev/mean -- 0.20 means <20% variance)
    exclude_merchants   : set of lowercase merchant names to skip
    min_amount          : ignore transactions below this amount
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    exclude = {m.lower() for m in (exclude_merchants or set())}

    rows = conn.execute(
        """
        SELECT
            COALESCE(merchant_name, raw_name) AS merchant,
            date,
            amount
        FROM transactions
        WHERE date >= ?
          AND amount > ?
          AND pending = 0
          AND category_primary NOT IN ('TRANSFER_IN', 'TRANSFER_OUT', 'INCOME')
        ORDER BY merchant, date
        """,
        (cutoff, min_amount),
    ).fetchall()

    # Group by merchant
    by_merchant: dict[str, list[dict]] = {}
    for r in rows:
        m = r["merchant"]
        if not m:
            continue
        if m.lower() in exclude:
            continue
        by_merchant.setdefault(m, []).append({"date": r["date"], "amount": r["amount"]})

    results: list[RecurringCharge] = []

    for merchant, txns in by_merchant.items():
        if len(txns) < min_occurrences:
            continue

        dates = sorted(date.fromisoformat(t["date"]) for t in txns)
        amounts = [t["amount"] for t in txns]

        # Compute gaps between consecutive dates
        if len(dates) < 2:
            continue
        gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        median_gap = statistics.median(gaps)

        # Monthly: gap 25-40 days
        if not (25 <= median_gap <= 40):
            continue

        # Amount stability check
        mean_amt = statistics.mean(amounts)
        if mean_amt == 0:
            continue
        if len(amounts) > 1:
            cv = statistics.stdev(amounts) / mean_amt
        else:
            cv = 0.0
        if cv > amount_cv_threshold:
            continue

        # Estimate day-of-month from most recent transactions
        last_date = dates[-1]
        expected_day = min(last_date.day, 28)  # cap at 28 for month-end safety

        # Confidence: based on gap consistency and number of occurrences
        gap_variance = statistics.stdev(gaps) if len(gaps) > 1 else 0
        gap_consistency = max(0.0, 1.0 - gap_variance / 10.0)
        occurrence_score = min(1.0, len(dates) / 6.0)  # max out at 6 occurrences
        confidence = round((gap_consistency * 0.6 + occurrence_score * 0.4), 2)

        results.append(RecurringCharge(
            merchant=merchant,
            expected_amount=round(mean_amt, 2),
            expected_day=expected_day,
            last_seen=last_date,
            next_expected=_next_occurrence(expected_day),
            frequency="monthly",
            source="auto",
            confidence=confidence,
        ))

    # Sort by next expected date
    results.sort(key=lambda r: r.next_expected)
    return results


def parse_manual_recurring(config_str: str) -> list[RecurringCharge]:
    """
    Parse RECURRING_EXPENSES from config.
    Format: "Netflix|15.99|15,Spotify|9.99|20"
    Returns list of RecurringCharge with source="manual".
    """
    charges = []
    for entry in config_str.split(","):
        parts = [p.strip() for p in entry.strip().split("|")]
        if len(parts) != 3:
            continue
        try:
            merchant = parts[0]
            amount   = float(parts[1])
            day      = int(parts[2])
            charges.append(RecurringCharge(
                merchant=merchant,
                expected_amount=amount,
                expected_day=day,
                last_seen=None,
                next_expected=_next_occurrence(day),
                frequency="monthly",
                source="manual",
                confidence=1.0,
            ))
        except (ValueError, IndexError):
            continue
    return charges


def merge_recurring(
    auto: list[RecurringCharge],
    manual: list[RecurringCharge],
) -> list[RecurringCharge]:
    """
    Merge auto-detected and manual recurring charges.
    If a manual entry matches an auto-detected one (case-insensitive merchant),
    the manual entry wins (it carries confidence=1.0 and overrides the amount).
    """
    manual_names = {c.merchant.lower() for c in manual}
    filtered_auto = [c for c in auto if c.merchant.lower() not in manual_names]
    merged = manual + filtered_auto
    merged.sort(key=lambda r: r.next_expected)
    return merged


def get_upcoming(
    charges: list[RecurringCharge],
    days: int = 7,
) -> list[RecurringCharge]:
    """Charges due within the next N days (including overdue)."""
    return [c for c in charges if 0 <= c.days_until() <= days]


def get_missing(
    charges: list[RecurringCharge],
    conn: sqlite3.Connection,
    grace_days: int = 5,
) -> list[RecurringCharge]:
    """
    Charges that were expected this month but haven't been seen yet.
    A charge is considered "missing" if:
      - next_expected is in the past (beyond grace_days)
      - no transaction matching the merchant appears within window_days of expected date
    """
    missing = []
    today = date.today()

    for charge in charges:
        if not charge.is_overdue(grace_days):
            continue

        # Check if we actually saw it near the expected date
        window_start = (charge.next_expected - timedelta(days=grace_days)).isoformat()
        window_end   = (charge.next_expected + timedelta(days=grace_days)).isoformat()

        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM transactions
            WHERE COALESCE(merchant_name, raw_name) LIKE ?
              AND date >= ? AND date <= ?
              AND pending = 0
            """,
            (f"%{charge.merchant}%", window_start, window_end),
        ).fetchone()

        if row["cnt"] == 0:
            missing.append(charge)

    return missing
