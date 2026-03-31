import os
import json
import uuid
import time
import httpx
import logging
from pathlib import Path
from typing import Optional, Union, List, AsyncIterator
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("claude-proxy")

# ─── Config ──────────────────────────────────────────────────────────────────
PROXY_API_KEY   = os.environ.get("PROXY_API_KEY", "proxy-key-change-me")
ANTHROPIC_URL   = "https://api.anthropic.com/v1"
ANTHROPIC_VER   = "2023-06-01"

CLAUDE_DIR = Path("/root/.claude")
CLAUDE_CREDENTIALS_FILE = Path(
    os.environ.get("CLAUDE_CREDENTIALS_FILE", "/root/.claude/.credentials.json")
)
CLAUDE_SESSION_KEY_FALLBACK = os.environ.get("CLAUDE_SESSION_KEY", "")

# ─── Model Mapping ───────────────────────────────────────────────────────────
MODEL_MAP = {
    # ─── أحدث النماذج (4.6) ──────────────────────────────────
    "claude-sonnet-4-6":  "claude-sonnet-4-6",
    "claude-opus-4-6":    "claude-opus-4-6",
    # ─── نماذج 4.5 ───────────────────────────────────────────
    "claude-opus-4-5":    "claude-opus-4-5",
    "claude-sonnet-4-5":  "claude-sonnet-4-5",
    "claude-haiku-4-5":   "claude-haiku-4-5",
    # ─── أسماء مختصرة ────────────────────────────────────────
    "claude-sonnet":      "claude-sonnet-4-6",
    "claude-opus":        "claude-opus-4-6",
    "claude-haiku":       "claude-haiku-4-5",
    # ─── أسماء OpenAI (للتوافق) ──────────────────────────────
    "gpt-4":              "claude-opus-4-6",
    "gpt-4o":             "claude-sonnet-4-6",
    "gpt-3.5-turbo":      "claude-sonnet-4-6",
}
DEFAULT_MODEL = "claude-sonnet-4-6"

# ─── Credential Manager ──────────────────────────────────────────────────────
class CredentialManager:
    """يقرأ OAuth token من ملف Claude CLI."""

    _token: Optional[str] = None
    _cache_time: float = 0
    CACHE_TTL = 300  # 5 دقائق

    @classmethod
    def _find_credentials(cls) -> Optional[dict]:
        candidates = [
            CLAUDE_CREDENTIALS_FILE,
            CLAUDE_DIR / ".credentials.json",
            CLAUDE_DIR / "credentials.json",
        ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text())
                oauth = data.get("claudeAiOauth") or data.get("oauth") or data
                if isinstance(oauth, dict) and oauth.get("accessToken"):
                    log.info(f"✅ OAuth token من: {path.name}")
                    return oauth
            except Exception as e:
                log.warning(f"تعذّر قراءة {path}: {e}")
        return None

    @classmethod
    def get_token(cls) -> str:
        now = time.time()
        if cls._token and (now - cls._cache_time) < cls.CACHE_TTL:
            return cls._token

        oauth = cls._find_credentials()
        if oauth:
            cls._token = oauth["accessToken"]
            cls._cache_time = now
            return cls._token

        if CLAUDE_SESSION_KEY_FALLBACK:
            return CLAUDE_SESSION_KEY_FALLBACK

        raise HTTPException(
            status_code=503,
            detail="لا يوجد مصادقة. شغّل: docker exec -it claude-proxy claude"
        )

    @classmethod
    def clear_cache(cls):
        cls._token = None
        cls._cache_time = 0

    @classmethod
    def is_authenticated(cls) -> bool:
        try:
            cls.get_token()
            return True
        except Exception:
            return False

# ─── FastAPI ─────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Claude Max Proxy",
    description="بروكسي يستخدم Claude Max OAuth مع Anthropic API – متوافق مع OpenAI API",
    version="3.0.0"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Pydantic Models ─────────────────────────────────────────────────────────
class Message(BaseModel):
    role: str
    content: Union[str, List[dict]]

class ChatRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: List[Message]
    max_tokens: Optional[int] = 8096
    temperature: Optional[float] = 1.0
    stream: Optional[bool] = False
    system: Optional[str] = None

# ─── Auth ────────────────────────────────────────────────────────────────────
async def verify_api_key(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    if auth.split("Bearer ", 1)[1].strip() != PROXY_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

# ─── Anthropic API Client ────────────────────────────────────────────────────
def get_headers() -> dict:
    token = CredentialManager.get_token()
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-version": ANTHROPIC_VER,
        "content-type": "application/json",
    }

def build_anthropic_payload(req: ChatRequest, model: str) -> dict:
    """تحويل OpenAI format إلى Anthropic Messages API format."""
    messages = []
    system = req.system or ""

    for msg in req.messages:
        content = msg.content if isinstance(msg.content, str) \
                  else " ".join(p.get("text", "") for p in msg.content if isinstance(p, dict))
        if msg.role == "system":
            system += ("\n" if system else "") + content
        else:
            role = "user" if msg.role == "user" else "assistant"
            messages.append({"role": role, "content": content})

    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": req.max_tokens or 8096,
    }
    if system:
        payload["system"] = system
    if req.temperature is not None:
        payload["temperature"] = min(req.temperature, 1.0)
    return payload

async def stream_anthropic(payload: dict, model: str) -> AsyncIterator[str]:
    """يبث الرد من Anthropic API بصيغة OpenAI SSE."""
    request_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    payload["stream"] = True

    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{ANTHROPIC_URL}/messages",
            headers=get_headers(),
            json=payload,
        ) as response:
            if response.status_code == 401:
                CredentialManager.clear_cache()
                raise HTTPException(status_code=401, detail="انتهت صلاحية الـ token. أعد تسجيل الدخول.")
            if response.status_code != 200:
                body = await response.aread()
                raise HTTPException(status_code=response.status_code, detail=body.decode())

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")
                if etype == "content_block_delta":
                    text = event.get("delta", {}).get("text", "")
                    if text:
                        chunk = {
                            "id": request_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                elif etype == "message_stop":
                    stop_chunk = {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    yield f"data: {json.dumps(stop_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

# ─── Endpoints ───────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    auth = CredentialManager.is_authenticated()
    creds_found = any(
        p.exists() for p in [CLAUDE_DIR / ".credentials.json", CLAUDE_DIR / "credentials.json"]
    )
    return {
        "service": "Claude Max Proxy",
        "version": "3.0.0",
        "authenticated": auth,
        "auth_source": "claude-cli" if creds_found else ("env-session-key" if CLAUDE_SESSION_KEY_FALLBACK else "none"),
        "hint": None if auth else "شغّل: docker exec -it claude-proxy claude",
    }

@app.get("/health")
async def health():
    ok = CredentialManager.is_authenticated()
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "healthy" if ok else "unauthenticated", "timestamp": int(time.time())}
    )

@app.post("/auth/refresh")
async def refresh_auth():
    CredentialManager.clear_cache()
    ok = CredentialManager.is_authenticated()
    return {"refreshed": True, "authenticated": ok}

@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": 1700000000, "owned_by": "anthropic"}
            for m in MODEL_MAP
        ],
    }

@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(req: ChatRequest):
    model = MODEL_MAP.get(req.model, DEFAULT_MODEL)
    log.info(f"Request → {req.model} → {model}, stream={req.stream}, msgs={len(req.messages)}")

    payload = build_anthropic_payload(req, model)

    if req.stream:
        return StreamingResponse(
            stream_anthropic(payload, model),
            media_type="text/event-stream"
        )

    # Non-streaming
    payload["stream"] = False
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{ANTHROPIC_URL}/messages",
            headers=get_headers(),
            json=payload,
        )
        if r.status_code == 401:
            CredentialManager.clear_cache()
            raise HTTPException(status_code=401, detail="انتهت صلاحية الـ token.")
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)

    data = r.json()
    content = data.get("content", [{}])
    text = content[0].get("text", "") if content else ""
    usage = data.get("usage", {})

    return JSONResponse({
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", -1),
            "completion_tokens": usage.get("output_tokens", -1),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
