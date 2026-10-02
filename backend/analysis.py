"""Website analysis: turns raw Clarity numbers into findings and plain-language suggestions.

Python does the maths and the pattern-spotting (so the numbers are right and the same
every time); Claude then explains the findings to Kesha and picks the top few actions.

  analyze_website(num_days=1)                      full analysis: totals + device, channel and page breakdowns
  analyze_website(num_days=1, deep=False, ...)     totals only (uses no extra Clarity calls; used by the brief)

Clarity allows ~10 calls a day, so every breakdown here is a cached call (see clarity_client).
"""
import logging
from urllib.parse import urlparse

from clarity_client import ClarityError, get_clarity_insights

log = logging.getLogger("founder-assistant")

# Friction signals: Clarity metric -> plain-English name
FRICTION = {
    "ScriptErrorCount": "script errors",
    "RageClickCount": "rage clicks",
    "DeadClickCount": "dead clicks",
    "ErrorClickCount": "error clicks",
    "QuickbackClick": "quickbacks",
    "ExcessiveScroll": "excessive scrolling",
}
NUMERIC_KEYS = {
    "totalSessionCount", "totalBotSessionCount", "distinctUserCount", "distantUserCount",
    "pagesPerSessionPercentage", "averageScrollDepth", "totalTime", "activeTime",
    "sessionsCount", "sessionsWithMetricPercentage", "sessionsWithoutMetricPercentage",
    "pagesViews", "subTotal",
}
SEVERITY = {"high": 3, "medium": 2, "low": 1}
MAX_FINDINGS = 8


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _mean(rows: list[dict], field: str) -> float:
    vals = [_num(r.get(field)) for r in rows if r.get(field) is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _dim_key(metrics: dict) -> str | None:
    """The column that holds the breakdown value (Device, Channel, URL...)."""
    for rows in metrics.values():
        for row in rows:
            for key in row:
                if key not in NUMERIC_KEYS:
                    return key
    return None


def _tidy_url(url: str) -> str:
    if not url:
        return "(unknown)"
    path = urlparse(url).path if "://" in url else url.split("?")[0]
    return (path or "/")[:80]


def _segments(metrics: dict, dim: str | None) -> list[dict]:
    """One stats dict per breakdown value (or a single 'All visitors' row when there is no breakdown)."""
    def group(name):
        rows = metrics.get(name, [])
        if dim is None:
            return {"all": rows}
        out = {}
        for r in rows:
            out.setdefault(str(r.get(dim, "(unknown)")), []).append(r)
        return out

    traffic, scroll, engage = group("Traffic"), group("ScrollDepth"), group("EngagementTime")
    friction = {name: group(name) for name in FRICTION}
    total_sessions = sum(_num(r.get("totalSessionCount")) for rows in traffic.values() for r in rows) or 1

    segments = []
    for key, rows in traffic.items():
        sessions = sum(_num(r.get("totalSessionCount")) for r in rows)
        bots = sum(_num(r.get("totalBotSessionCount")) for r in rows)
        avg_time = _mean(engage.get(key, []), "totalTime")   # Clarity reports these as per-session averages (seconds)
        active = _mean(engage.get(key, []), "activeTime")
        label = _tidy_url(key) if dim == "URL" else ("All visitors" if key == "all" else key)
        segments.append({
            "name": label,
            "sessions": int(sessions),
            "share_pct": round(sessions / total_sessions * 100, 1),
            "bot_pct": round(bots / sessions * 100, 1) if sessions else 0.0,
            "users": int(sum(_num(r.get("distinctUserCount", r.get("distantUserCount"))) for r in rows)),
            "pages_per_session": round(_mean(rows, "pagesPerSessionPercentage"), 2),
            "scroll_depth_pct": round(_mean(scroll.get(key, []), "averageScrollDepth"), 1),
            "avg_time_s": round(avg_time, 1),
            "active_time_s": round(active, 1),
            "active_share_pct": round(active / avg_time * 100, 1) if avg_time else 0.0,
            "friction_pct": {FRICTION[m]: round(_mean(friction[m].get(key, []), "sessionsWithMetricPercentage"), 1)
                             for m in FRICTION},
            "friction_events": {FRICTION[m]: int(sum(_num(r.get("subTotal")) for r in friction[m].get(key, [])))
                                for m in FRICTION},
        })
    segments = [s for s in segments if s["sessions"] > 0]
    segments.sort(key=lambda s: s["sessions"], reverse=True)
    return segments


def _confidence(sessions: int) -> str:
    if sessions < 30:
        return "very low (under 30 sessions: patterns could be chance)"
    if sessions < 100:
        return "low (under 100 sessions: treat as directional, not proof)"
    return "ok"


def _finding(severity, topic, finding, evidence, suggestion):
    return {"severity": severity, "topic": topic, "finding": finding, "evidence": evidence, "suggestion": suggestion}


def _site_findings(site: dict) -> list[dict]:
    f, ev, out = site["friction_pct"], site["friction_events"], []
    n = site["sessions"]

    if f["script errors"] >= 2:
        out.append(_finding("high" if f["script errors"] >= 5 else "medium", "Site errors",
            f"{f['script errors']:.0f}% of visits hit a script error (something on the page failed to run).",
            f"{ev['script errors']} errors across {n} sessions.",
            "Ask whoever looks after the Shopify theme (or the support team of your newest app) to check the page for errors. "
            "In Clarity, filter recordings by 'Script errors' and watch two or three: recently added apps or theme edits are the usual cause."))
    if f["rage clicks"] >= 1:
        out.append(_finding("high" if f["rage clicks"] >= 3 else "medium", "Frustration",
            f"{f['rage clicks']:.1f}% of visits include rage clicks (rapid repeated clicking on something that isn't working).",
            f"{ev['rage clicks']} rage-click events.",
            "Watch the rage-click recordings in Clarity. Usual culprits: size/variant pickers, the Add to cart button, image zoom, discount-code boxes."))
    if f["dead clicks"] >= 10:
        out.append(_finding("high" if f["dead clicks"] >= 20 else "medium", "Confusing layout",
            f"{f['dead clicks']:.0f}% of visits click things that do nothing (dead clicks).",
            f"{ev['dead clicks']} dead-click events.",
            "Find what people expect to be clickable (product photos, badges, 'as seen in' logos) and either link it or restyle it so it doesn't look like a button."))
    if f["error clicks"] >= 3:
        out.append(_finding("medium", "Site errors",
            f"{f['error clicks']:.0f}% of visits click right before an error appears.",
            f"{ev['error clicks']} error-click events.",
            "Watch the recordings filtered by 'Error clicks' to see which button triggers the error."))
    if f["quickbacks"] >= 8:
        out.append(_finding("high" if f["quickbacks"] >= 15 else "medium", "Wrong first impression",
            f"{f['quickbacks']:.0f}% of visits hit Back almost immediately after landing (quickbacks).",
            f"{ev['quickbacks']} quickbacks.",
            "Check that the landing page matches what the ad or link promised, loads fast on a phone, and shows the product, price and Add to cart without scrolling."))
    if n >= 20 and site["scroll_depth_pct"] and site["scroll_depth_pct"] < 40:
        out.append(_finding("high" if site["scroll_depth_pct"] < 25 else "low", "Visitors leave early",
            f"On average visitors only scroll {site['scroll_depth_pct']:.0f}% of the page.",
            f"Average scroll depth {site['scroll_depth_pct']:.0f}%.",
            "Put the key benefit, price and Add to cart in the first screen. Move the long story lower down, and test one change at a time."))
    if n >= 20 and site["avg_time_s"] and site["avg_time_s"] < 30:
        out.append(_finding("medium", "Short visits",
            f"The average visit lasts only {site['avg_time_s']:.0f} seconds ({site['active_time_s']:.0f}s of it active).",
            f"{site['active_share_pct']:.0f}% of time on site is active.",
            "Open with a clear hero image and a one-line promise, and add a short 'how it works' near the top to hold attention."))
    if n >= 20 and site["pages_per_session"] and site["pages_per_session"] < 1.3:
        out.append(_finding("medium", "One-page visits",
            f"Most visitors see just one page ({site['pages_per_session']:.2f} pages per visit).",
            f"{site['pages_per_session']:.2f} pages per session.",
            "Add clear next steps: a 'Shop all' button, related products and a visible link to the other product ranges."))
    if site["bot_pct"] >= 12:
        out.append(_finding("high" if site["bot_pct"] >= 25 else "medium", "Traffic quality",
            f"{site['bot_pct']:.0f}% of sessions are bots, not shoppers.",
            f"{round(n * site['bot_pct'] / 100)} of {n} sessions.",
            "Look at which traffic source the bots come from (Channel breakdown) and pause or tighten it if it's paid traffic. Judge conversion on real visitors only."))
    return out


def _segment_findings(devices: list[dict], channels: list[dict], pages: list[dict], site: dict) -> list[dict]:
    out = []
    # ---- devices
    by_name = {d["name"].lower(): d for d in devices}
    mobile, pc = by_name.get("mobile"), by_name.get("pc") or by_name.get("desktop")
    if mobile and mobile["share_pct"] >= 60:
        out.append(_finding("low", "Mostly phone visitors",
            f"{mobile['share_pct']:.0f}% of visitors are on a phone.",
            f"Mobile {mobile['sessions']} of {site['sessions']} sessions.",
            "Check every key page on a real phone first: tap targets, photo size, and the Add to cart button should all be reachable with a thumb."))
    if mobile and pc and mobile["sessions"] >= 20 and pc["sessions"] >= 10:
        gap = mobile["friction_pct"]["script errors"] - pc["friction_pct"]["script errors"]
        if gap >= 3:
            out.append(_finding("high", "Phone-only problem",
                f"Script errors hit {mobile['friction_pct']['script errors']:.0f}% of phone visits versus {pc['friction_pct']['script errors']:.0f}% on computers.",
                f"Mobile {mobile['sessions']} sessions, PC {pc['sessions']}.",
                "The bug is specific to phones. Test the store on an iPhone and an Android phone and ask your theme developer to check mobile scripts."))
        if mobile["scroll_depth_pct"] and pc["scroll_depth_pct"] and mobile["scroll_depth_pct"] < pc["scroll_depth_pct"] * 0.6:
            out.append(_finding("medium", "Phone visitors leave sooner",
                f"Phone visitors scroll {mobile['scroll_depth_pct']:.0f}% of the page versus {pc['scroll_depth_pct']:.0f}% on computers.",
                f"Mobile {mobile['scroll_depth_pct']:.0f}% vs PC {pc['scroll_depth_pct']:.0f}% scroll depth.",
                "Shorten the mobile layout: smaller hero image, key benefit and button above the fold, collapse long text into tabs."))
    # ---- channels
    if channels:
        top = channels[0]
        if len(channels) > 1 and top["share_pct"] >= 60 and top["sessions"] >= 20:
            out.append(_finding("low", "One main traffic source",
                f"{top['share_pct']:.0f}% of visits come from {top['name']}.",
                f"{top['sessions']} of {site['sessions']} sessions.",
                "That's a lot riding on one source. Pick one second channel (email list, Instagram, search) to grow so a change in one doesn't hit sales."))
        for c in channels:
            if c["sessions"] >= 20 and c["bot_pct"] >= 25:
                out.append(_finding("medium", "Poor-quality traffic",
                    f"{c['bot_pct']:.0f}% of visits from {c['name']} are bots.",
                    f"{c['sessions']} sessions from {c['name']}.",
                    f"Review where {c['name']} traffic comes from. If it's paid, pause or tighten targeting before spending more."))
        base = site["scroll_depth_pct"]
        for c in channels:
            if c["sessions"] >= 20 and base and c["scroll_depth_pct"] and c["scroll_depth_pct"] < base * 0.6:
                out.append(_finding("medium", "Weak channel",
                    f"Visitors from {c['name']} engage much less: {c['scroll_depth_pct']:.0f}% scroll depth versus {base:.0f}% site-wide.",
                    f"{c['sessions']} sessions.",
                    f"Check what the {c['name']} link or ad promises versus the page it lands on. Send it to a page that matches the message."))
                break
    # ---- pages
    scored = []
    for p in pages:
        if p["sessions"] < 10:
            continue
        fp = p["friction_pct"]
        score = fp["script errors"] + fp["rage clicks"] + fp["dead clicks"] + fp["quickbacks"] + fp["error clicks"]
        scored.append((score, p))
    if scored:
        score, worst = max(scored, key=lambda x: x[0])
        if score >= 10:
            bits = [f"{k} {v:.0f}%" for k, v in worst["friction_pct"].items() if v >= 3 and k != "excessive scrolling"]
            out.append(_finding("medium", "Problem page",
                f"{worst['name']} has the most trouble spots ({', '.join(bits) or 'several issues'} of visits).",
                f"{worst['sessions']} sessions on that page.",
                f"Open Clarity recordings for {worst['name']} and watch a few. Fixing the busiest problem page gives the best return."))
    if pages:
        busiest = pages[0]
        if busiest["sessions"] >= 20 and busiest["scroll_depth_pct"] and busiest["scroll_depth_pct"] < 25:
            out.append(_finding("high", "Busiest page loses people",
                f"Your busiest page ({busiest['name']}, {busiest['share_pct']:.0f}% of visits) only gets scrolled {busiest['scroll_depth_pct']:.0f}% on average.",
                f"{busiest['sessions']} sessions.",
                f"Improve what's visible at the top of {busiest['name']}: clear promise, product photo, price, button."))
    return out


def _conversion_findings(conv: dict, human_sessions: int) -> list[dict]:
    rate = conv["rate_pct"]
    if human_sessions < 100:
        return []
    if conv["orders"] == 0:
        return [_finding("high", "No sales from this traffic",
            f"{human_sessions} real visitors produced no orders in this period.",
            f"0 orders vs {human_sessions} human sessions.",
            "Place a test order yourself right now on a phone to confirm checkout works. Then look at where visitors drop off.")]
    if rate < 1.0:
        return [_finding("high", "Low conversion",
            f"Only {rate:.1f}% of visitors bought ({conv['orders']} orders from {human_sessions} real sessions). Online stores typically see around 1-3%.",
            f"{conv['orders']} orders, {human_sessions} human sessions.",
            "Show shipping cost and delivery time early, put reviews or credentials (e.g. doctor-developed, FDA registered) next to Add to cart, and test the checkout on a phone.")]
    if rate < 1.5:
        return [_finding("medium", "Conversion has room to grow",
            f"{rate:.1f}% of visitors bought ({conv['orders']} orders from {human_sessions} real sessions).",
            f"{conv['orders']} orders, {human_sessions} human sessions.",
            "Small wins: trust signals next to Add to cart, clearer shipping info, and a simple offer for first-time buyers.")]
    return []


def _strengths(site: dict, conv: dict | None) -> list[str]:
    out = []
    if site["sessions"] >= 20 and site["scroll_depth_pct"] >= 55:
        out.append(f"Visitors read deep: {site['scroll_depth_pct']:.0f}% average scroll depth.")
    if site["sessions"] >= 100 and max(site["friction_pct"].values()) < 1:
        out.append("No sign of frustration: almost no rage clicks, dead clicks or errors.")
    if conv and conv["rate_pct"] >= 2.5 and conv["human_sessions"] >= 100:
        out.append(f"Conversion is strong at {conv['rate_pct']:.1f}% of real visitors.")
    if site["sessions"] >= 20 and site["pages_per_session"] >= 2:
        out.append(f"Visitors explore: {site['pages_per_session']:.1f} pages per visit.")
    return out[:2]


def analyze_website(num_days: int = 1, deep: bool = True, include_sales: bool = True) -> dict:
    """Analyse the last `num_days` (1-3) of Clarity data. `deep` adds device/channel/page breakdowns;
    `include_sales` adds a conversion estimate from Shopify orders in the same rolling window."""
    totals = get_clarity_insights(num_days=num_days)["metrics"]
    site_rows = _segments(totals, None)
    if not site_rows or not site_rows[0]["sessions"]:
        return {"period": f"last {num_days * 24} hours (UTC)", "confidence": "no data",
                "notes": ["Clarity returned no sessions for this period."], "findings": [], "strengths": []}
    site = site_rows[0]
    notes = []

    def breakdown(dimension, limit):
        try:
            metrics = get_clarity_insights(num_days=num_days, dimension=dimension)["metrics"]
            return _segments(metrics, _dim_key(metrics))[:limit]
        except ClarityError as e:
            notes.append(f"{dimension} breakdown skipped: {e}")
        except Exception as e:
            log.warning("analysis: %s breakdown failed: %s", dimension, e)
            notes.append(f"{dimension} breakdown unavailable.")
        return []

    devices = breakdown("Device", 5) if deep else []
    channels = breakdown("Channel", 6) if deep else []
    pages = breakdown("URL", 8) if deep else []

    human = max(site["sessions"] - round(site["sessions"] * site["bot_pct"] / 100), 0)
    conversion = None
    if include_sales:
        try:
            from shopify_client import get_orders_since
            orders = [o for o in get_orders_since(num_days * 24) if not o.get("cancelled_at")]
            revenue = round(sum(_num(o.get("total_price")) for o in orders), 2)
            conversion = {
                "orders": len(orders), "revenue": revenue, "human_sessions": human,
                "rate_pct": round(len(orders) / human * 100, 2) if human else 0.0,
                "revenue_per_session": round(revenue / human, 2) if human else 0.0,
                "note": "Orders in the same rolling window as the Clarity data. Approximate: Clarity counts visits, Shopify counts orders.",
            }
        except Exception as e:
            log.warning("analysis: Shopify conversion failed: %s", e)
            notes.append("Conversion estimate unavailable (couldn't read Shopify orders).")

    findings = _site_findings(site) + _segment_findings(devices, channels, pages, site)
    if conversion:
        findings += _conversion_findings(conversion, human)
    confidence = _confidence(site["sessions"])
    if confidence.startswith("very low"):
        for f in findings:
            f["severity"] = "low"
        notes.append("Very few sessions: every finding is marked low priority because it could be chance.")
    findings.sort(key=lambda f: -SEVERITY[f["severity"]])

    return {
        "period": f"last {num_days * 24} hours (UTC)",
        "confidence": confidence,
        "site": site,
        "devices": devices,
        "channels": channels,
        "pages": pages,
        "conversion": conversion,
        "findings": findings[:MAX_FINDINGS],
        "strengths": _strengths(site, conversion),
        "notes": notes,
    }
