# Genesis AI (Founder AI Operating Assistant) — Build Notes

Internal project log for Cool Comforts Co's (Hervypod's) Founder AI Operating Assistant. Update this after every major step: what changed, why, and what's next.

## What this is

An internal AI tool for founder Krisha that pulls data from Shopify, Microsoft Clarity, Walmart/RJW 3PL, Amazon, and ad platforms, and answers plain-language questions ("what happened with sales yesterday?") instead of her checking five dashboards by hand.

## Tech stack

- Plain Python + FastAPI backend, plain HTML/JS frontend (no framework)
- Anthropic SDK directly, model `claude-sonnet-5`
- No LangChain — hand-rolled agent/tool-calling loop
- One venv, `.env` for secrets, `.gitignore` protecting both

## Project structure

```
founder-assistant/
  backend/
    .venv/
    .env
    config.py
    llm.py
    shopify_client.py
    clarity_client.py
    brief.py
    auth.py
    main.py
    requirements.txt
    tests/test_chat.py
    tests/test_clarity.py
    tests/test_sales_and_brief.py
    tests/test_auth.py
  frontend/
    index.html
    login.html
```

## How to run it (every session)

1. Open the `founder-assistant` folder in VS Code.
2. Open a terminal (**Terminal → New Terminal**) — confirm with `pwd` that it's inside `founder-assistant`.
3. ```bash
   cd backend
   source .venv/bin/activate
   python3 -m uvicorn main:app --reload --port 8000
   ```
4. Leave that terminal running. Open **`http://127.0.0.1:8000`** in the browser — not by double-clicking `index.html` directly (that has no server behind it and always fails with "Failed to fetch").
5. If you edit `.env` (secrets, mode, model), you must **fully stop (Ctrl+C) and restart** `uvicorn` — `--reload` only picks up `.py` file changes, not `.env` changes.
6. To run a one-off command (installing a package, testing a function directly) without killing the server, open a **second terminal tab** — don't type into the one running `uvicorn`.

## Timeline

