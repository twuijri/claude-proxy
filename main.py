import os
import json
import uuid
import time
import asyncio
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
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "proxy-key-change-me")
CLAUDE_DIR    = Path("/root/.claude")

# ─── Model Mapping ───────────────────────────────────────────────────────────
MODEL_MAP = {
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-opus-4-6":   "claude-opus-4-6",
    "claude-opus-4-5":   "claude-opus-4-5",
    "claude-sonnet-4-5": "claude-sonnet-4-5",
    "claude-haiku-4-5":  "claude-haiku-4-5",
    "claude-sonnet":     "claude-sonnet-4-6",
    "claude-opus":       "claude-opus-4-6",
    "claude-haiku":      "claude-haiku-4-5",
    "gpt-4":             "claude-opus-4-6",
    "gpt-4o":            "claude-sonnet-4-6",
    "gpt-3.5-turbo":     "claude-sonnet-4-6",
}
DEFAULT_MODEL = "claude-sonnet-4-6"

# ─── Auth Check ──────────────────────────────────────────────────────────────
def is_authenticated() -> bool:
    for name in [".credentials.json", "credentials.json"]:
        path = CLAUDE_DIR / name
        if path.exists():
            try:
                data = json.loads(path.read_text())
                oauth = data.get("claudeAiOauth") or data.get("oauth") or data
                if isinstance(oauth, dict) and oauth.get("accessToken"):
                    return True
            except Exception:
                pass
    return False

def restore_claude_config():
    """يسترجع ملف الإعدادات من النسخة الاحتياطية إذا كان مفقوداً."""
    config = Path("/root/.claude.json")
    if config.exists():
        return
    backups_dir = CLAUDE_DIR / "backups"
    if not backups_dir.exists():
        return
    backups = sorted(backups_dir.glob(".claude.json.backup.*"), reverse=True)
    if backups:
        import shutil
        shutil.copy(backups[0], config)
        log.info(f"✅ تم استرجاع الإعدادات من: {backups[0].name}")

# استرجاع عند بدء التشغيل
restore_claude_config()

# ─── FastAPI ─────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Claude Max Proxy",
    description="بروكسي يستخدم Claude CLI كـ backend – متوافق مع OpenAI API",
    version="4.0.0"
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

# ─── Auth Middleware ──────────────────────────────────────────────────────────
async def verify_api_key(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    if auth.split("Bearer ", 1)[1].strip() != PROXY_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

# ─── Prompt Builder ───────────────────────────────────────────────────────────
def build_prompt(req: ChatRequest) -> tuple[str, str]:
    """
    يبني الـ prompt من messages بصيغة نص واحد.
    يرجع (system, prompt).
    """
    system = req.system or ""
    parts = []

    for msg in req.messages:
        content = msg.content if isinstance(msg.content, str) \
                  else " ".join(p.get("text", "") for p in msg.content if isinstance(p, dict))
        if msg.role == "system":
            system += ("\n" if system else "") + content
        elif msg.role == "user":
            parts.append(f"Human: {content}")
        elif msg.role == "assistant":
            parts.append(f"Assistant: {content}")

    return system, "\n\n".join(parts)

# ─── Claude CLI Runner ───────────────────────────────────────────────────────
async def run_claude_cli(prompt: str, model: str, system: str = "", timeout: int = 120) -> str:
    """
    يشغّل `claude -p "..."` كـ subprocess ويرجع الرد كاملاً.
    """
    if not is_authenticated():
        raise HTTPException(
            status_code=503,
            detail="لا يوجد مصادقة. شغّل: docker exec -it claude-proxy claude"
        )

    full_prompt = f"{system}\n\n{prompt}" if system else prompt

    cmd = ["claude", "-p", full_prompt, "--model", model, "--dangerously-skip-permissions"]
    log.info(f"CLI → model={model}, prompt_len={len(full_prompt)}")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        process.kill()
        raise HTTPException(status_code=504, detail="انتهت مهلة الطلب (timeout)")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="Claude CLI غير موجود في الـ container")

    if process.returncode != 0:
        err = stderr.decode().strip()
        log.error(f"Claude CLI error (exit {process.returncode}): {err}")
        raise HTTPException(status_code=500, detail=f"Claude CLI error: {err}")

    return stdout.decode().strip()

async def stream_claude_cli(prompt: str, model: str, system: str = "") -> AsyncIterator[str]:
    """
    يشغّل الـ CLI ويبث الرد بصيغة OpenAI SSE.
    """
    text = await run_claude_cli(prompt, model, system)

    request_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    # بث الرد كلمة كلمة (simulate streaming)
    words = text.split(" ")
    for i, word in enumerate(words):
        piece = word if i == 0 else " " + word
        chunk = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": piece}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        await asyncio.sleep(0.01)

    # إشارة النهاية
    stop_chunk = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(stop_chunk)}\n\n"
    yield "data: [DONE]\n\n"

# ─── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    auth = is_authenticated()
    return {
        "service": "Claude Max Proxy",
        "version": "4.0.0",
        "authenticated": auth,
        "backend": "claude-cli",
        "hint": None if auth else "شغّل: docker exec -it claude-proxy claude",
    }

@app.get("/health")
async def health():
    ok = is_authenticated()
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "healthy" if ok else "unauthenticated", "timestamp": int(time.time())}
    )

@app.post("/auth/refresh")
async def refresh_auth():
    ok = is_authenticated()
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

    system, prompt = build_prompt(req)

    if req.stream:
        return StreamingResponse(
            stream_claude_cli(prompt, model, system),
            media_type="text/event-stream"
        )

    # Non-streaming
    text = await run_claude_cli(prompt, model, system)
    words = len(text.split())

    return JSONResponse({
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": -1, "completion_tokens": words, "total_tokens": -1},
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
