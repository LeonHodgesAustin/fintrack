"""
Real estate / property tracking for fintrack.

Market value sources (tried in order when `fintrack assets property refresh` runs):
  1. Rentcast AVM API  — set RENTCAST_API_KEY in .env (free tier at rentcast.io)
  2. Manual override   — set any time with `fintrack assets property set-value`

Rentcast API returns a price estimate plus a low/high confidence range.
Results are stored in the properties table; no re-fetch happens unless you
explicitly run `refresh` (counts against your monthly quota).

Home equity shown in `fintrack networth` = current_value - outstanding_mortgage_balance.
The mortgage balance comes from the loans table — no extra input needed once both
the loan and property are entered.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date


@dataclass
class Property:
    id: int
    name: str
    address: str
    purchase_price: float
    purchase_date: date
    current_value: float | None      # None until set manually or via Rentcast
    value_range_low: float | None
    value_range_high: float | None
    value_updated_at: str | None


def fetch_rentcast_value(
    address: str,
    api_key: str,
    property_type: str = "Single Family",
) -> dict:
    """
    Call the Rentcast AVM endpoint and return a value estimate.

    Returns:
        {price, price_range_low, price_range_high, latitude, longitude}

    Raises ValueError with a readable message on any API or network error.

    Rentcast free tier: ~50 requests/month. Each call to this function
    consumes one request, so avoid calling it in a loop.
    """
    params = urllib.parse.urlencode({
        "address": address,
        "propertyType": property_type,
    })
    url = f"https://api.rentcast.io/v1/avm/value?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "X-Api-Key": api_key,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            msg = json.loads(body).get("message") or body
        except Exception:
            msg = body
        raise ValueError(f"Rentcast API {exc.code}: {msg}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Rentcast connection failed: {exc.reason}") from exc

    price = data.get("price")
    if price is None:
        raise ValueError(
            f"Rentcast returned no price estimate for this address. "
            f"Full response: {data}"
        )

    return {
        "price":            float(price),
        "price_range_low":  float(data["priceRangeLow"])  if data.get("priceRangeLow")  else None,
        "price_range_high": float(data["priceRangeHigh"]) if data.get("priceRangeHigh") else None,
        "latitude":         data.get("latitude"),
        "longitude":        data.get("longitude"),
    }


def appreciation(prop: Property) -> float | None:
    if prop.current_value is None:
        return None
    return round(prop.current_value - prop.purchase_price, 2)


def appreciation_pct(prop: Property) -> float | None:
    if prop.current_value is None or prop.purchase_price == 0:
        return None
    return round((prop.current_value - prop.purchase_price) / prop.purchase_price * 100, 1)


def from_db_row(row: dict) -> Property:
    return Property(
        id=row["id"],
        name=row["name"],
        address=row["address"],
        purchase_price=row["purchase_price"],
        purchase_date=date.fromisoformat(row["purchase_date"]),
        current_value=row.get("current_value"),
        value_range_low=row.get("value_range_low"),
        value_range_high=row.get("value_range_high"),
        value_updated_at=row.get("value_updated_at"),
    )
