import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

os.environ["LLM_MODE"] = "echo"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brief
import config
import shopify_client

NY = ZoneInfo("America/New_York")


def order(name, created, total, items, cancelled=None, status="paid"):
    return {"name": name, "created_at": created, "total_price": str(total), "total_discounts": "0",
            "currency": "USD", "financial_status": status, "cancelled_at": cancelled,
            "line_items": [{"title": t, "quantity": q, "price": str(p)} for t, q, p in items]}


@pytest.fixture(autouse=True)
def store_tz(monkeypatch):
    monkeypatch.setitem(shopify_client._shop_cache, "tz", NY)


# ---------- Shopify date-range summary

def test_summary_math_and_cancellations():
    orders = [
        order("#1", "2026-09-30T09:00:00-04:00", 50, [("VPOD Elite", 2, 25)]),
        order("#2", "2026-09-30T20:00:00-04:00", 30, [("VPOD Mini", 1, 30)]),
        order("#3", "2026-09-30T21:00:00-04:00", 99, [("VPOD Mini", 3, 33)], cancelled="2026-09-30T22:00:00-04:00"),
    ]
    s = shopify_client.summarize_orders(orders, date(2026, 9, 30), date(2026, 9, 30))
    assert s["order_count"] == 2
    assert s["cancelled_orders"] == 1
    assert s["total_revenue"] == 80
    assert s["average_order_value"] == 40
    assert s["units_sold"] == 3
    assert s["top_products"][0] == {"title": "VPOD Elite", "units": 2, "revenue": 50}
    assert s["by_day"] == {"2026-09-30": {"orders": 2, "revenue": 80}}


def test_by_day_uses_store_timezone():
    # 02:00 UTC on Oct 1 is still Sept 30 in New York
    s = shopify_client.summarize_orders([order("#9", "2026-10-01T02:00:00+00:00", 10, [])],
                                        date(2026, 9, 30), date(2026, 9, 30))
    assert list(s["by_day"]) == ["2026-09-30"]


def test_date_range_query_and_pagination():
    page1, page2 = MagicMock(), MagicMock()
    page1.json.return_value = {"orders": [order("#1", "2026-09-29T10:00:00-04:00", 10, [])]}
    page1.links = {"next": {"url": "https://shop/next-page"}}
    page2.json.return_value = {"orders": [order("#2", "2026-09-30T10:00:00-04:00", 20, [])]}
    page2.links = {}
    with patch("shopify_client._get", side_effect=[page1, page2]) as get:
        out = shopify_client.get_sales_summary("2026-09-29", "2026-09-30")
    first_params = get.call_args_list[0].args[1]
    assert first_params["created_at_min"] == "2026-09-29T00:00:00-04:00"
    assert first_params["created_at_max"].startswith("2026-09-30T23:59:59")
    assert get.call_args_list[1].args == ("https://shop/next-page", None)
    assert out["order_count"] == 2 and out["total_revenue"] == 30


def test_api_version_is_configurable(monkeypatch):
    monkeypatch.setattr(config, "SHOPIFY_STORE_DOMAIN", "x.myshopify.com")
    monkeypatch.setattr(config, "SHOPIFY_API_VERSION", "2026-01")
    assert shopify_client._api_url("orders.json") == "https://x.myshopify.com/admin/api/2026-01/orders.json"


def test_system_prompt_includes_today():
    import llm
    assert "Today is" in llm.system_prompt()


# ---------- Daily brief

def _sales(orders, revenue):
    return {"order_count": orders, "total_revenue": revenue, "average_order_value": revenue / orders if orders else 0,
            "units_sold": orders, "cancelled_orders": 0, "currency": "USD",
            "top_products": [{"title": "VPOD Elite", "units": orders, "revenue": revenue}],
            "sample_orders": [{"order": "#1018"}]}


def _clarity(sessions, rage):
    return {"metrics": {"Traffic": [{"totalSessionCount": sessions, "totalBotSessionCount": 2,
                                      "pagesPerSessionPercentage": 1.5}],
                        "RageClickCount": [{"subTotal": rage}]}}


def test_brief_flags_revenue_drop_and_rage_clicks():
    sales = {"2026-09-30": _sales(1, 40), "week": _sales(35, 7000)}
    with patch("brief.get_sales_summary", side_effect=lambda s, e=None: sales["2026-09-30"] if e is None else sales["week"]), \
         patch("brief.get_clarity_insights", side_effect=lambda num_days, **k: _clarity(100, 12) if num_days == 1 else _clarity(200, 13)):
        data = brief.gather_brief_data(today=date(2026, 10, 1))
    assert data["date"] == "2026-09-30"
    assert data["sales"]["revenue_vs_7_day_avg_pct"] == -96
    assert any("Revenue down" in f for f in data["flags"])
    assert any("Rage clicks jumped to 12" in f for f in data["flags"])
    assert data["website"]["previous_24h"]["sessions"] == 100
    assert not data["errors"]


def test_brief_quiet_day_has_no_flags():
    with patch("brief.get_sales_summary", side_effect=lambda s, e=None: _sales(5, 500) if e is None else _sales(35, 3500)), \
         patch("brief.get_clarity_insights", side_effect=lambda num_days, **k: _clarity(100, 0) if num_days == 1 else _clarity(205, 0)):
        data = brief.gather_brief_data(today=date(2026, 10, 1))
    assert data["flags"] == []
    assert "Nothing unusual" in brief.plain_brief(data)


def test_brief_survives_a_failing_source():
    with patch("brief.get_sales_summary", side_effect=RuntimeError("401 Unauthorized")), \
         patch("brief.get_clarity_insights", side_effect=lambda num_days, **k: _clarity(100, 0)):
        data = brief.gather_brief_data(today=date(2026, 10, 1))
    assert "Shopify data unavailable" in data["errors"][0]
    text = brief.plain_brief(data)
    assert "Sales data unavailable" in text and "Nothing unusual" not in text


def test_brief_endpoint_caches_per_day(tmp_path, monkeypatch):
    monkeypatch.setattr(brief, "CACHE_FILE", tmp_path / "brief.json")
    monkeypatch.setattr(config, "LLM_MODE", "echo")
    from fastapi.testclient import TestClient
    from main import app
    fake = {"date": "2026-09-30", "weekday": "Wednesday", "flags": [], "errors": [], "sales": None}
    with patch("brief.gather_brief_data", return_value=fake) as g, \
         patch("brief.store_today", return_value=date(2026, 10, 1)):
        client = TestClient(app)
        first = client.get("/api/brief").json()
        second = client.get("/api/brief").json()
        client.get("/api/brief?refresh=true")
    assert first["cached"] is False and second["cached"] is True
    assert g.call_count == 2
    assert "Here's yesterday, Wednesday September 30." in first["brief"]
