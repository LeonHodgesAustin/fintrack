"""
Prophet-based spending forecasts for fintrack.

Prophet is an OPTIONAL dependency. If not installed, all functions raise
ImportError with instructions. Install with:
    pip install prophet

Prophet models monthly category-level spending using:
  - Trend component (linear / logistic growth)
  - Yearly seasonality (Fourier series, default 10 harmonics)
  - Holiday effects (optional, not wired up here)

For individual transaction anomaly detection we use a simpler z-score approach
on a rolling window -- faster, more interpretable, no Stan required.

Usage pattern:
    from fintrack.forecasting import forecast_all_categories, detect_anomalous_months
    forecasts = forecast_all_categories(conn, months_ahead=3)
    anomalies = detect_anomalous_months(conn, sigma_threshold=2.0)
"""

import sqlite3
import statistics
from datetime import date, timedelta


def _check_prophet():
    try:
        import prophet  # noqa: F401
    except ImportError:
        raise ImportError(
            "prophet is not installed.\n"
            "Install it with:  pip install prophet\n"
            "Note: prophet requires cmdstanpy and a C++ compiler.\n"
            "Full instructions: https://facebook.github.io/prophet/docs/installation.html"
        )


def _get_monthly_spend_series(
    conn: sqlite3.Connection,
    category: str,
    training_months: int = 24,
) -> list[dict]:
    """
    Return [{ds: 'YYYY-MM-01', y: float}, ...] for the given category,
    aggregated by calendar month, for the last training_months months.
    Excludes pending and transfer categories.
    """
    cutoff = date.today().replace(day=1)
    start = date(
        cutoff.year - training_months // 12,
        ((cutoff.month - 1 - training_months % 12) % 12) + 1,
        1,
    )
    # Simpler: subtract months directly
    y, m = cutoff.year, cutoff.month
    for _ in range(training_months):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    start = date(y, m, 1)

    rows = conn.execute(
        """
        SELECT
            strftime('%Y-%m-01', date) AS ds,
            SUM(amount)                AS y
        FROM transactions
        WHERE category_primary = ?
          AND amount > 0
          AND pending = 0
          AND date >= ?
        GROUP BY strftime('%Y-%m', date)
        ORDER BY ds
        """,
        (category, start.isoformat()),
    ).fetchall()

    return [{"ds": r["ds"], "y": float(r["y"])} for r in rows]


def forecast_category_spend(
    conn: sqlite3.Connection,
    category: str,
    months_ahead: int = 3,
    training_months: int = 18,
    yearly_seasonality: bool = True,
) -> list[dict]:
    """
    Forecast spending for a single category for the next months_ahead months.

    Returns list of dicts:
        [{ds, yhat, yhat_lower, yhat_upper, is_forecast}, ...]
    where is_forecast=False for historical fitted values, True for future.

    Raises ImportError if prophet is not installed.
    """
    _check_prophet()
    import pandas as pd
    from prophet import Prophet

    series = _get_monthly_spend_series(conn, category, training_months)
    if len(series) < 3:
        return []  # not enough data to fit

    df = pd.DataFrame(series)
    df["ds"] = pd.to_datetime(df["ds"])
    df["y"]  = df["y"].clip(lower=0)  # spending can't be negative

    model = Prophet(
        yearly_seasonality=yearly_seasonality,
        weekly_seasonality=False,   # monthly data -- no weekly pattern
        daily_seasonality=False,
        interval_width=0.80,        # 80% prediction interval
        changepoint_prior_scale=0.05,  # conservative trend changes
    )
    model.fit(df)

    # Make future dataframe: last historical date + months_ahead months
    future = model.make_future_dataframe(periods=months_ahead, freq="MS")
    forecast = model.predict(future)

    # Clip lower bound to 0
    forecast["yhat"]       = forecast["yhat"].clip(lower=0)
    forecast["yhat_lower"] = forecast["yhat_lower"].clip(lower=0)
    forecast["yhat_upper"] = forecast["yhat_upper"].clip(lower=0)

    historical_ds = set(df["ds"].dt.strftime("%Y-%m-%d"))

    result = []
    for _, row in forecast.iterrows():
        ds = row["ds"].strftime("%Y-%m-%d")
        result.append({
            "ds":          ds,
            "yhat":        round(float(row["yhat"]), 2),
            "yhat_lower":  round(float(row["yhat_lower"]), 2),
            "yhat_upper":  round(float(row["yhat_upper"]), 2),
            "is_forecast": ds not in historical_ds,
            "category":    category,
        })

    return result


