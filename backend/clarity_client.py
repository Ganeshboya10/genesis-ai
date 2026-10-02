"""Microsoft Clarity Data Export API client.

Clarity only allows ~10 API calls per project per day and only covers the
last 1-3 days, so every response is cached to a local JSON file and a daily
call counter stops us before we hit Clarity's limit.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import config

API_URL = "https://www.clarity.ms/export-data/api/v1/project-live-insights"
ALLOWED_DIMENSIONS = {
    "Browser", "Device", "Country/Region", "OS",
    "Source", "Medium", "Campaign", "Channel", "URL",
}
CACHE_FILE = Path(__file__).resolve().parent / ".clarity_cache.json"
CACHE_TTL_SECONDS = 3 * 60 * 60   # reuse a result for 3 hours
DAILY_CALL_LIMIT = 9              # stay one under Clarity's 10/day
MAX_ROWS_PER_METRIC = 25          # keep tool output small for the LLM


class ClarityError(Exception):
    pass


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache))


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_clarity_insights(num_days: int = 1, dimension: str | None = None) -> dict:
    """Return Clarity metrics for the last `num_days` (1-3), optionally broken
    down by one dimension (e.g. "Device", "Source", "URL")."""
    if not config.CLARITY_API_TOKEN:
        raise ClarityError("CLARITY_API_TOKEN is not set in .env.")
    if num_days not in (1, 2, 3):
        raise ClarityError("num_days must be 1, 2 or 3 (Clarity only keeps the last 72 hours).")
    if dimension and dimension not in ALLOWED_DIMENSIONS:
        raise ClarityError(f"dimension must be one of {sorted(ALLOWED_DIMENSIONS)}.")

    cache = _load_cache()
    key = f"{num_days}|{dimension or ''}"
    entry = cache.get("results", {}).get(key)
    now = time.time()
    if entry and now - entry["fetched_at"] < CACHE_TTL_SECONDS:
        return {**entry["data"], "cached": True,
                "fetched_minutes_ago": int((now - entry["fetched_at"]) / 60)}

    calls = cache.get("calls", {})
    if calls.get("date") != _today_utc():
        calls = {"date": _today_utc(), "count": 0}
    if calls["count"] >= DAILY_CALL_LIMIT:
        if entry:  # stale but better than nothing
            return {**entry["data"], "cached": True, "stale": True,
                    "fetched_minutes_ago": int((now - entry["fetched_at"]) / 60)}
        raise ClarityError("Daily Clarity API limit reached; try again tomorrow (UTC).")

    params = {"numOfDays": num_days}
    if dimension:
        params["dimension1"] = dimension
    resp = requests.get(
        API_URL,
        headers={"Authorization": f"Bearer {config.CLARITY_API_TOKEN}",
                 "Content-Type": "application/json"},
        params=params,
        timeout=30,
    )
    calls["count"] += 1
    cache["calls"] = calls
    _save_cache(cache)

    if resp.status_code == 429:
        raise ClarityError("Clarity says the daily API limit (10 calls) is used up.")
    if resp.status_code == 401:
        raise ClarityError("Clarity rejected the API token (401). Check CLARITY_API_TOKEN.")
    resp.raise_for_status()

    metrics = {}
    for m in resp.json():
        rows = m.get("information", [])
        metrics[m.get("metricName", "Unknown")] = rows[:MAX_ROWS_PER_METRIC]

    data = {
        "period": f"last {num_days * 24} hours (UTC)",
        "dimension": dimension,
        "metrics": metrics,
    }
    cache.setdefault("results", {})[key] = {"fetched_at": now, "data": data}
    _save_cache(cache)
    return {**data, "cached": False}
