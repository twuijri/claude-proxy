import os
import json
import uuid
import time
import httpx
import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator, Optional, Union, List
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
PROXY_API_KEY        = os.environ.get("PROXY_API_KEY", "proxy-key-change-me")
CLAUDE_ORG_ID        = os.environ.get("CLAUDE_ORG_ID", "")
CLAUDE_BASE_URL      = "https://claude.ai/api"

# مسار بيانات المصادقة – نفس المسار الذي يستخدمه Claude CLI
CLAUDE_CREDENTIALS_FILE = Path(
    os.environ.get("CLAUDE_CREDENTIALS_FILE", "/root/.claude/credentials.json")
)
# بديل: Session Key يدوي (إذا لم يُستخدم CLI)
CLAUDE_SESSION_KEY_FALLBACK = os.environ.get("CLAUDE_SESSION_KEY", "")

# Model mapping
MODEL_MAP = {
    # ─── أحدث النماذج (4.6) ───────────────────────────────────
    "claude-sonnet-4-6":      "claude-sonnet-4-6",
    "claude-opus-4-6":        "claude-opus-4-6",
    # ─── نماذج 4.5 ────────────────────────────────────────────
    "claude-opus-4-5":        "claude-opus-4-5",
    "claude-sonnet-4-5":      "claude-sonnet-4-5",
    "claude-haiku-4-5":       "claude-haiku-4-5",
    # ─── أسماء مختصرة / توافق مع الإصدارات القديمة ─────────
    "claude-sonnet":          "claude-sonnet-4-6",
    "claude-opus":            "claude-opus-4-6",
    "claude-haiku":           "claude-haiku-4-5",
    # ─── أسماء OpenAI (للتوافق) ───────────────────────────────
    "gpt-4":                  "claude-opus-4-6",
    "gpt-4o":                 "claude-sonnet-4-6",
    "gpt-3.5-turbo":          "claude-sonnet-4-6",
}
DEFAULT_MODEL = "claude-sonnet-4-6"

# ─── Credential Manager ───────────────────────────────────────────────────────
class CredentialManager:
    """يقرأ بيانات المصادقة من Claude CLI أو من متغيرات البيئة."""

    _cache: Optional[dict] = None
    _cache_time: float = 0
    CACHE_TTL = 60  # ثانية

    @classmethod
    def _load_from_cli_file(cls) -> Optional[dict]:
        """يقرأ ملف credentials.json الخاص بـ Claude CLI."""
        if not CLAUDE_CREDENTIALS_FILE.exists():
            return None
        try:
            data = json.loads(CLAUDE_CREDENTIALS_FILE.read_text())
            # الهيكل: { "claudeAiOauth": { "accessToken": "...", "refreshToken": "...", ... } }
            oauth = data.get("claudeAiOauth") or data.get("oauth") or data
            if isinstance(oauth, dict) and oauth.get("accessToken"):
                log.info("✅ تم تحميل OAuth token من Claude CLI credentials")
                return oauth
        except Exception as e:
            log.warning(f"تعذّر قراءة credentials.json: {e}")
        return None

    @classmethod
    def get_auth_header(cls) -> dict:
        """يُعيد headers المصادقة المناسبة."""
        now = time.time()

        # تحقق من الكاش
        if cls._cache and (now - cls._cache_time) < cls.CACHE_TTL:
            return cls._build_header(cls._cache)

        # حاول قراءة ملف CLI
        oauth = cls._load_from_cli_file()
        if oauth:
            cls._cache = oauth
            cls._cache_time = now
            return cls._build_header(oauth)

        # بديل: Session Key من env
        if CLAUDE_SESSION_KEY_FALLBACK:
            log.info("⚠️  يستخدم CLAUDE_SESSION_KEY من env (وضع احتياطي)")
            return {"Cookie": f"sessionKey={CLAUDE_SESSION_KEY_FALLBACK}"}

        raise HTTPException(
            status_code=503,
            detail="لا يوجد مصادقة صالحة. شغّل 'docker exec -it claude-auth claude' لتسجيل الدخول."
        )

    @staticmethod
    def _build_header(oauth: dict) -> dict:
        token = oauth.get("accessToken", "")
        return {"Authorization": f"Bearer {token}"}

    @classmethod
    def clear_cache(cls):
        cls._cache = None
        cls._cache_time = 0

    @classmethod
    def is_authenticated(cls) -> bool:
        try:
            cls.get_auth_header()
            return True
        except Exception:
            return False

# ─── FastAPI App ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="Claude Max Proxy",
    description="بروكسي يستخدم Claude CLI للمصادقة – متوافق مع OpenAI API",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Pydantic Models ──────────────────────────────────────────────────────────
class Message(BaseModel):
    role: str
    content: Union[str, List[dict]]

class ChatRequest(BaseModel):
    model: str = "claude-sonnet-4-6"
    messages: List[Message]
    max_tokens: Optional[int] = 8096
    temperature: Optional[float] = 1.0
    stream: Optional[bool] = False
    system: Optional[str] = None