def forecast_all_categories(
    conn: sqlite3.Connection,
    months_ahead: int = 3,
    training_months: int = 18,
    min_history_months: int = 3,
) -> dict[str, list[dict]]:
    """
    Forecast all categories with sufficient history.
    Returns {category: [forecast_rows]}.
    """
    _check_prophet()

    # Get categories with enough data
    rows = conn.execute(
        """
        SELECT
            category_primary AS category,
            COUNT(DISTINCT strftime('%Y-%m', date)) AS months
        FROM transactions
        WHERE amount > 0 AND pending = 0
          AND category_primary NOT IN ('TRANSFER_IN', 'TRANSFER_OUT', 'UNCATEGORIZED')
          AND category_primary IS NOT NULL
        GROUP BY category_primary
        HAVING months >= ?
        """,
        (min_history_months,),
    ).fetchall()

    results = {}
    for r in rows:
        cat = r["category"]
        try:
            fc = forecast_category_spend(conn, cat, months_ahead, training_months)
            if fc:
                results[cat] = fc
        except Exception:
            pass  # skip categories that fail to fit

    return results


def detect_anomalous_months(
    conn: sqlite3.Connection,
    months: int = 12,
    sigma_threshold: float = 2.0,
    use_prophet: bool = True,
) -> list[dict]:
    """
    Find months where actual spending is unusually high vs. historical pattern.

    If Prophet is available and use_prophet=True, uses Prophet's residuals and
    uncertainty intervals to flag anomalies (actual > yhat_upper).

    Falls back to simple z-score analysis if Prophet is not installed:
    computes rolling mean and stddev, flags months > sigma_threshold sigmas
    above the mean. This is interpretable and works with minimal history.

    Returns list of dicts:
        [{category, month_ds, actual, expected, sigma, method}, ...]
    sorted by sigma descending.
    """
    if use_prophet:
        try:
            return _detect_anomalies_prophet(conn, months, sigma_threshold)
        except ImportError:
            pass  # fall through to z-score

    return _detect_anomalies_zscore(conn, months, sigma_threshold)


def _detect_anomalies_prophet(
    conn: sqlite3.Connection,
    months: int,
    sigma_threshold: float,
) -> list[dict]:
    """Prophet-based anomaly detection using prediction interval exceedances."""
    _check_prophet()

    all_forecasts = forecast_all_categories(conn, months_ahead=0, training_months=months + 6)
    anomalies = []

    for category, fc_rows in all_forecasts.items():
        for row in fc_rows:
            if row["is_forecast"]:
                continue
            # Get actual spend for this month
            actual_rows = conn.execute(
                """
                SELECT SUM(amount) AS total
                FROM transactions
                WHERE category_primary = ?
                  AND strftime('%Y-%m-01', date) = ?
                  AND amount > 0 AND pending = 0
                """,
                (category, row["ds"]),
            ).fetchone()
            actual = float(actual_rows["total"] or 0)

            if actual > row["yhat_upper"] and row["yhat_upper"] > 0:
                # Approximate sigma: how many interval widths above upper bound
                interval_half = (row["yhat_upper"] - row["yhat_lower"]) / 2
                if interval_half > 0:
                    sigma = (actual - row["yhat"]) / interval_half
                else:
                    sigma = 999.0

                if sigma >= sigma_threshold:
                    anomalies.append({
                        "category":  category,
                        "month_ds":  row["ds"][:7],
                        "actual":    round(actual, 2),
                        "expected":  round(row["yhat"], 2),
                        "sigma":     round(sigma, 1),
                        "method":    "prophet",
                    })

    anomalies.sort(key=lambda r: r["sigma"], reverse=True)
    return anomalies


def _detect_anomalies_zscore(
    conn: sqlite3.Connection,
    months: int,
    sigma_threshold: float,
) -> list[dict]:
    """
    Simple z-score anomaly detection. No external deps.
    Uses the last `months` of data; flags the most recent month if it's
    > sigma_threshold standard deviations above the historical mean.
    """
    rows = conn.execute(
        """
        SELECT
            category_primary AS category,
            strftime('%Y-%m', date) AS month,
            SUM(amount) AS total
        FROM transactions
        WHERE amount > 0 AND pending = 0
          AND category_primary NOT IN ('TRANSFER_IN', 'TRANSFER_OUT', 'UNCATEGORIZED')
          AND category_primary IS NOT NULL
        GROUP BY category_primary, strftime('%Y-%m', date)
        ORDER BY category_primary, month
        """,
    ).fetchall()

    # Group by category
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(
            {"month": r["month"], "total": float(r["total"])}
        )

    anomalies = []
    for cat, series in by_cat.items():
        if len(series) < 3:
            continue
        # Use all but the last entry as baseline
        baseline = [s["total"] for s in series[:-1]]
        current  = series[-1]
        mean = statistics.mean(baseline)
        if len(baseline) > 1:
            std = statistics.stdev(baseline)
        else:
            continue
        if std == 0:
            continue
        sigma = (current["total"] - mean) / std
        if sigma >= sigma_threshold:
            anomalies.append({
                "category": cat,
                "month_ds": current["month"],
                "actual":   round(current["total"], 2),
                "expected": round(mean, 2),
                "sigma":    round(sigma, 1),
                "method":   "zscore",
            })

    anomalies.sort(key=lambda r: r["sigma"], reverse=True)
    return anomalies
