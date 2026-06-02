"""
Budget analysis and scenario modeling.

Builds a structured monthly budget from real transaction history:
  - Income: average monthly credits (payroll, etc.) excluding transfers
  - Fixed loans: scheduled payments from the loans table
  - Recurring charges: auto-detected + manual recurring expenses
  - Variable spending: average monthly by category
  - Transfers out: investments, child support, Venmo, etc.

The scenario modeler applies hypothetical monthly changes to show the
impact on surplus before you commit to a real change.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date


@dataclass
class ScenarioChange:
    label: str
    monthly_amount: float   # positive = new expense, negative = expense removed


@dataclass
class BudgetSnapshot:
    months: int
    period_label: str       # e.g. "Mar–May 2026"

    # Income
    income_by_source: list[dict] = field(default_factory=list)
    avg_income: float = 0.0

    # Fixed loan obligations (from loans table)
    loan_payments: list[dict] = field(default_factory=list)

    # Recurring charges (subscriptions, child support, etc.)
    recurring: list[dict] = field(default_factory=list)

    # Variable spending by category
    variable: list[dict] = field(default_factory=list)

    # True outflows — money leaving your financial ecosystem
    transfers_out: float = 0.0
    transfer_detail: list[dict] = field(default_factory=list)

    # Savings transfers — money moving to your own savings/goal accounts
    # Still belongs to you; excluded from the deficit calculation
    savings_out: float = 0.0
    savings_detail: list[dict] = field(default_factory=list)

    # Saved normalizations from the budget_adjustments table.
    # Negative amounts reduce the expense total (e.g. -1000 strips an annual
    # item that inflated one month's average).
    saved_adjustments: list[dict] = field(default_factory=list)

    # Per-category spending targets from the budget_targets table.
    # {category: {target_amount, notes, ...}}
    targets: dict = field(default_factory=dict)

    @property
    def total_loans(self) -> float:
        return round(sum(l["monthly_payment"] for l in self.loan_payments), 2)

    @property
    def total_recurring(self) -> float:
        return round(sum(r["monthly_amount"] for r in self.recurring), 2)

    @property
    def total_variable(self) -> float:
        return round(sum(v["avg_amount"] for v in self.variable), 2)

    @property
    def total_obligations(self) -> float:
        return round(self.total_loans + self.total_recurring, 2)

    @property
    def total_saved_adjustments(self) -> float:
        """Net monthly delta from all active saved adjustments."""
        return round(sum(a["monthly_amount"] for a in self.saved_adjustments), 2)

    @property
    def raw_surplus(self) -> float:
        """
        Surplus straight from transaction history — before any saved
        normalization adjustments are applied.  Shows the unedited picture.
        """
        return round(
            self.avg_income
            - self.total_loans
            - self.total_recurring
            - self.total_variable
            - self.transfers_out,
            2,
        )

    @property
    def surplus(self) -> float:
        """
        Surplus after applying all active saved normalizations.
        This is the number used for what-if scenario modeling.
        Savings transfers are excluded — that money is still yours.
        """
        return round(self.raw_surplus - self.total_saved_adjustments, 2)

    def model_scenario(self, changes: list[ScenarioChange]) -> float:
        """Return adjusted surplus after applying additional one-off changes."""
        delta = sum(c.monthly_amount for c in changes)
        return round(self.surplus - delta, 2)


def _month_bounds_n_ago(n: int) -> tuple[str, str]:
    """Return (start_iso, end_iso) for the calendar month n months ago."""
    today = date.today()
    m = today.month - n
    y = today.year
    while m <= 0:
        m += 12
        y -= 1
    start = date(y, m, 1)
    if m == 12:
        end = date(y + 1, 1, 1)
    else:
        end = date(y, m + 1, 1)
    return start.isoformat(), end.isoformat()


def _period_label(months: int) -> str:
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    today = date.today()
    labels = []
    for n in range(months - 1, -1, -1):
        m = today.month - 1 - n
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        labels.append(month_names[m - 1])
    if len(labels) == 1:
        return labels[0]
    return f"{labels[0]}–{labels[-1]} {today.year}"


def build_budget(
    conn: sqlite3.Connection,
    months: int = 3,
    transfer_categories: frozenset | None = None,
    savings_merchants: set[str] | None = None,
) -> BudgetSnapshot:
    """
    Build a budget snapshot averaged over the last N complete calendar months.
    The current (partial) month is excluded.
    """
    if transfer_categories is None:
        transfer_categories = frozenset(["TRANSFER_IN", "TRANSFER_OUT"])
    if savings_merchants is None:
        savings_merchants = set()

    snap = BudgetSnapshot(months=months, period_label=_period_label(months))

    # Date range: N complete months ending at start of current month
    today = date.today()
    end_month = date(today.year, today.month, 1)
    start_m = today.month - months
    start_y = today.year
    while start_m <= 0:
        start_m += 12
        start_y -= 1
    start_date = date(start_y, start_m, 1).isoformat()
    end_date = end_month.isoformat()

    # ── Income ────────────────────────────────────────────────────────────────
    income_rows = conn.execute(
        """
        SELECT
            COALESCE(category_primary, 'INCOME') AS category,
            SUM(ABS(amount)) / ? AS avg_amount,
            COUNT(*) AS txn_count
        FROM transactions
        WHERE date >= ? AND date < ?
          AND amount < 0
          AND pending = 0
          AND category_primary NOT IN ('TRANSFER_IN', 'TRANSFER_OUT')
        GROUP BY category_primary
        ORDER BY avg_amount DESC
        """,
        (months, start_date, end_date),
    ).fetchall()

    snap.income_by_source = [dict(r) for r in income_rows]
    snap.avg_income = round(sum(r["avg_amount"] for r in snap.income_by_source), 2)

    # ── Fixed loan payments ───────────────────────────────────────────────────
    try:
        from .assets.db import get_loans
        from .assets.loans import from_db_row, monthly_payment, current_balance
        for row in get_loans(conn):
            loan = from_db_row(row)
            bal = current_balance(loan)
            if bal > 0:
                snap.loan_payments.append({
                    "name":            loan.name,
                    "type":            loan.loan_type,
                    "monthly_payment": round(monthly_payment(loan), 2),
                    "balance":         bal,
                    "rate_pct":        round(loan.annual_rate * 100, 3),
                })
    except Exception:
        pass  # assets module not migrated yet

    # ── Recurring charges ─────────────────────────────────────────────────────
    try:
        from .recurring import detect_recurring, parse_manual_recurring, merge_recurring
        from .db import get_recurring_excludes
        from .config import get_settings
        s = get_settings()
        excludes = get_recurring_excludes(conn) | s.get_recurring_exclude_merchants()
        auto = detect_recurring(conn, lookback_days=s.recurring_lookback_days,
                                min_occurrences=s.recurring_min_occurrences,
                                window_days=s.recurring_window_days,
                                amount_cv_threshold=s.recurring_amount_tolerance,
                                exclude_merchants=excludes)
        manual = parse_manual_recurring(s.recurring_expenses)
        all_recurring = merge_recurring(auto, manual)

        # Exclude items that are already covered by the loans table, and
        # filter out low-confidence auto-detected entries (noise like
        # occasional same-amount purchases at the same merchant).
        loan_name_fragments = {
            word.lower()
            for loan in snap.loan_payments
            for word in loan["name"].split()
            if len(word) > 3
        }

        # ALSO pull merchant names from actual LOAN_PAYMENTS transactions.
        # This catches cases where the servicer name (e.g. "Aqua Finance")
        # differs from the loan's label in the assets DB (e.g. "Roof Loan").
        # Any merchant that has ever appeared in a LOAN_PAYMENTS transaction
        # is a loan servicer and should not also appear in recurring.
        _loan_servicer_names: set[str] = {
            row[0].strip().lower()
            for row in conn.execute(
                "SELECT COALESCE(merchant_name, raw_name, '') "
                "FROM transactions "
                "WHERE category_primary = 'LOAN_PAYMENTS' AND amount > 0 "
                "GROUP BY COALESCE(merchant_name, raw_name)"
            ).fetchall()
            if row[0].strip()
        }

        def _is_loan_duplicate(merchant: str) -> bool:
            ml = merchant.lower()
            # Match against word fragments from the loan name in the assets DB
            if any(frag in ml for frag in loan_name_fragments):
                return True
            # Match against known LOAN_PAYMENTS merchants from transaction history
            # (len > 4 guard prevents short tokens like "co." producing false hits)
            return any(
                (lm in ml or ml in lm)
                for lm in _loan_servicer_names
                if len(lm) > 4
            )

        snap.recurring = [
            {"label": c.merchant, "monthly_amount": c.expected_amount,
             "source": c.source, "confidence": c.confidence}
            for c in all_recurring
            if c.frequency == "monthly"
            and not _is_loan_duplicate(c.merchant)
            and (c.source == "manual" or c.confidence >= 0.55)
        ]
    except Exception:
        pass

    # ── Variable spending ─────────────────────────────────────────────────────
    # Exclude transfers, income, loan payments, AND merchants already captured
    # in recurring fixed obligations (e.g. streaming subscriptions that Plaid
    # categorizes as ENTERTAINMENT while also appearing in the recurring list).
    #
    # Matching strategy — two passes to handle billing-entity mismatches:
    #   1. Whole label:  "YouTube Premium" → NOT LIKE '%youtube premium%'
    #   2. First token:  "YouTube" (from "YouTube Premium") → NOT LIKE '%youtube%'
    # This catches cases where Plaid enriches the merchant_name differently
    # from the subscription service name (e.g. YouTube billed as "Google").
    recurring_merchants_lower = [r["label"].lower() for r in snap.recurring]

    # Build de-duplicated list of patterns: full label + first significant token.
    # The first-token fallback handles billing-entity mismatches: "YouTube Premium"
    # (recurring label) may be charged as "Google" in Plaid, so we also try
    # NOT LIKE '%youtube%'.  We only use the FIRST token to avoid over-matching —
    # adding '%premium%' would incorrectly exclude unrelated "Premium" merchants.
    _seen_patterns: set[str] = set()
    _exclusion_patterns: list[str] = []
    for label in recurring_merchants_lower:
        for candidate in [label]:
            pat = f"%{candidate}%"
            if pat not in _seen_patterns:
                _seen_patterns.add(pat)
                _exclusion_patterns.append(pat)
        # First significant word as fallback (len > 4 skips short filler words)
        first_word = next((tok for tok in label.split() if len(tok) > 4), None)
        if first_word and first_word != label:
            pat = f"%{first_word}%"
            if pat not in _seen_patterns:
                _seen_patterns.add(pat)
                _exclusion_patterns.append(pat)

    merchant_exclusion_clauses = " AND ".join(
        "LOWER(COALESCE(merchant_name, raw_name, '')) NOT LIKE ?"
        for _ in _exclusion_patterns
    )
    merchant_params = _exclusion_patterns

    where_extra = f"AND {merchant_exclusion_clauses}" if merchant_exclusion_clauses else ""

    variable_rows = conn.execute(
        f"""
        SELECT
            COALESCE(category_primary, 'UNCATEGORIZED') AS category,
            SUM(amount) / ? AS avg_amount,
            COUNT(*) AS txn_count
        FROM transactions
        WHERE date >= ? AND date < ?
          AND amount > 0
          AND pending = 0
          AND category_primary NOT IN (
              'TRANSFER_IN', 'TRANSFER_OUT',
              'LOAN_PAYMENTS', 'INCOME'
          )
          {where_extra}
        GROUP BY category_primary
        ORDER BY avg_amount DESC
        """,
        [months, start_date, end_date] + merchant_params,
    ).fetchall()

    snap.variable = [dict(r) for r in variable_rows]

    # ── Transfers out ─────────────────────────────────────────────────────────
    # Split into true outflows vs savings transfers.
    # Also deduplicate: if a merchant is already counted in recurring fixed
    # obligations, don't subtract it again here (e.g. child support via Venmo
    # is in recurring AND would otherwise appear in Venmo transfers).
    recurring_merchants = {r["label"].lower() for r in snap.recurring}

    transfer_rows = conn.execute(
        """
        SELECT
            COALESCE(merchant_name, raw_name, 'Unknown') AS merchant,
            SUM(amount) / ? AS avg_amount,
            COUNT(*) AS txn_count
        FROM transactions
        WHERE date >= ? AND date < ?
          AND amount > 0
          AND pending = 0
          AND category_primary = 'TRANSFER_OUT'
        GROUP BY COALESCE(merchant_name, raw_name)
        ORDER BY avg_amount DESC
        """,
        (months, start_date, end_date),
    ).fetchall()

    for row in transfer_rows:
        d = dict(row)
        merchant_lower = d["merchant"].lower()

        # Route to savings if merchant is in SAVINGS_TRANSFER_MERCHANTS
        is_savings = any(s in merchant_lower for s in savings_merchants if s)
        if is_savings:
            snap.savings_detail.append(d)
            continue

        # Skip if this merchant is already fully accounted for in recurring
        # (prevents double-counting e.g. Venmo child support)
        if any(rec in merchant_lower or merchant_lower in rec
               for rec in recurring_merchants):
            continue

        snap.transfer_detail.append(d)

    snap.transfers_out = round(sum(r["avg_amount"] for r in snap.transfer_detail), 2)
    snap.savings_out   = round(sum(r["avg_amount"] for r in snap.savings_detail), 2)

    # ── Saved adjustments ─────────────────────────────────────────────────────
    try:
        from .db import get_budget_adjustments
        snap.saved_adjustments = get_budget_adjustments(conn, active_only=True)
    except Exception:
        pass

    # ── Category targets ──────────────────────────────────────────────────────
    try:
        from .db import get_budget_targets
        snap.targets = get_budget_targets(conn)
    except Exception:
        pass

    return snap
