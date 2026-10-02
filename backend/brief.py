"""Daily founder brief: yesterday's business in one message.

Python gathers the numbers and flags anything unusual (deterministic, testable);
Claude only turns that into a short, readable brief. The result is cached per day
so reloading the page doesn't re-spend Clarity calls or API tokens.

Run from the backend folder:  python3 brief.py          (prints today's brief)
                              python3 brief.py --refresh (regenerates it)
"""
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

import config
from clarity_client import get_clarity_insights
from shopify_client import get_sales_summary, get_store_timezone, store_today

log = logging.getLogger("founder-assistant")
CACHE_FILE = Path(__file__).resolve().parent / ".brief_cache.json"

# How big a change has to be before the brief calls it out.
SALES_CHANGE_THRESHOLD = 0.30     # ±30% vs the previous 7-day daily average
SESSIONS_CHANGE_THRESHOLD = 0.30  # ±30% vs the previous 24 hours
FRICTION_METRICS = {              # Clarity metric -> plain-English name
    "RageClickCount": "rage clicks",
    "DeadClickCount": "dead clicks",
    "ErrorClickCount": "error clicks",
    "ScriptErrorCount": "script errors",
    "QuickbackClick": "quickbacks",
    "ExcessiveScroll": "excessive scrolling",
}

BRIEF_PROMPT = """Write the founder's morning brief from the JSON data below.

Format (plain text, no markdown symbols like ** or #):
Good morning — here's {date_label}.

SALES
• 2-4 bullets: orders, revenue, AOV, top products, and how it compares with the 7-day average.

WEBSITE (last 24h)
• 2-3 bullets: sessions vs the previous 24h, engagement, anything notable.

SUGGESTIONS
• 1-2 bullets: the actions in "website_findings", in plain words, for today. If that list is missing or empty, write "• No changes needed today."

NEEDS ATTENTION
• Only the items in "flags" and "errors", most important first, each with a concrete next step. If both are empty, write "• Nothing unusual."

Rules: use only numbers from the data, round money to whole dollars unless under $100, keep it under 190 words, and if a section's data is missing say so in one bullet."""


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _pct_change(new: float, old: float) -> float | None:
    return None if not old else (new - old) / old


def _sum_field(rows: list[dict], field: str) -> float:
    return sum(_num(r.get(field)) for r in rows)


def _clarity_snapshot() -> dict:
    """Site-wide Clarity totals for the last 24h and the 24h before that.
    Clarity can't query a specific past day, so 'previous 24h' = last 48h minus last 24h."""
    last24 = get_clarity_insights(num_days=1)["metrics"]
    last48 = get_clarity_insights(num_days=2)["metrics"]

    def totals(metrics):
        t = {
            "sessions": _sum_field(metrics.get("Traffic", []), "totalSessionCount"),
            "bot_sessions": _sum_field(metrics.get("Traffic", []), "totalBotSessionCount"),
        }
        for key in FRICTION_METRICS:
            t[key] = _sum_field(metrics.get(key, []), "subTotal")
        return t

    now, both = totals(last24), totals(last48)
    prev = {k: max(both[k] - now[k], 0) for k in now}
    extras = {}
    if metrics := last24.get("ScrollDepth"):
        extras["average_scroll_depth_pct"] = round(_num(metrics[0].get("averageScrollDepth")), 1)
    if metrics := last24.get("EngagementTime"):
        extras["engagement_total_time_s"] = _num(metrics[0].get("totalTime"))
        extras["engagement_active_time_s"] = _num(metrics[0].get("activeTime"))
    if metrics := last24.get("Traffic"):
        extras["pages_per_session"] = round(_num(metrics[0].get("pagesPerSessionPercentage")), 2)
    return {"last_24h": now, "previous_24h": prev, **extras}


