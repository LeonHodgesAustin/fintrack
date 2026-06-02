"""
Net worth snapshot — aggregates all asset types into a single number.

  Net Worth = liquid_accounts
            + property_values
            + vehicle_values
            + vested_equity_value
            + unvested_equity_value   (shown separately, not counted in NW)
            - loan_balances
"""

import sqlite3
from datetime import date
from dataclasses import dataclass, field


@dataclass
class NetWorthSnapshot:
    as_of: date

    # Liquid (from Plaid accounts — sum of positive balances)
    liquid_cash: float = 0.0
    liquid_accounts: list[dict] = field(default_factory=list)

    # Loans (liabilities)
    loan_balances: list[dict] = field(default_factory=list)
    total_debt: float = 0.0

    # Properties
    property_values: list[dict] = field(default_factory=list)
    total_property: float = 0.0
    total_home_equity: float = 0.0   # property values - mortgage balances

    # Vehicles
    vehicle_values: list[dict] = field(default_factory=list)
    total_vehicles: float = 0.0

    # Equity — vested
    vested_equity: list[dict] = field(default_factory=list)
    total_vested_equity: float = 0.0

    # Equity — unvested (informational only, not counted in NW)
    unvested_equity: list[dict] = field(default_factory=list)
    total_unvested_equity: float = 0.0

    # ESPP (informational — shows accrued contributions + estimated gain)
    espp_accrual: list[dict] = field(default_factory=list)
    total_espp_accrual: float = 0.0

    # Manual accounts (401k, IRA, HSA, pension, etc.)
    manual_accounts: list[dict] = field(default_factory=list)
    total_manual_accounts: float = 0.0

    @property
    def net_worth(self) -> float:
        return round(
            self.liquid_cash
            + self.total_property
            + self.total_vehicles
            + self.total_vested_equity
            + self.total_manual_accounts
            - self.total_debt,
            2,
        )

    @property
    def total_assets(self) -> float:
        return round(
            self.liquid_cash
            + self.total_property
            + self.total_vehicles
            + self.total_vested_equity
            + self.total_manual_accounts,
            2,
        )


