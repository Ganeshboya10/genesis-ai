import os, sys
from pathlib import Path
os.environ["LLM_MODE"] = "echo"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import analysis
import clarity_client


def _frict(name, pct, events, **dim):
    return {name: [{"sessionsCount": 100, "sessionsWithMetricPercentage": pct, "sessionsWithoutMetricPercentage": 100 - pct,
                    "pagesViews": events, "subTotal": events, **dim}]}


def metrics(sessions=200, bots=10, scroll=45, avg_time=80, active=40, pps=1.8, script=0, rage=0, dead=0, quick=0, **dim):
    m = {"Traffic": [{"totalSessionCount": sessions, "totalBotSessionCount": bots, "distinctUserCount": sessions,
                      "pagesPerSessionPercentage": pps, **dim}],
         "ScrollDepth": [{"averageScrollDepth": scroll, **dim}],
         "EngagementTime": [{"totalTime": avg_time, "activeTime": active, **dim}]}
    for name, pct in [("ScriptErrorCount", script), ("RageClickCount", rage), ("DeadClickCount", dead),
                      ("QuickbackClick", quick), ("ErrorClickCount", 0), ("ExcessiveScroll", 0)]:
        m.update(_frict(name, pct, round(pct), **dim))
    return m


def merge(*ms):
    out = {}
    for m in ms:
        for k, rows in m.items():
            out.setdefault(k, []).extend(rows)
    return out


@pytest.fixture
def fake(monkeypatch):
    def install(totals, **breakdowns):
        def get(num_days=1, dimension=None):
            if dimension is None:
                return {"metrics": totals}
            if dimension in breakdowns:
                value = breakdowns[dimension]
                if isinstance(value, Exception):
                    raise value
                return {"metrics": value}
            raise clarity_client.ClarityError("not available")
        monkeypatch.setattr(analysis, "get_clarity_insights", get)
    return install


def topics(result):
    return {f["topic"] for f in result["findings"]}


def test_healthy_site_has_no_findings(fake):
    fake(metrics())
    r = analysis.analyze_website(1, deep=False, include_sales=False)
    assert r["confidence"] == "ok" and r["findings"] == []
    assert all("frustration" in x.lower() or "errors" in x.lower() or "scroll" in x.lower() for x in r["strengths"])  # only genuine positives


def test_script_errors_rage_and_quickbacks_flagged_with_suggestions(fake):
    fake(metrics(script=8, rage=4, quick=18, scroll=20))
    r = analysis.analyze_website(1, deep=False, include_sales=False)
    assert {"Site errors", "Frustration", "Wrong first impression", "Visitors leave early"} <= topics(r)
    assert all(f["suggestion"] and f["evidence"] for f in r["findings"])
    assert r["findings"][0]["severity"] == "high"


def test_bot_share_flagged(fake):
    fake(metrics(sessions=200, bots=70))
    r = analysis.analyze_website(1, deep=False, include_sales=False)
    assert "Traffic quality" in topics(r)


def test_small_sample_is_low_confidence_and_demoted(fake):
    fake(metrics(sessions=25, bots=0, script=20))
    r = analysis.analyze_website(1, deep=False, include_sales=False)
    assert r["confidence"].startswith("very low")
    assert all(f["severity"] == "low" for f in r["findings"]) and r["notes"]


def test_phone_only_problem_and_segments(fake):
    mobile = metrics(sessions=150, scroll=20, script=9, Device="Mobile")
    pc = metrics(sessions=40, scroll=50, script=0, Device="PC")
    fake(metrics(sessions=190, script=7), Device=merge(mobile, pc))
    r = analysis.analyze_website(1, include_sales=False)
    assert [d["name"] for d in r["devices"]] == ["Mobile", "PC"]
    assert r["devices"][0]["share_pct"] == 78.9
    assert {"Phone-only problem", "Mostly phone visitors", "Phone visitors leave sooner"} <= topics(r)
    assert any("Channel breakdown skipped" in n for n in r["notes"])  # channel call unavailable -> noted, not fatal


def test_page_and_channel_findings(fake):
    pages = merge(metrics(sessions=100, scroll=15, quick=20, script=5, URL="https://shop.com/products/vpod?x=1"),
                  metrics(sessions=30, scroll=60, URL="https://shop.com/"))
    channels = merge(metrics(sessions=150, scroll=50, Channel="Social"), metrics(sessions=30, scroll=10, bots=15, Channel="Paid"))
    fake(metrics(sessions=180, scroll=45), URL=pages, Channel=channels)
    r = analysis.analyze_website(1, include_sales=False)
    assert r["pages"][0]["name"] == "/products/vpod"            # host and query string removed
    assert {"Problem page", "Busiest page loses people", "Weak channel", "Poor-quality traffic"} <= topics(r)


def test_conversion_estimate(fake, monkeypatch):
    import shopify_client
    fake(metrics(sessions=500, bots=0))
    monkeypatch.setattr(shopify_client, "get_orders_since",
                        lambda hours: [{"total_price": "50", "cancelled_at": None}] * 3 + [{"total_price": "80", "cancelled_at": "x"}])
    r = analysis.analyze_website(1, deep=False)
    assert r["conversion"]["orders"] == 3 and r["conversion"]["rate_pct"] == 0.6
    assert "Low conversion" in topics(r)


def test_no_data(fake):
    fake({"Traffic": []})
    assert analysis.analyze_website(1, deep=False, include_sales=False)["confidence"] == "no data"
