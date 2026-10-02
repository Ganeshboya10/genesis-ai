import time
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import requests

import config

_token_cache = {"token": None, "expires_at": 0}
_shop_cache = {"tz": None}

MAX_ORDERS = 2000   # safety cap for one date-range query (250 per page)


def _get_access_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    url = f"https://{config.SHOPIFY_STORE_DOMAIN}/admin/oauth/access_token"
    resp = requests.post(url, json={
        "client_id": config.SHOPIFY_CLIENT_ID,
        "client_secret": config.SHOPIFY_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 86400)
    return _token_cache["token"]


def _api_url(path: str) -> str:
    return f"https://{config.SHOPIFY_STORE_DOMAIN}/admin/api/{config.SHOPIFY_API_VERSION}/{path}"


def _get(url: str, params: dict | None = None) -> requests.Response:
    resp = requests.get(url, headers={"X-Shopify-Access-Token": _get_access_token()},
                        params=params, timeout=30)
    resp.raise_for_status()
    return resp


def get_store_timezone() -> ZoneInfo:
    """The store's own time zone, so 'yesterday' means Krisha's yesterday."""
    if _shop_cache["tz"] is None:
        name = config.STORE_TIMEZONE
        if not name:
            try:
                name = _get(_api_url("shop.json")).json()["shop"]["iana_timezone"]
            except Exception:
                name = "America/New_York"
        _shop_cache["tz"] = ZoneInfo(name)
    return _shop_cache["tz"]


def store_today() -> date:
    return datetime.now(get_store_timezone()).date()


def get_recent_orders(limit=10):
    return _get(_api_url("orders.json"), {"limit": min(int(limit), 250), "status": "any"}).json().get("orders", [])


ORDER_FIELDS = ("id,name,order_number,created_at,total_price,subtotal_price,total_discounts,"
                "currency,financial_status,cancelled_at,line_items,source_name")


def _fetch_orders(created_at_min: str, created_at_max: str | None = None) -> list[dict]:
    """All orders in a time window, following Shopify's page_info pagination."""
    params = {"status": "any", "limit": 250, "created_at_min": created_at_min, "fields": ORDER_FIELDS}
    if created_at_max:
        params["created_at_max"] = created_at_max
    url, orders = _api_url("orders.json"), []
    while url and len(orders) < MAX_ORDERS:
        resp = _get(url, params)
        orders.extend(resp.json().get("orders", []))
        url = resp.links.get("next", {}).get("url")
        params = None  # the next-page URL already carries page_info
    return orders


def get_orders_between(start: date, end: date) -> list[dict]:
    """All orders created from the start of `start` to the end of `end` (inclusive),
    in the store's time zone."""
    tz = get_store_timezone()
    return _fetch_orders(datetime.combine(start, dtime.min, tz).isoformat(),
                         datetime.combine(end, dtime.max, tz).isoformat())


def get_orders_since(hours: float) -> list[dict]:
    """Orders created in the last `hours` hours (a rolling window ending now)."""
    return _fetch_orders((datetime.now(get_store_timezone()) - timedelta(hours=hours)).isoformat())


def summarize_orders(orders: list[dict], start: date, end: date, sample_size: int = 15) -> dict:
    """Do the arithmetic in Python so the model never has to add up orders itself."""
    tz = get_store_timezone()
    live = [o for o in orders if not o.get("cancelled_at")]
    cancelled = len(orders) - len(live)
    currency = next((o.get("currency") for o in orders if o.get("currency")), None)

    revenue = sum(float(o.get("total_price") or 0) for o in live)
    discounts = sum(float(o.get("total_discounts") or 0) for o in live)
    refunded = sum(1 for o in live if o.get("financial_status") in ("refunded", "partially_refunded"))

    products = defaultdict(lambda: {"units": 0, "revenue": 0.0})
    by_day = defaultdict(lambda: {"orders": 0, "revenue": 0.0})
    for o in live:
        for li in o.get("line_items", []):
            p = products[li.get("title") or "Unknown"]
            qty = int(li.get("quantity") or 0)
            p["units"] += qty
            p["revenue"] += float(li.get("price") or 0) * qty
        day = datetime.fromisoformat(o["created_at"]).astimezone(tz).date().isoformat()
        by_day[day]["orders"] += 1
        by_day[day]["revenue"] += float(o.get("total_price") or 0)

    top = sorted(products.items(), key=lambda kv: kv[1]["revenue"], reverse=True)[:10]
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "timezone": str(tz),
        "currency": currency,
        "order_count": len(live),
        "cancelled_orders": cancelled,
        "refunded_or_partially_refunded_orders": refunded,
        "total_revenue": round(revenue, 2),
        "total_discounts": round(discounts, 2),
        "average_order_value": round(revenue / len(live), 2) if live else 0,
        "units_sold": sum(p["units"] for p in products.values()),
        "top_products": [{"title": t, "units": v["units"], "revenue": round(v["revenue"], 2)} for t, v in top],
        "by_day": {d: {"orders": v["orders"], "revenue": round(v["revenue"], 2)} for d, v in sorted(by_day.items())},
        "truncated": len(orders) >= MAX_ORDERS,
        "sample_orders": [
            {"order": o.get("name") or o.get("order_number"), "created_at": o.get("created_at"),
             "total": o.get("total_price"), "status": o.get("financial_status")}
            for o in sorted(live, key=lambda o: o["created_at"], reverse=True)[:sample_size]
        ],
    }


def get_sales_summary(start_date: str, end_date: str | None = None) -> dict:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date) if end_date else start
    if end < start:
        start, end = end, start
    return summarize_orders(get_orders_between(start, end), start, end)