def gather_brief_data(today=None) -> dict:
    """Collect yesterday's numbers and work out what's unusual."""
    today = today or store_today()
    yesterday = today - timedelta(days=1)
    data = {"date": yesterday.isoformat(), "weekday": yesterday.strftime("%A"), "flags": [], "errors": []}

    # --- Shopify: yesterday vs the 7 days before it
    try:
        y = get_sales_summary(yesterday.isoformat())
        week = get_sales_summary((yesterday - timedelta(days=7)).isoformat(),
                                 (yesterday - timedelta(days=1)).isoformat())
        avg_orders = week["order_count"] / 7
        avg_revenue = week["total_revenue"] / 7
        data["sales"] = {
            "yesterday": {k: y[k] for k in ("order_count", "total_revenue", "average_order_value",
                                            "units_sold", "cancelled_orders", "currency")},
            "top_products": y["top_products"][:3],
            "prior_7_day_avg": {"orders_per_day": round(avg_orders, 1), "revenue_per_day": round(avg_revenue, 2)},
            "orders": [o["order"] for o in y["sample_orders"][:5]],
        }
        change = _pct_change(y["total_revenue"], avg_revenue)
        data["sales"]["revenue_vs_7_day_avg_pct"] = None if change is None else round(change * 100)
        if y["order_count"] == 0 and avg_orders >= 1:
            data["flags"].append(f"No orders yesterday, versus about {avg_orders:.1f}/day the week before.")
        elif change is not None and abs(change) >= SALES_CHANGE_THRESHOLD:
            direction = "up" if change > 0 else "down"
            data["flags"].append(f"Revenue {direction} {abs(change)*100:.0f}% vs the 7-day average "
                                 f"(${y['total_revenue']:,.2f} vs ${avg_revenue:,.2f}/day).")
        if y["cancelled_orders"]:
            data["flags"].append(f"{y['cancelled_orders']} order(s) cancelled yesterday.")
    except Exception as e:
        log.warning("brief: Shopify data failed: %s", e)
        data["errors"].append(f"Shopify data unavailable: {e}")

    # --- Clarity: last 24h vs the 24h before
    try:
        c = _clarity_snapshot()
        data["website"] = c
        now, prev = c["last_24h"], c["previous_24h"]
        change = _pct_change(now["sessions"], prev["sessions"])
        c["sessions_change_pct"] = None if change is None else round(change * 100)
        if change is not None and abs(change) >= SESSIONS_CHANGE_THRESHOLD and prev["sessions"] >= 20:
            direction = "up" if change > 0 else "down"
            data["flags"].append(f"Website sessions {direction} {abs(change)*100:.0f}% "
                                 f"({now['sessions']:.0f} vs {prev['sessions']:.0f} the previous 24h).")
        for key, label in FRICTION_METRICS.items():
            n, p = now.get(key, 0), prev.get(key, 0)
            if n >= 5 and n >= 2 * max(p, 1):
                data["flags"].append(f"{label.capitalize()} jumped to {n:.0f} (from {p:.0f}) — check Clarity recordings.")
        if now["sessions"] and now["bot_sessions"] / now["sessions"] > 0.3:
            data["flags"].append(f"{now['bot_sessions']/now['sessions']*100:.0f}% of sessions were bots.")
    except Exception as e:
        log.warning("brief: Clarity data failed: %s", e)
        data["errors"].append(f"Clarity data unavailable: {e}")
    else:
        # Plain-language actions from the website analysis. Uses the Clarity data fetched above (cached), so no extra calls.
        try:
            from analysis import analyze_website
            found = analyze_website(1, deep=False, include_sales=False)
            data["website_findings"] = [{"finding": f["finding"], "suggestion": f["suggestion"]}
                                        for f in found.get("findings", [])[:2]]
            data["website_confidence"] = found.get("confidence")
        except Exception as e:
            log.info("brief: website analysis skipped: %s", e)

    return data


def _date_label(data: dict) -> str:
    """'yesterday, Wednesday September 30' — friendlier than an ISO date."""
    d = datetime.fromisoformat(data["date"])
    return f"yesterday, {d:%A %B} {d.day}"


def plain_brief(data: dict) -> str:
    """No-AI version: used in echo mode and as a fallback if the LLM call fails."""
    lines = [f"Here's {_date_label(data)}.", "", "SALES"]
    if s := data.get("sales"):
        y, avg = s["yesterday"], s["prior_7_day_avg"]
        lines.append(f"• {y['order_count']} orders, ${y['total_revenue']:,.2f} revenue, AOV ${y['average_order_value']:,.2f}")
        lines.append(f"• 7-day average: {avg['orders_per_day']} orders, ${avg['revenue_per_day']:,.2f}/day")
        if s["top_products"]:
            lines.append("• Top: " + ", ".join(f"{p['title']} ({p['units']})" for p in s["top_products"]))
    else:
        lines.append("• Sales data unavailable.")
    lines += ["", "WEBSITE (last 24h)"]
    if w := data.get("website"):
        lines.append(f"• {w['last_24h']['sessions']:.0f} sessions vs {w['previous_24h']['sessions']:.0f} the previous 24h")
    else:
        lines.append("• Website data unavailable.")
    if tips := data.get("website_findings"):
        lines += ["", "SUGGESTIONS"] + [f"• {t['suggestion']}" for t in tips]
    attention = [f"• {f}" for f in data["flags"]] + [f"• {e}" for e in data["errors"]]
    lines += ["", "NEEDS ATTENTION"] + (attention or ["• Nothing unusual."])
    return "\n".join(lines)


def write_brief(data: dict) -> str:
    if config.LLM_MODE != "anthropic":
        return plain_brief(data)
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=config.LLM_MODEL,
            max_tokens=config.LLM_MAX_TOKENS,
            system=config.SYSTEM_PROMPT,
            messages=[{"role": "user", "content":
                       BRIEF_PROMPT.replace("{date_label}", _date_label(data)) +
                       "\n\nDATA:\n" + json.dumps(data, indent=1)}],
        )
        for block in response.content:
            if block.type == "text":
                return block.text
    except Exception as e:
        log.warning("brief: LLM failed, using plain brief: %s", e)
    return plain_brief(data)


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_brief(refresh: bool = False) -> dict:
    today = store_today().isoformat()
    cache = _load_cache()
    if not refresh and today in cache:
        return {**cache[today], "cached": True}
    data = gather_brief_data()
    result = {
        "date": today,
        "brief": write_brief(data),
        "flags": data["flags"],
        "errors": data["errors"],
        "generated_at": datetime.now(get_store_timezone()).isoformat(timespec="minutes"),
    }
    CACHE_FILE.write_text(json.dumps({today: result}))  # keep only today's
    return {**result, "cached": False}


if __name__ == "__main__":
    print(get_brief(refresh="--refresh" in sys.argv)["brief"])
