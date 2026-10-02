import logging, time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

import artifacts
import auth
import config
from llm import LLMError, run_agent
from brief import get_brief

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("founder-assistant")

app = FastAPI()
FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


LOGIN_PAGE = FRONTEND.parent / "login.html"
PUBLIC_PATHS = {"/login", "/api/login", "/favicon.ico"}


def _client_id(request: Request) -> str:
    # Behind the Cloudflare tunnel every request comes from localhost, so use Cloudflare's header.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


@app.middleware("http")
async def require_login(request: Request, call_next):
    """When APP_PASSWORD is set, everything except the sign-in page needs a valid session."""
    if auth.enabled() and request.url.path not in PUBLIC_PATHS:
        if not auth.session_user(request.cookies.get(auth.COOKIE_NAME)):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "You've been signed out. Reload the page to sign in."}, status_code=401)
            return RedirectResponse("/login")
    return await call_next(request)


class LoginRequest(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=500)


@app.get("/login")
def login_page(request: Request):
    if not auth.enabled() or auth.session_user(request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse("/")
    return FileResponse(LOGIN_PAGE)


@app.post("/api/login")
def login(req: LoginRequest, request: Request):
    try:
        user = auth.check_login(req.username, req.password, _client_id(request))
    except auth.AuthError as e:
        raise HTTPException(e.status, str(e))
    log.info("signed in: %s", user)
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE_NAME, auth.make_session(user), max_age=config.SESSION_HOURS * 3600,
                    httponly=True, samesite="lax", secure=https)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login" if auth.enabled() else "/")
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)

class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=40)

@app.get("/")
def index():
    return FileResponse(FRONTEND)

@app.get("/health")
def health():
    return {"status": "ok", "mode": config.LLM_MODE, "login": auth.enabled()}

@app.post("/api/chat")
def chat(req: ChatRequest):
    if req.messages[-1].role != "user":
        raise HTTPException(400, "The last message must come from the user.")
    start = time.perf_counter()
    try:
        result = run_agent([m.model_dump() for m in req.messages])
    except LLMError as e:
        raise HTTPException(502, str(e))
    ms = int((time.perf_counter() - start) * 1000)
    log.info("chat mode=%s turns=%d reports=%d latency_ms=%d", config.LLM_MODE, len(req.messages), len(result["artifacts"]), ms)
    return {"reply": result["reply"], "artifacts": result["artifacts"], "mode": config.LLM_MODE, "latency_ms": ms}


@app.get("/api/artifacts/{art_id}")
def get_artifact(art_id: str, download: bool = False):
    """A report the assistant created. Served with a strict policy (no scripts) so it's safe to embed."""
    page = artifacts.read_artifact(art_id)
    if page is None:
        raise HTTPException(404, "That report isn't available any more.")
    headers = {"Content-Security-Policy": artifacts.CSP, "X-Content-Type-Options": "nosniff",
               "Cache-Control": "private, max-age=3600"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{artifacts.filename_for(page)}"'
    return HTMLResponse(page, headers=headers)


@app.get("/api/brief")
def brief(refresh: bool = False):
    """Today's founder brief (yesterday's numbers). Cached for the day; ?refresh=true regenerates."""
    start = time.perf_counter()
    try:
        result = get_brief(refresh=refresh)
    except Exception as e:
        log.exception("brief failed")
        raise HTTPException(502, f"Couldn't build the brief: {e}")
    log.info("brief cached=%s flags=%d latency_ms=%d", result["cached"], len(result["flags"]),
             int((time.perf_counter() - start) * 1000))
    return result