### 2026-09-26 — Real AI wired in
- Got a real Anthropic API key into `.env`, confirmed `generate_reply()` returns real Claude output (not `(echo)` placeholder).
- Fixed a bug in `llm.py`: `response.content[0].text` broke because Claude Sonnet 5 can return a `ThinkingBlock` before the text block. Fixed by looping through `response.content` and returning the first block where `block.type == "text"`.
- Started the FastAPI server (`uvicorn`) and confirmed the chat UI talks to it at `http://127.0.0.1:8000`. Root cause of an earlier "Failed to fetch": the frontend's `fetch("/api/chat")` is a relative path and only resolves when the page is loaded through the server — opening `index.html` directly from Finder can't work.
- Added `SYSTEM_PROMPT` in `config.py` and passed it into `client.messages.create(system=...)` in `llm.py`, giving Claude context on Cool Comforts Co and its job (concise, cite real data, admit when it doesn't have live data rather than guessing).
- Verified: asking "who are you" returns an answer that correctly identifies the business and honestly says it has no live data yet (expected at that stage — no tool-calling built yet).

### 2026-09-26 — Tool-calling loop built
- Added a `TOOLS` schema in `llm.py` describing `get_recent_orders` (optional `limit` param) so Claude knows the tool exists and when to use it.
- Added `_run_tool()` to map a tool name to the real Python function and trim Shopify's huge raw order objects down to the fields that matter (order number, date, total, currency, payment status, line items).
- Rewrote `generate_reply()` as a loop (capped at 5 rounds): call the API → if `stop_reason == "tool_use"`, run the tool and feed the result back as a `tool_result` message → repeat until Claude returns real text.
- Confirmed working end-to-end logically (Claude correctly called the tool and reported an honest error when Shopify auth failed — proof the loop itself was functioning).

### 2026-09-29 — Shopify auth bug found and partially fixed
- Diagnosed a persistent "400 Bad Request" on the Shopify OAuth token exchange by calling the token endpoint directly and printing the raw response body (not just the status code).
- Root cause: `SHOPIFY_CLIENT_ID` in `.env` was still the literal placeholder string `your_real_client_id`, never replaced with a real value. Shopify's error ("Could not find Shopify API application with api_key your_real_client_id") confirmed this precisely.
- Got real Shopify API credentials from the custom app in the Shopify admin (Settings → Apps and sales channels → Develop apps), including `read_orders` scope, and put them in `.env`.
- Confirmed directly via script: token exchange now returns `status: 200` with a real access token, and `get_recent_orders(limit=3)` returns real order data (orders #1018, #1017, #1016 with real customers, totals, line items).
- **Known issue carried forward:** the running `uvicorn` server was started *before* `.env` was fixed, so it's still holding the old placeholder values in memory (`--reload` doesn't reload `.env`, only `.py` changes). Chat UI was still showing the old auth error as of this session. Fix identified but not yet confirmed: fully stop (Ctrl+C) and restart the server.

### 2026-10-01 — Picking back up
- Confirmed the project folder and files are intact. Session paused before confirming the full-restart fix above.
- **Next action:** restart `uvicorn` cleanly, reload the chat UI at `http://127.0.0.1:8000`, and ask a real sales question to confirm live Shopify data now flows all the way through to the chat.
- Shopify worked from a terminal script but not in the chat UI. Fix: activated the venv, killed whatever was still listening on port 8000 (`kill $(lsof -t -i :8000)`), started `uvicorn` fresh, and hard-refreshed the page (Cmd+Shift+R).
- **Confirmed: live Shopify order data now shows up in the chat UI end to end.** Shopify part of V1 is done.

### 2026-10-01 — Clarity tool added (code done, needs token)
- New `clarity_client.py`: calls Clarity's Data Export API (`GET https://www.clarity.ms/export-data/api/v1/project-live-insights`, `Authorization: Bearer <token>`), params `numOfDays` (1–3) and optional `dimension1` (Browser, Device, Country/Region, OS, Source, Medium, Campaign, Channel, URL).
- Clarity allows only 10 calls/project/day and only covers the last 72 hours, so results are cached in `backend/.clarity_cache.json` for 3 hours and a counter stops at 9 calls/day (UTC). If the limit is hit, it falls back to the last cached result.
- Registered `get_clarity_insights` as a second tool in `llm.py`; system prompt now lists both data sources.
- `llm.py` now logs every tool call and tool error to the `uvicorn` terminal, so failures are visible without guessing.
- Added `tests/test_clarity.py` (mocked API: shaping, caching, daily limit, bad input, missing token, tool routing). `pytest` → 10 passed.
- **To activate:** Clarity → Settings → Data Export → Generate new API token → add `CLARITY_API_TOKEN=...` to `backend/.env` → full restart of `uvicorn`. Then ask "how was website traffic in the last 24 hours, by device?"

### 2026-10-01 — V2 started: Shopify date filter + daily founder brief
**Shopify date ranges**
- New tool `get_sales_summary(start_date, end_date)`: fetches every order in the range (store time zone, follows Shopify pagination, cap 2,000), and returns pre-computed totals: orders, revenue, AOV, discounts, cancellations, units, top products, per-day breakdown, sample order numbers. Python does the maths so Claude never adds up orders itself.
- `get_recent_orders` kept for "latest orders" questions.
- Today's date + store time zone are now added to the system prompt on every request, so "yesterday" / "this week" / "last 30 days" resolve to real dates. Comparisons = Claude calls the tool once per period.
- Store time zone is read from Shopify (`shop.json`); override with `STORE_TIMEZONE=` in `.env` if needed.
- Shopify API version bumped `2024-01` → `2026-01`, now configurable via `SHOPIFY_API_VERSION`.
- `LLM_MAX_TOKENS` default raised to 4096; tool loop raised to 8 rounds.
- Note: without the `read_all_orders` scope Shopify only returns the last 60 days of orders.

**Daily founder brief (`brief.py`)**
- Gathers yesterday's Shopify summary vs the previous 7-day daily average, and Clarity's last 24h vs the previous 24h (= last 48h minus last 24h, since Clarity can't query a specific past day; 2 Clarity calls, cached).
- Python flags anything unusual: revenue ±30% vs 7-day avg, zero orders, cancellations, sessions ±30%, rage/dead/error clicks, script errors or quickbacks doubling (min 5), bots >30%. Any data source that fails is listed under "Needs attention" instead of silently disappearing.
- Claude writes a short plain-text brief (Sales / Website / Needs attention) from that data only. Echo mode or an LLM failure falls back to a no-AI version.
- Cached per day in `backend/.brief_cache.json`; `GET /api/brief` (add `?refresh=true` to regenerate).
- Chat UI shows a "Morning brief" card at the top with Refresh / Hide; orange border when there's something to check.
- CLI: `python3 brief.py` (or `--refresh`) prints the brief — ready to hook to a scheduled job / email later.
- Tests: `tests/test_sales_and_brief.py` (summary maths, time zones, pagination, flags, failing source, endpoint caching). `pytest` → 19 passed.

### 2026-10-01 — UI cleanup
- Redesigned `frontend/index.html` (still plain HTML/JS, no framework). Cool palette to match the brand (frost white, slate ink, glacier teal, amber for things to check), Schibsted Grotesk font, automatic dark mode.
- Laptop: morning brief is a panel on the left, chat on the right. Phone: brief on top (Hide/Show), chat below.
- The brief's plain text is parsed into sections (Sales / Website / Needs attention); "Needs attention" items show as amber rows, or teal when nothing's unusual. Header shows today's date and how many things need checking.
- Chat replies now render bold text and bullet/numbered lists instead of raw `**` asterisks (HTML-escaped first, so it's safe).
- Removed the You/AI avatars and the "Mode: anthropic" badge; a small warning appears only in test (echo) mode or when the server isn't reachable.
- Added suggested questions on the empty chat and a "New chat" button that clears the conversation.
- Brief greeting now uses a friendly date ("yesterday, Wednesday September 30") instead of an ISO date.

### 2026-10-01 — Matched the UI to the HerVpod brand (hervpod.com)
- Colours taken from the live site's theme: plum `#641f40` (text/headings), light plum `#832a54`, rose `#c24784` (buttons, user messages), blush `#faecf3` (tints), page `#f8f3f6`, promo red `#d02e2e` for errors. Amber kept only for "needs attention". Matching dark-mode palette.
- Fonts match the site: Raleway for headings, buttons and UI; Libre Baskerville for the brief and chat replies. Buttons use the site's 4px corners and spaced capitals (SEND).
- Header shows the HerVpod logo (loaded from the store's Shopify CDN); falls back to "Cool Comforts Co" text if it can't load (e.g. offline).

### 2026-10-01 — Named the app Genesis AI
- Browser tab title and header now read "Genesis AI" (next to the HerVpod logo); the system prompt tells Claude its name is Genesis AI.

### 2026-10-01 — Password protection + temporary live link
- Added a login to `main.py`: when `APP_PASSWORD` is set in `.env`, every page and API call requires a username + password (later replaced by the branded sign-in page below). Username defaults to `krisha` (`APP_USERNAME`); several users can share the password by listing them comma-separated, e.g. `APP_USERNAME=kwilliams@coolcomforts.co,iwilliams@coolcomforts.co`. Usernames ignore capital letters. With no `APP_PASSWORD`, the app stays open as before for local use. `tests/test_auth.py` → 22 tests pass.
- Temporary public link = Cloudflare quick tunnel from the Mac (free, no account): `cloudflared tunnel --url http://127.0.0.1:8000` prints an `https://<random>.trycloudflare.com` address. The link works only while the Mac is awake and both `uvicorn` and `cloudflared` are running; it changes every time the tunnel restarts.
- Never run the tunnel without `APP_PASSWORD` set — the app has live sales data and spends Anthropic credit.

### 2026-10-01 — Branded sign-in page (replaces the browser login pop-up)
- New `frontend/login.html` in the HerVpod theme: logo, "Genesis AI", the pink sky from the homepage, email + password form with Show/Hide, clear error messages, works on phone and in dark mode. Remembers the last email on that device.
- New `backend/auth.py`: checks email + shared password, then sets a signed, HTTP-only session cookie (12 hours by default, `SESSION_HOURS`). Wrong password: short delay; 10 wrong tries in 15 minutes from one visitor = blocked for 15 minutes.
- `main.py`: while signed out, pages redirect to `/login` and API calls return 401; `/logout` signs out. Header shows "Sign out" when login is on. Removing an email from `APP_USERNAME` cuts off that person's existing session.
- Optional `SESSION_SECRET=` (any long random text) in `.env` keeps people signed in across server restarts; without it, a restart signs everyone out.
- `tests/test_auth.py` rewritten for the new flow → 27 tests pass.

## Open items / next steps (standing list)

**V2 plan:** (1) Shopify date filter ✅ (2) daily founder brief ✅ (3) Walmart Seller Center (4) hosting + password so Krisha can use it without the laptop. Stretch: RJW 3PL (pending API/SFTP answer), Amazon SP-API (start developer registration early). V3: ads, calendar/email/accelerator agents.

1. ~~Confirm Shopify data now shows up correctly in the chat UI after a clean server restart.~~ Done 2026-10-01.
1a. ~~Shopify date filtering, API version bump, LLM_MAX_TOKENS~~ Done 2026-10-01.
2. Write a system prompt / data dictionary explaining the four additional sources to come (Clarity, Amazon, ads, RJW), so new tools slot into the same pattern as `get_recent_orders`.
3. ~~Add a Clarity tool~~ Code done 2026-10-01 (`get_clarity_insights`); add `CLARITY_API_TOKEN` to `.env` and test live.
4. Add a Walmart/RJW 3PL fulfillment data tool.
5. Add an Amazon orders/sales tool.
6. Add an ads spend/performance tool (platform TBD).
7. Longer term: decide how multi-source questions get combined (e.g. "how did we do yesterday across every channel") — likely means Claude calling multiple tools in one turn, which the current loop already supports, but worth a dedicated test once 2+ real tools exist.

## Gotchas to remember

- Going live temporarily: set `APP_PASSWORD` in `.env` and restart first, then in a second terminal tab run `caffeinate -i cloudflared tunnel --url http://127.0.0.1:8000`. Ctrl+C in that tab takes it offline.

- Editing `.env` requires a full server restart, not just a save.
- If the chat behaves like it has old settings, an old server may still be on port 8000: check with `lsof -i :8000`, clear it with `kill $(lsof -t -i :8000)`, restart, then hard-refresh the page (Cmd+Shift+R).
- The chat UI only works when loaded through the server (`http://127.0.0.1:8000`), never via `file://.../index.html`.
- Running `uvicorn` occupies its terminal tab — always use a separate terminal tab for other commands.
- Never paste real API keys, tokens, or `.env` contents into chat. If sharing command output for debugging, redact secret values first.

## Analysis + reports (Genesis AI)
- `backend/analysis.py`: deterministic Clarity analysis (`analyze_website`). Returns findings (severity, evidence, suggestion), strengths, confidence. Small samples are demoted to "low". The model explains it; it does not compute it.
- `backend/artifacts.py`: report builder. The model sends structured blocks (heading, text, kpis, bar_chart, line_chart, table, callout, suggestions); Python renders safe, escaped, branded HTML (strict CSP, sandboxed iframe). Saved in `backend/artifacts/` (gitignored, newest 200 kept). Served at `/api/artifacts/<id>` (`?download=true` to download).
- `llm.py`: tools are now get_sales_summary, get_recent_orders, get_clarity_insights, analyze_website, create_artifact. `run_agent` returns reply + artifacts. max_tokens floor 8192 for reports.
- Morning brief now has a SUGGESTIONS section (reuses cached Clarity data, no extra calls).
- Frontend: report cards in chat + slide-over viewer. Reports are not kept in chat history after a page reload (files remain on disk).
- Clarity budget: 10 calls/day. Brief uses about 2; deep analysis up to 3 more per distinct day range.
- Tests: 49 passing (`python3 -m pytest -q` in backend).
