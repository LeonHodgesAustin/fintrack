"""
Loan amortization and balance calculation.

Supports mortgage and auto loans. Given the original terms, every number
(schedule, current balance, total interest paid, equity) is derived
mathematically — no external data needed.

Sign convention: balance is always positive (amount still owed).
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Generator


@dataclass
class Loan:
    id: int
    name: str
    loan_type: str          # "mortgage" | "auto" | "personal" | "student"
    principal: float        # balance as of start_date — NOT necessarily the original loan amount.
                            # Use original amount + original first payment date when you have docs.
                            # Use current servicer balance + next payment date when you don't.
    annual_rate: float      # e.g. 0.065 for 6.5%
    term_months: int        # months REMAINING from start_date
    start_date: date        # date of first scheduled payment from principal
    monthly_payment: float | None = None   # calculated from terms if None
    actual_balance: float | None = None    # servicer-confirmed balance; overrides calculated when set
    balance_updated_at: str | None = None  # when actual_balance was last confirmed


def _calc_payment(principal: float, monthly_rate: float, term_months: int) -> float:
    """Standard annuity formula. Returns 0 for 0% rate loans."""
    if monthly_rate == 0:
        return principal / term_months
    return principal * monthly_rate * (1 + monthly_rate) ** term_months / (
        (1 + monthly_rate) ** term_months - 1
    )


def monthly_payment(loan: Loan) -> float:
    if loan.monthly_payment is not None:
        return loan.monthly_payment
    return _calc_payment(loan.principal, loan.annual_rate / 12, loan.term_months)


@dataclass
class AmortizationRow:
    payment_number: int
    payment_date: date
    payment: float
    principal_paid: float
    interest_paid: float
    balance: float


def amortization_schedule(loan: Loan) -> list[AmortizationRow]:
    """Full amortization schedule from payment 1 through payoff."""
    r = loan.annual_rate / 12
    pmt = monthly_payment(loan)
    balance = loan.principal
    rows = []

    for n in range(1, loan.term_months + 1):
        # Payment date: start_date + (n-1) months
        m = loan.start_date.month + (n - 1)
        y = loan.start_date.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        try:
            pmt_date = date(y, m, loan.start_date.day)
        except ValueError:
            # Month-end safety (e.g. Feb 30 → Feb 28)
            import calendar
            last = calendar.monthrange(y, m)[1]
            pmt_date = date(y, m, last)

        interest = round(balance * r, 2)
        principal = round(min(pmt - interest, balance), 2)
        balance = round(max(balance - principal, 0.0), 2)

        rows.append(AmortizationRow(
            payment_number=n,
            payment_date=pmt_date,
            payment=round(pmt, 2),
            principal_paid=principal,
            interest_paid=interest,
            balance=balance,
        ))

        if balance == 0:
            break

    return rows


def current_balance(loan: Loan, as_of: date | None = None) -> float:
    """
    Outstanding balance as of a given date (defaults to today).

    If an actual_balance has been set (via `fintrack assets loan set-balance`),
    that value is returned directly — it reflects the real servicer balance and
    accounts for missed payments, extra principal, or any other deviation from
    the original schedule.

    Otherwise, falls back to the calculated amortization schedule balance,
    which assumes every payment has been made on time.
    """
    if loan.actual_balance is not None:
        return loan.actual_balance

    as_of = as_of or date.today()
    schedule = amortization_schedule(loan)
    paid_through = [row for row in schedule if row.payment_date <= as_of]
    if not paid_through:
        return loan.principal
    return paid_through[-1].balance


def calculated_balance(loan: Loan, as_of: date | None = None) -> float:
    """Scheduled balance from amortization, ignoring any actual_balance override."""
    as_of = as_of or date.today()
    schedule = amortization_schedule(loan)
    paid_through = [row for row in schedule if row.payment_date <= as_of]
    if not paid_through:
        return loan.principal
    return paid_through[-1].balance


def principal_paid(loan: Loan, as_of: date | None = None) -> float:
    """Total principal repaid through as_of (scheduled basis)."""
    return round(loan.principal - current_balance(loan, as_of), 2)


def interest_paid(loan: Loan, as_of: date | None = None) -> float:
    """Total interest paid through as_of."""
    as_of = as_of or date.today()
    schedule = amortization_schedule(loan)
    return round(
        sum(row.interest_paid for row in schedule if row.payment_date <= as_of),
        2,
    )


def payoff_date(loan: Loan) -> date:
    schedule = amortization_schedule(loan)
    return schedule[-1].payment_date


def from_db_row(row: dict) -> Loan:
    return Loan(
        id=row["id"],
        name=row["name"],
        loan_type=row["loan_type"],
        principal=row["principal"],
        annual_rate=row["annual_rate"],
        term_months=row["term_months"],
        start_date=date.fromisoformat(row["start_date"]),
        monthly_payment=row.get("monthly_payment"),
        actual_balance=row.get("actual_balance"),
        balance_updated_at=row.get("balance_updated_at"),
    )