def snapshot(
    conn: sqlite3.Connection,
    as_of: date | None = None,
    fetch_prices: bool = True,
) -> NetWorthSnapshot:
    """
    Build a full net worth snapshot.

    fetch_prices=True hits yfinance for current stock prices.
    fetch_prices=False uses the most recent cached price (useful for offline use).
    """
    from .loans import from_db_row as loan_from_db, current_balance, monthly_payment
    from .vehicles import from_db_row as vehicle_from_db, estimated_value
    from .equity import rsu_from_db, espp_from_db, vested_shares, unvested_shares
    from .properties import from_db_row as property_from_db, appreciation, appreciation_pct
    from .db import (get_loans, get_vehicles, get_properties,
                     get_equity_grants, get_total_shares_sold, get_cached_price,
                     get_holdings, get_manual_accounts)
    from .prices import get_current_price

    as_of = as_of or date.today()
    snap = NetWorthSnapshot(as_of=as_of)

    # ── Liquid cash (from Plaid account balances) ──────────────────────────
    # Note: Plaid balance data requires the "balance" product — we approximate
    # by summing recent credits minus debits per account. For now we report
    # this as 0 and show a note; a balance sync can be added later.
    # TODO: wire in Plaid /accounts/get when balance product is enabled.

    # ── Loans ──────────────────────────────────────────────────────────────
    for row in get_loans(conn):
        loan = loan_from_db(row)
        balance = current_balance(loan, as_of)
        pmt = monthly_payment(loan)
        snap.loan_balances.append({
            "id":               loan.id,
            "name":             loan.name,
            "type":             loan.loan_type,
            "original":         loan.principal,
            "balance":          balance,
            "monthly_payment":  round(pmt, 2),
            "rate_pct":         round(loan.annual_rate * 100, 3),
        })
    snap.total_debt = round(sum(l["balance"] for l in snap.loan_balances), 2)

    # ── Properties ────────────────────────────────────────────────────────────
    mortgage_balance = sum(
        l["balance"] for l in snap.loan_balances if l["type"] == "mortgage"
    )
    for row in get_properties(conn):
        prop = property_from_db(row)
        value = prop.current_value
        app = appreciation(prop)
        app_pct = appreciation_pct(prop)
        equity = round(value - mortgage_balance, 2) if value is not None else None

        snap.property_values.append({
            "id":               prop.id,
            "name":             prop.name,
            "address":          prop.address,
            "purchase_price":   prop.purchase_price,
            "current_value":    value,
            "value_range_low":  prop.value_range_low,
            "value_range_high": prop.value_range_high,
            "value_updated_at": prop.value_updated_at,
            "appreciation":     app,
            "appreciation_pct": app_pct,
            "home_equity":      equity,
        })
    snap.total_property = round(
        sum(p["current_value"] for p in snap.property_values if p["current_value"] is not None),
        2,
    )
    snap.total_home_equity = round(
        sum(p["home_equity"] for p in snap.property_values if p["home_equity"] is not None),
        2,
    )

    # ── Vehicles ───────────────────────────────────────────────────────────
    for row in get_vehicles(conn):
        vehicle = vehicle_from_db(row)
        value = estimated_value(vehicle, as_of)
        snap.vehicle_values.append({
            "id":               vehicle.id,
            "name":             vehicle.name,
            "purchase_price":   vehicle.purchase_price,
            "estimated_value":  value,
            "depreciation":     round(vehicle.purchase_price - value, 2),
            "depreciation_pct": round(
                (vehicle.purchase_price - value) / vehicle.purchase_price * 100, 1
            ) if vehicle.purchase_price else 0,
        })
    snap.total_vehicles = round(sum(v["estimated_value"] for v in snap.vehicle_values), 2)

    # ── Equity ─────────────────────────────────────────────────────────────
    # Collect all tickers so we fetch prices once per ticker
    grants = get_equity_grants(conn)
    tickers = {g["ticker"] for g in grants}

    prices: dict[str, float] = {}
    for ticker in tickers:
        try:
            if fetch_prices:
                prices[ticker] = get_current_price(ticker, conn)
            else:
                today_str = as_of.isoformat()
                cached = get_cached_price(conn, ticker, today_str)
                prices[ticker] = cached or 0.0
        except Exception:
            prices[ticker] = 0.0

    for row in grants:
        ticker = row["ticker"]
        price = prices.get(ticker, 0.0)

        if row["grant_type"] == "rsu":
            grant = rsu_from_db(row)
            sold = get_total_shares_sold(conn, ticker)
            vest = vested_shares(grant, as_of)
            unvest = unvested_shares(grant, as_of)
            # Available = vested - sold (across all grants for this ticker,
            # approximate here; sold is per-ticker not per-grant)
            available = max(vest - sold, 0.0)

            snap.vested_equity.append({
                "id":             grant.id,
                "ticker":         ticker,
                "grant_date":     grant.grant_date.isoformat(),
                "vested_shares":  vest,
                "available":      available,
                "price":          price,
                "value":          round(available * price, 2),
                "unvested":       unvest,
                "unvested_value": round(unvest * price, 2),
            })
            snap.unvested_equity.append({
                "ticker":         ticker,
                "unvested_shares": unvest,
                "unvested_value": round(unvest * price, 2),
            })

        elif row["grant_type"] == "espp":
            plan = espp_from_db(row)
            # ESPP accrual: contributions withheld since last purchase date
            # We approximate by looking at payroll since the start of the
            # current purchase period.
            from datetime import date as _date
            today = _date.today()
            # Current period start = most recent multiple of purchase_period_months
            # from the offering start
            months_since = (
                (today.year - plan.offering_start_date.year) * 12
                + (today.month - plan.offering_start_date.month)
            )
            periods_completed = months_since // plan.purchase_period_months
            period_start_months = periods_completed * plan.purchase_period_months
            ps_year = plan.offering_start_date.year + period_start_months // 12
            ps_month = plan.offering_start_date.month + period_start_months % 12
            if ps_month > 12:
                ps_month -= 12
                ps_year += 1
            current_period_start = _date(ps_year, ps_month, plan.offering_start_date.day)

            from .equity import estimate_espp_contributions
            accrued = estimate_espp_contributions(plan, current_period_start, today, conn)

            snap.espp_accrual.append({
                "id":                   plan.id,
                "ticker":               ticker,
                "period_start":         current_period_start.isoformat(),
                "accrued_contributions": accrued,
                "discount_rate_pct":    round(plan.discount_rate * 100, 1),
                "lookback_months":      plan.lookback_months,
                "current_price":        price,
            })

    # ── Direct stock holdings (any source) ────────────────────────────────────
    # Collect tickers from holdings so we can batch the price lookups with
    # the grant tickers already fetched above.
    for row in get_holdings(conn):
        ticker = row["ticker"]
        price = prices.get(ticker)
        if price is None:
            try:
                price = get_current_price(ticker, conn) if fetch_prices else (
                    get_cached_price(conn, ticker, as_of.isoformat()) or 0.0
                )
                prices[ticker] = price
            except Exception:
                price = 0.0

        snap.vested_equity.append({
            "id":             row["id"],
            "ticker":         ticker,
            "grant_date":     row["as_of_date"],
            "vested_shares":  row["shares"],
            "available":      row["shares"],
            "price":          price,
            "value":          round(row["shares"] * price, 2),
            "unvested":       0.0,
            "unvested_value": 0.0,
            "source":         "direct",
            "notes":          row.get("notes") or "",
            "cost_basis":     row.get("cost_basis"),
        })

    snap.total_vested_equity = round(
        sum(e["value"] for e in snap.vested_equity), 2
    )
    snap.total_unvested_equity = round(
        sum(e["unvested_value"] for e in snap.unvested_equity), 2
    )
    snap.total_espp_accrual = round(
        sum(e["accrued_contributions"] for e in snap.espp_accrual), 2
    )

    # ── Manual accounts ────────────────────────────────────────────────────────
    for row in get_manual_accounts(conn):
        snap.manual_accounts.append({
            "id":          row["id"],
            "name":        row["name"],
            "type":        row["account_type"],
            "institution": row.get("institution") or "",
            "balance":     row.get("balance"),
            "updated_at":  row.get("balance_updated_at"),
            "notes":       row.get("notes") or "",
        })
    snap.total_manual_accounts = round(
        sum(a["balance"] for a in snap.manual_accounts if a["balance"] is not None),
        2,
    )

    return snap
