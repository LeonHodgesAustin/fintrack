"""
Vehicle depreciation estimates.

Uses declining-balance depreciation, which roughly tracks real-world
used-car market values for US vehicles:
  Year 1: heaviest drop (~20% of purchase price)
  Years 2-5: ~15-18%/yr of remaining value
  Year 6+: slows to ~10-12%/yr

The default 18%/yr declining balance is a reasonable average. Override
annual_depreciation per vehicle if you have a more accurate figure
(e.g. trucks/SUVs depreciate slower; luxury cars faster).

This is an estimate, not a quote. For precision, use KBB or Carfax.
"""

from dataclasses import dataclass
from datetime import date


@dataclass
class Vehicle:
    id: int
    name: str
    purchase_price: float
    purchase_date: date
    annual_depreciation: float  # e.g. 0.18 for 18%/yr


def estimated_value(vehicle: Vehicle, as_of: date | None = None) -> float:
    """
    Current estimated value using declining-balance depreciation.

    V(t) = purchase_price × (1 − annual_depreciation)^t
    where t = fractional years since purchase.
    """
    as_of = as_of or date.today()
    days = (as_of - vehicle.purchase_date).days
    if days <= 0:
        return vehicle.purchase_price
    years = days / 365.25
    value = vehicle.purchase_price * (1 - vehicle.annual_depreciation) ** years
    return round(max(value, 0.0), 2)


def total_depreciation(vehicle: Vehicle, as_of: date | None = None) -> float:
    """Amount the vehicle has depreciated from purchase price."""
    return round(vehicle.purchase_price - estimated_value(vehicle, as_of), 2)


def depreciation_schedule(vehicle: Vehicle, years: int = 10) -> list[dict]:
    """Year-by-year estimated value for planning purposes."""
    rows = []
    for y in range(years + 1):
        from datetime import timedelta
        as_of = date(
            vehicle.purchase_date.year + y,
            vehicle.purchase_date.month,
            vehicle.purchase_date.day,
        )
        value = estimated_value(vehicle, as_of)
        rows.append({
            "year": y,
            "as_of": as_of.isoformat(),
            "estimated_value": value,
            "depreciation_to_date": round(vehicle.purchase_price - value, 2),
        })
    return rows


def from_db_row(row: dict) -> Vehicle:
    return Vehicle(
        id=row["id"],
        name=row["name"],
        purchase_price=row["purchase_price"],
        purchase_date=date.fromisoformat(row["purchase_date"]),
        annual_depreciation=row["annual_depreciation"],
    )
