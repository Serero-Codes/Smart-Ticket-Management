"""
forecasting.py
--------------
Trend prediction engine for Smart Ticket Management System.

Computes:
  - 14-day ticket volume forecast using linear regression on the last 30 days
  - Per-category surge detection (% change week-over-week)
  - Projected workload score and alert level
  - Day-of-week pattern analysis
"""

from datetime import datetime, timedelta
from collections import defaultdict


# ── Simple linear regression (no external deps) ─────────────────────────────

def _linear_regression(y: list[float]) -> tuple[float, float]:
    """Returns (slope, intercept) for index-based x."""
    n = len(y)
    if n < 2:
        return 0.0, float(y[0]) if y else 0.0
    x = list(range(n))
    x_mean = sum(x) / n
    y_mean = sum(y) / n
    num = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(n))
    den = sum((x[i] - x_mean) ** 2 for i in range(n))
    slope = num / den if den else 0.0
    intercept = y_mean - slope * x_mean
    return slope, intercept


def _predict(slope: float, intercept: float, x: int) -> float:
    return max(0.0, slope * x + intercept)


# ── Main forecast function ────────────────────────────────────────────────────

def build_forecast(tickets: list) -> dict:
    """
    Given a list of sqlite3.Row ticket objects, returns a forecast dict
    containing all data needed to render the forecast template.
    """
    now = datetime.now()

    # ── 1. Build daily volume map: last 30 days actual + 14 days forecast ──
    days_back = 30
    days_fwd  = 14

    actual_map: dict[str, int] = {}
    for i in range(days_back - 1, -1, -1):
        key = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        actual_map[key] = 0

    for t in tickets:
        try:
            key = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d")
            if key in actual_map:
                actual_map[key] += 1
        except Exception:
            pass

    actual_dates  = list(actual_map.keys())
    actual_values = list(actual_map.values())

    # Fit regression on last 30 days
    slope, intercept = _linear_regression(actual_values)

    # Project 14 days ahead
    forecast_dates:  list[str]   = []
    forecast_values: list[float] = []
    for i in range(1, days_fwd + 1):
        fdate = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        fval  = _predict(slope, intercept, days_back + i - 1)
        forecast_dates.append(fdate)
        forecast_values.append(round(fval, 2))

    # Combined chart labels (short format)
    def short(d: str) -> str:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%b %d")

    chart_labels   = [short(d) for d in actual_dates] + [short(d) for d in forecast_dates]
    chart_actual   = actual_values + [None] * days_fwd          # None = gap in chart
    chart_forecast = [None] * days_back + forecast_values       # overlaps last actual point

    # Smooth overlap: repeat last actual value as first forecast point
    if actual_values:
        chart_forecast[days_back - 1] = float(actual_values[-1])

    # ── 2. Week-over-week surge detection per category ──
    week1_start = now - timedelta(days=14)
    week2_start = now - timedelta(days=7)

    cat_week1: dict[str, int] = defaultdict(int)
    cat_week2: dict[str, int] = defaultdict(int)

    for t in tickets:
        try:
            ts  = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S")
            cat = t["category"]
            if week1_start <= ts < week2_start:
                cat_week1[cat] += 1
            elif week2_start <= ts <= now:
                cat_week2[cat] += 1
        except Exception:
            pass

    all_cats = sorted(set(list(cat_week1.keys()) + list(cat_week2.keys())))
    surge_data = []
    for cat in all_cats:
        prev = cat_week1.get(cat, 0)
        curr = cat_week2.get(cat, 0)
        if prev == 0:
            pct = 100.0 if curr > 0 else 0.0
        else:
            pct = round((curr - prev) / prev * 100, 1)
        if curr >= 2 and pct >= 50:
            level = "high"
        elif pct >= 20:
            level = "moderate"
        elif pct <= -20:
            level = "declining"
        else:
            level = "stable"
        surge_data.append({
            "category": cat,
            "prev_week": prev,
            "curr_week": curr,
            "change_pct": pct,
            "level": level,
        })

    # Sort: high first
    order = {"high": 0, "moderate": 1, "stable": 2, "declining": 3}
    surge_data.sort(key=lambda x: order.get(x["level"], 9))

    # ── 3. Day-of-week pattern ──
    dow_map = defaultdict(int)
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for t in tickets:
        try:
            ts = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S")
            dow_map[ts.weekday()] += 1
        except Exception:
            pass
    dow_values = [dow_map.get(i, 0) for i in range(7)]
    busiest_day = dow_names[dow_values.index(max(dow_values))] if any(dow_values) else "N/A"

    # ── 4. Projected workload score ──
    avg_last7   = sum(actual_values[-7:]) / 7 if len(actual_values) >= 7 else 0
    avg_next7   = sum(forecast_values[:7]) / 7 if forecast_values else 0
    peak_next14 = max(forecast_values) if forecast_values else 0

    if avg_next7 > avg_last7 * 1.3 or peak_next14 >= 5:
        alert_level = "high"
        alert_text  = "Significant ticket surge predicted. Consider increasing staffing."
    elif avg_next7 > avg_last7 * 1.1:
        alert_level = "moderate"
        alert_text  = "Moderate increase expected. Monitor closely over the next week."
    else:
        alert_level = "normal"
        alert_text  = "Workload is projected to remain stable."

    trend_direction = "rising" if slope > 0.05 else ("falling" if slope < -0.05 else "stable")
    total_forecast_14 = round(sum(forecast_values))

    return {
        # Chart data
        "chart_labels":   chart_labels,
        "chart_actual":   chart_actual,
        "chart_forecast": chart_forecast,
        # Surge
        "surge_data":     surge_data,
        # DoW
        "dow_labels":     dow_names,
        "dow_values":     dow_values,
        "busiest_day":    busiest_day,
        # Summary
        "avg_last7":       round(avg_last7, 1),
        "avg_next7":       round(avg_next7, 1),
        "peak_next14":     round(peak_next14, 1),
        "total_forecast_14": total_forecast_14,
        "trend_direction": trend_direction,
        "slope":           round(slope, 3),
        "alert_level":     alert_level,
        "alert_text":      alert_text,
    }
