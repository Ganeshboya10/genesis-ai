import json
import logging
import anthropic
import config
from datetime import datetime
from shopify_client import get_recent_orders, get_sales_summary, get_store_timezone
from clarity_client import ALLOWED_DIMENSIONS, get_clarity_insights
import artifacts
from analysis import analyze_website

log = logging.getLogger("founder-assistant")

class LLMError(Exception):
    """Error with a message that is safe to show the user."""

TOOLS = [
    {
        "name": "get_sales_summary",
        "description": (
            "Get a Shopify sales summary for a date range in the store's time zone: order count, "
            "revenue, average order value, discounts, cancellations, units sold, top products, a "
            "per-day breakdown and a sample of orders. Use this for any question about a period "
            "(yesterday, last week, this month, last 30 days) and call it once per period for comparisons. "
            "All totals are pre-computed; quote them rather than adding up orders yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "First day, YYYY-MM-DD (inclusive)."},
                "end_date": {"type": "string", "description": "Last day, YYYY-MM-DD (inclusive). Omit for a single day."}
            },
            "required": ["start_date"]
        }
    },
    {
        "name": "get_recent_orders",
        "description": "Get the most recent N orders from the Shopify store, including order number, date, total price, currency, payment status, and line items. Use this for questions about the latest specific orders; use get_sales_summary for anything about a time period.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many recent orders to fetch. Defaults to 10 if not specified."
                }
            }
        }
    },
    {
        "name": "get_clarity_insights",
        "description": (
            "Get Microsoft Clarity website analytics for the store for the last 1-3 days: "
            "sessions, bot sessions, unique users, pages per session, scroll depth, engagement time, "
            "rage clicks, dead clicks, quickbacks, excessive scrolling and script errors. "
            "Optionally break results down by one dimension. Use this for questions about website "
            "traffic, visitor behaviour, UX problems or where visitors come from. "
            "Clarity only allows ~10 calls per day, so prefer one broad call; results are cached for 3 hours."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "num_days": {
                    "type": "integer",
                    "enum": [1, 2, 3],
                    "description": "How many days back: 1 = last 24h, 2 = last 48h, 3 = last 72h. Defaults to 1."
                },
                "dimension": {
                    "type": "string",
                    "enum": sorted(ALLOWED_DIMENSIONS),
                    "description": "Optional breakdown, e.g. Device, Source, Channel or URL. Omit for site-wide totals."
                }
            }
        }
    },
    {
        "name": "analyze_website",
        "description": (
            "Analyse how the website is performing and what to improve. Pulls Microsoft Clarity data for the last 1-3 days, "
            "compares phones vs computers, traffic sources and pages, estimates conversion (orders / real visitors) from Shopify, "
            "and returns prioritised findings, each with a suggested action, plus a confidence rating. "
            "Use this FIRST for any question about website performance, traffic, visitors, user experience, conversion, "
            "'how is the site doing' or 'what should we improve'. Uses up to 3 of the ~10 daily Clarity calls (cached for 3 hours)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "num_days": {"type": "integer", "enum": [1, 2, 3],
                             "description": "How many days back: 1 = last 24h, 2 = 48h, 3 = 72h. Defaults to 1; use 3 for a bigger sample."}
            }
        }
    },
    {
        "name": "create_artifact",
        "description": (
            "Create a polished report page (an 'artifact') in the HerVpod style that the founder can open, download or print. "
            "Use it when she asks for a report, dashboard, chart, comparison or something to keep or share, or when 3+ numbers "
            "across periods or segments are clearer as a visual. Don't use it for simple answers. Use real numbers from your tool "
            "results only. You send structured blocks (never HTML); the page is rendered for you. "
            "Block types: "
            "{type:'heading', text, level?:2|3}; "
            "{type:'text', text} (blank line = new paragraph; lines starting '- ' = bullets; **bold** allowed); "
            "{type:'kpis', items:[{label, value:'$412', delta?:'+12% vs last week', tone?:'good'|'bad'|'neutral'}]} (max 6; tone says whether the change is good or bad for the business); "
            "{type:'bar_chart', title, subtitle?, unit?:'$'|'%'|'s'|'x'|word, labels:[...], series:[{name, values:[...]}]} (compare categories; 1-3 series, max 24 labels, no negatives); "
            "{type:'line_chart', title, subtitle?, unit?, labels:[dates or steps in order], series:[{name, values:[...]}]} (change over time; 1-3 series); "
            "{type:'table', title?, columns:[...], rows:[[...]]}; "
            "{type:'callout', tone:'info'|'good'|'warning'|'bad', title?, text}; "
            "{type:'suggestions', items:[{title, why, action, impact?, priority:'high'|'medium'|'low'}]} (recommended actions for the founder). "
            "A good report: key numbers first (kpis), then the charts or table that explain them, then suggestions. 3-8 blocks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short report title, e.g. 'Website health: last 24 hours'."},
                "subtitle": {"type": "string", "description": "One line on what the report shows."},
                "sources": {"type": "array", "items": {"type": "string"}, "description": "Where the data came from, e.g. ['Shopify', 'Clarity']."},
                "blocks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"type": {"type": "string", "enum": list(artifacts.RENDERERS)}},
                        "required": ["type"],
                        "additionalProperties": True,
                    },
                },
            },
            "required": ["title", "blocks"],
        }
    }
]