# ─── Auth Middleware ───────────────────────────────────────────────────────────
async def verify_api_key(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    if auth.split("Bearer ", 1)[1].strip() != PROXY_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

# ─── Claude Client ────────────────────────────────────────────────────────────
def get_base_headers() -> dict:
    auth_header = CredentialManager.get_auth_header()
    return {
        "User-Agent": "Claude-CLI/1.0 (Linux; x86_64)",
        "Accept": "application/json, text/event-stream",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Content-Type": "application/json",
        "Referer": "https://claude.ai/",
        "Origin": "https://claude.ai",
        **auth_header,
    }

async def get_org_id() -> str:
    if CLAUDE_ORG_ID:
        return CLAUDE_ORG_ID
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{CLAUDE_BASE_URL}/organizations",
            headers=get_base_headers()
        )
        if r.status_code == 401:
            CredentialManager.clear_cache()
            raise HTTPException(status_code=401, detail="انتهت صلاحية الـ token. أعد تسجيل الدخول.")
        r.raise_for_status()
        orgs = r.json()
        if not orgs:
            raise HTTPException(status_code=500, detail="لا توجد منظمات في الحساب")
        org = orgs[0]
        log.info(f"Organization: {org.get('name')} ({org['uuid']})")
        return org["uuid"]

async def create_conversation(org_id: str, model: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{CLAUDE_BASE_URL}/organizations/{org_id}/chat_conversations",
            headers=get_base_headers(),
            json={"uuid": str(uuid.uuid4()), "name": "", "model": model}
        )
        r.raise_for_status()
        return r.json()["uuid"]

async def delete_conversation(org_id: str, conv_id: str):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.delete(
                f"{CLAUDE_BASE_URL}/organizations/{org_id}/chat_conversations/{conv_id}",
                headers=get_base_headers()
            )
    except Exception as e:
        log.debug(f"تنظيف المحادثة {conv_id}: {e}")

def messages_to_claude(messages: List[Message], system_override: Optional[str] = None):
    claude_msgs = []
    system = system_override or ""
    for msg in messages:
        content = msg.content if isinstance(msg.content, str) \
                  else " ".join(p.get("text", "") for p in msg.content if isinstance(p, dict))
        if msg.role == "system":
            system += ("\n" if system else "") + content
        else:
            claude_msgs.append({"role": "human" if msg.role == "user" else "assistant", "content": content})
    return claude_msgs, system

async def stream_response(org_id: str, conv_id: str, messages: list, system: str, model: str):
    request_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    payload = {
        "prompt": messages[-1]["content"] if messages else "",
        "model": model,
        "timezone": "Asia/Riyadh",
        "attachments": [],
        "files": [],
    }
    if system:
        payload["system_prompt"] = system
    if len(messages) > 1:
        payload["conversation_history"] = messages[:-1]

    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{CLAUDE_BASE_URL}/organizations/{org_id}/chat_conversations/{conv_id}/completion",
            headers={**get_base_headers(), "Accept": "text/event-stream"},
            json=payload
        ) as response:
            if response.status_code == 401:
                CredentialManager.clear_cache()
                raise HTTPException(status_code=401, detail="انتهت صلاحية الـ token.")
            if response.status_code != 200:
                body = await response.aread()
                raise HTTPException(status_code=response.status_code, detail=body.decode())

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")
                if etype == "content_block_delta":
                    text = event.get("delta", {}).get("text", "")
                    if text:
                        yield f"data: {json.dumps({'id': request_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': text}, 'finish_reason': None}]})}\n\n"
                elif etype == "message_stop":
                    yield f"data: {json.dumps({'id': request_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
                    yield "data: [DONE]\n\n"
                    break

# ─── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    authenticated = CredentialManager.is_authenticated()
    return {
        "service": "Claude Max Proxy",
        "version": "2.0.0",
        "authenticated": authenticated,
        "auth_source": "claude-cli" if CLAUDE_CREDENTIALS_FILE.exists() else
                       "env-session-key" if CLAUDE_SESSION_KEY_FALLBACK else "none",
        "hint": None if authenticated else "شغّل: docker exec -it claude-auth claude"
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
    """أعد تحميل بيانات المصادقة من الملف (بدون إعادة تشغيل)."""
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
        ]
    }

@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(req: ChatRequest):
    model = MODEL_MAP.get(req.model, DEFAULT_MODEL)
    log.info(f"Request → model={req.model}→{model}, stream={req.stream}, msgs={len(req.messages)}")

    org_id = await get_org_id()
    conv_id = await create_conversation(org_id, model)
    claude_msgs, system = messages_to_claude(req.messages, req.system)

    if req.stream:
        async def event_gen():
            try:
                async for chunk in stream_response(org_id, conv_id, claude_msgs, system, model):
                    yield chunk
            finally:
                await delete_conversation(org_id, conv_id)
        return StreamingResponse(event_gen(), media_type="text/event-stream")

    # Non-streaming
    full_text = ""
    try:
        async for chunk in stream_response(org_id, conv_id, claude_msgs, system, model):
            if chunk.startswith("data:") and "[DONE]" not in chunk:
                raw = chunk[5:].strip()
                if raw:
                    try:
                        d = json.loads(raw)
                        full_text += d["choices"][0]["delta"].get("content", "")
                    except Exception:
                        pass
    finally:
        await delete_conversation(org_id, conv_id)

    return JSONResponse({
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": full_text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": -1, "completion_tokens": -1, "total_tokens": -1}
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
