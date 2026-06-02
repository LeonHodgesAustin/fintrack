"""
Stock price fetching via yfinance with SQLite cache.

yfinance is an optional dependency. Install with:
    pip install "fintrack[assets]"

The cache stores daily closing prices in stock_price_cache so repeated
calls (e.g. running `fintrack networth` multiple times) don't re-hit Yahoo.
Cache entries are considered fresh for 4 hours for today's price,
indefinitely for historical dates.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone


def _check_yfinance():
    try:
        import yfinance  # noqa: F401
    except ImportError:
        raise ImportError(
            "yfinance is not installed.\n"
            "Install it with:  pip install 'fintrack[assets]'\n"
            "or:               pip install yfinance"
        )


def get_current_price(ticker: str, conn: sqlite3.Connection | None = None) -> float:
    """
    Current market price for a ticker.
    Checks cache first (4-hour TTL for today), then fetches from Yahoo.
    """
    _check_yfinance()
    import yfinance as yf

    today = date.today().isoformat()

    # Check cache — for today, only use if fetched within 4 hours
    if conn:
        row = conn.execute(
            "SELECT close_price, fetched_at FROM stock_price_cache WHERE ticker = ? AND price_date = ?",
            (ticker.upper(), today),
        ).fetchone()
        if row:
            fetched = datetime.fromisoformat(row["fetched_at"])
            age = datetime.now(timezone.utc) - fetched.replace(tzinfo=timezone.utc)
            if age.total_seconds() < 4 * 3600:
                return float(row["close_price"])

    t = yf.Ticker(ticker)
    price = t.fast_info.last_price
    if price is None:
        hist = t.history(period="1d")
        if hist.empty:
            raise ValueError(f"Could not fetch price for {ticker}")
        price = float(hist["Close"].iloc[-1])

    price = round(float(price), 4)

    if conn:
        from .db import cache_price
        cache_price(conn, ticker, today, price)
        conn.commit()

    return price


def get_historical_prices(
    ticker: str,
    start_date: str,
    end_date: str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, float]:
    """
    Daily closing prices for a date range.
    Fills from cache where available; fetches missing dates from Yahoo.
    Returns {date_str: close_price}.
    """
    _check_yfinance()
    import yfinance as yf

    cached: dict[str, float] = {}
    if conn:
        from .db import get_cached_range
        cached = get_cached_range(conn, ticker, start_date, end_date)

    # Count trading days expected in range
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    total_days = (end - start).days

    # If cache looks complete enough (within 10% of expected trading days), use it
    expected_trading_days = total_days * 5 / 7
    if len(cached) >= max(1, expected_trading_days * 0.9):
        return cached

    t = yf.Ticker(ticker)
    hist = t.history(start=start_date, end=(end + timedelta(days=1)).isoformat())

    prices: dict[str, float] = {}
    for ts, row in hist.iterrows():
        d = ts.date().isoformat()
        prices[d] = round(float(row["Close"]), 4)

    if conn:
        from .db import cache_price
        for d, p in prices.items():
            cache_price(conn, ticker, d, p)
        conn.commit()

    return prices


def min_price_in_range(
    ticker: str,
    start_date: str,
    end_date: str,
    conn: sqlite3.Connection | None = None,
) -> float:
    """Minimum closing price in a date range — used for ESPP lookback calculation."""
    prices = get_historical_prices(ticker, start_date, end_date, conn)
    if not prices:
        raise ValueError(f"No price data for {ticker} between {start_date} and {end_date}")
    return min(prices.values())


def price_on_date(
    ticker: str,
    target_date: str,
    conn: sqlite3.Connection | None = None,
) -> float:
    """
    Closing price on a specific date (or nearest prior trading day).
    """
    if conn:
        from .db import get_cached_price
        cached = get_cached_price(conn, ticker, target_date)
        if cached is not None:
            return cached

    # Fetch a small window around the date to handle weekends/holidays
    d = date.fromisoformat(target_date)
    start = (d - timedelta(days=5)).isoformat()
    end = (d + timedelta(days=1)).isoformat()
    prices = get_historical_prices(ticker, start, end, conn)

    if not prices:
        raise ValueError(f"No price data for {ticker} near {target_date}")

    # Return the closest date on or before target
    candidates = {k: v for k, v in prices.items() if k <= target_date}
    if not candidates:
        return next(iter(sorted(prices.items())))[1]
    return candidates[max(candidates)]