def system_prompt() -> str:
    """The base prompt plus today's date, so the model can turn 'yesterday' into real dates."""
    try:
        tz = get_store_timezone()
    except Exception:
        tz = None
    now = datetime.now(tz)
    return (config.SYSTEM_PROMPT +
            f"\nToday is {now:%A, %Y-%m-%d} (store time zone: {tz or 'unknown'}). "
            "'Yesterday', 'this week' (Mon-Sun) etc. are relative to this date.")


def first_text(response) -> str:
    for block in response.content:
        if block.type == "text":
            return block.text
    raise LLMError("No text content in Anthropic response.")


def _run_tool(name: str, tool_input: dict, created: list | None = None):
    if name == "analyze_website":
        return analyze_website(num_days=tool_input.get("num_days", 1))
    if name == "create_artifact":
        art = artifacts.create_artifact(tool_input)
        if created is not None:
            created.append(art)
        return {"created": True, "title": art["title"],
                "note": "The report is shown to the user as a card under your reply. Reply in 1-3 sentences with the headline finding; don't repeat the report."}
    if name == "get_sales_summary":
        return get_sales_summary(tool_input["start_date"], tool_input.get("end_date"))
    if name == "get_recent_orders":
        limit = tool_input.get("limit", 10)
        orders = get_recent_orders(limit=limit)
        return [
            {
                "order_number": o.get("order_number") or o.get("name"),
                "created_at": o.get("created_at"),
                "total_price": o.get("total_price"),
                "currency": o.get("currency"),
                "financial_status": o.get("financial_status"),
                "line_items": [
                    {"title": li.get("title"), "quantity": li.get("quantity")}
                    for li in o.get("line_items", [])
                ],
            }
            for o in orders
        ]
    if name == "get_clarity_insights":
        return get_clarity_insights(
            num_days=tool_input.get("num_days", 1),
            dimension=tool_input.get("dimension"),
        )
    raise LLMError(f"Unknown tool '{name}'")

MAX_ROUNDS = 10
MIN_OUTPUT_TOKENS = 8192   # reports are sent as tool input, so they need room beyond a chat reply


def run_agent(messages: list[dict]) -> dict:
    """Run the assistant (with tools) and return {"reply": text, "artifacts": [reports it created]}."""
    if config.LLM_MODE == "echo":
        return {"reply": f"(echo) You said: {messages[-1]['content']}", "artifacts": []}

    if config.LLM_MODE == "anthropic":
        try:
            conversation = list(messages)
            created: list[dict] = []
            client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

            for _ in range(MAX_ROUNDS):
                response = client.messages.create(
                    model=config.LLM_MODEL,
                    max_tokens=max(config.LLM_MAX_TOKENS, MIN_OUTPUT_TOKENS),
                    system=system_prompt(),
                    tools=TOOLS,
                    messages=conversation,
                )

                if response.stop_reason != "tool_use":
                    if response.stop_reason == "max_tokens" and any(b.type == "tool_use" for b in response.content):
                        raise LLMError("That report was too big to build. Ask for a shorter one (fewer charts).")
                    return {"reply": first_text(response), "artifacts": created}

                conversation.append({"role": "assistant", "content": response.content})

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        log.info("tool call %s", block.name)
                        try:
                            result = _run_tool(block.name, block.input, created)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result),
                            })
                        except Exception as e:
                            log.warning("tool %s failed: %s", block.name, e)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"Error running tool: {e}",
                                "is_error": True,
                            })

                conversation.append({"role": "user", "content": tool_results})

            raise LLMError("Too many tool calls without a final answer.")
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"Anthropic API error: {e}")

    raise LLMError(f"Unknown LLM_MODE '{config.LLM_MODE}'")


def generate_reply(messages: list[dict]) -> str:
    return run_agent(messages)["reply"]
