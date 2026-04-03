import os
import re
import pty
import fcntl
import struct
import termios
import json
import uuid
import time
import asyncio
import logging
from pathlib import Path
from typing import Optional, Union, List, AsyncIterator
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
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
UI_PASSWORD   = os.environ.get("UI_PASSWORD", "admin")
CLAUDE_DIR    = Path("/home/claude/.claude")

# ─── Auth Flow State ──────────────────────────────────────────────────────────
_auth_proc:      Optional[asyncio.subprocess.Process] = None
_auth_url:       Optional[str] = None
_auth_master_fd: Optional[int] = None

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
    config = Path("/home/claude/.claude.json")
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
        elif msg.role == "tool":
            parts.append(f"Tool result: {content}")

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

    cmd = ["claude", "--dangerously-skip-permissions", "-p", full_prompt, "--model", model]
    log.info(f"CLI → model={model}, prompt_len={len(full_prompt)}")

    env = {**os.environ, "HOME": "/home/claude", "USER": "claude", "LOGNAME": "claude"}

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
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

# ─── Web UI ───────────────────────────────────────────────────────────────────
_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Max Proxy</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f172a;color:#e2e8f0;
     min-height:100vh;display:flex;align-items:center;justify-content:center;padding:1rem}
.card{background:#1e293b;border:1px solid #334155;border-radius:16px;padding:2.5rem;
      width:100%;max-width:460px;box-shadow:0 25px 50px rgba(0,0,0,.5)}
h1{font-size:1.4rem;font-weight:700;color:#f1f5f9;margin-bottom:.3rem}
.sub{color:#64748b;font-size:.85rem;margin-bottom:2rem}
input{width:100%;background:#0f172a;border:1px solid #334155;border-radius:8px;
      padding:.7rem 1rem;color:#e2e8f0;font-size:.95rem;margin-bottom:.9rem;
      outline:none;transition:border-color .2s}
input:focus{border-color:#6366f1}
.btn{width:100%;background:#6366f1;color:#fff;border:none;border-radius:8px;
     padding:.75rem;font-size:.95rem;font-weight:600;cursor:pointer;transition:background .2s;margin-bottom:.5rem}
.btn:hover{background:#4f46e5}
.btn:disabled{background:#334155;cursor:not-allowed}
.btn-ghost{background:transparent;border:1px solid #334155;color:#94a3b8}
.btn-ghost:hover{background:#1e293b;border-color:#475569}
.err{color:#f87171;font-size:.85rem;margin-top:.25rem;min-height:1.2em}
.status-row{display:flex;align-items:center;gap:.6rem;padding:.7rem 1rem;
            background:#0f172a;border-radius:8px;margin-bottom:1.5rem}
.dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.dot.green{background:#22c55e;box-shadow:0 0 8px #22c55e55}
.dot.red{background:#ef4444}
.dot.yellow{background:#eab308;animation:blink 1.4s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.35}}
.url-box{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:.75rem;
         font-size:.72rem;word-break:break-all;margin-bottom:1rem;color:#818cf8;
         cursor:pointer;transition:border-color .2s;line-height:1.5}
.url-box:hover{border-color:#6366f1}
.lbl{font-size:.8rem;color:#64748b;margin-bottom:.4rem}
hr{border:none;border-top:1px solid #334155;margin:1.3rem 0}
#s-login,#s-dash{display:none}
</style>
</head>
<body>

<div id="s-login" class="card">
  <h1>Claude Max Proxy</h1>
  <p class="sub">أدخل كلمة المرور للمتابعة</p>
  <input type="password" id="pwd" placeholder="كلمة المرور"
         onkeydown="if(event.key==='Enter')doLogin()">
  <button class="btn" onclick="doLogin()">دخول</button>
  <p class="err" id="login-err"></p>
</div>

<div id="s-dash" class="card">
  <h1>Claude Max Proxy</h1>
  <p class="sub">لوحة التحكم</p>

  <div class="status-row">
    <div class="dot" id="dot"></div>
    <span id="status-txt">جاري التحقق...</span>
  </div>

  <div id="sec-btn">
    <button class="btn" onclick="startLogin()">تسجيل الدخول بـ Claude</button>
  </div>

  <div id="sec-oauth" style="display:none">
    <p class="lbl">افتح هذا الرابط في المتصفح (انقر للنسخ):</p>
    <div class="url-box" id="url-box" onclick="copyUrl()"></div>
    <hr>
    <p class="lbl">بعد الموافقة، أدخل الكود الذي ظهر لك:</p>
    <input type="text" id="code-inp" placeholder="أدخل الكود هنا"
           onkeydown="if(event.key==='Enter')submitCode()">
    <button class="btn" onclick="submitCode()">تأكيد</button>
    <button class="btn btn-ghost" onclick="cancelAuth()">إلغاء</button>
    <p class="err" id="code-err"></p>
  </div>
</div>

<script>
let pwd = '';

function show(id){
  ['s-login','s-dash'].forEach(s=>
    document.getElementById(s).style.display = s===id ? 'block' : 'none');
}

async function api(method, path, body){
  const r = await fetch(path,{
    method,
    headers:{'X-UI-Password':pwd,'Content-Type':'application/json'},
    body: body ? JSON.stringify(body) : undefined
  });
  const d = await r.json();
  if(r.status===403) throw new Error('كلمة مرور خاطئة');
  if(!r.ok) throw new Error(d.detail||'خطأ غير معروف');
  return d;
}

async function doLogin(){
  pwd = document.getElementById('pwd').value;
  try{
    await api('GET','/ui/status');
    show('s-dash');
    loadStatus();
    setInterval(loadStatus, 8000);
  }catch(e){
    document.getElementById('login-err').textContent = e.message;
  }
}

async function loadStatus(){
  try{
    const s = await api('GET','/ui/status');
    const dot = document.getElementById('dot');
    const txt = document.getElementById('status-txt');
    if(s.authenticated){
      dot.className='dot green'; txt.textContent='متصل';
    } else if(s.auth_in_progress){
      dot.className='dot yellow'; txt.textContent='جاري تسجيل الدخول...';
      if(s.auth_url) showOAuth(s.auth_url);
    } else {
      dot.className='dot red'; txt.textContent='غير متصل';
    }
  }catch(e){}
}

async function startLogin(){
  const btn = document.querySelector('#sec-btn .btn');
  btn.disabled=true; btn.textContent='جاري الاتصال...';
  try{
    const r = await api('POST','/ui/start-login');
    showOAuth(r.url);
  }catch(e){
    document.getElementById('code-err').textContent = e.message;
    btn.disabled=false; btn.textContent='تسجيل الدخول بـ Claude';
  }
}

function showOAuth(url){
  document.getElementById('sec-btn').style.display='none';
  document.getElementById('sec-oauth').style.display='block';
  const b = document.getElementById('url-box');
  b.textContent = url;
  b.dataset.url = url;
  navigator.clipboard.writeText(url).then(()=>{
    b.textContent = '✓ تم النسخ — ' + url;
    setTimeout(()=>{ b.textContent = url; }, 2500);
  }).catch(()=>{});
}

function copyUrl(){
  const url = document.getElementById('url-box').dataset.url;
  navigator.clipboard.writeText(url).then(()=>{
    const b = document.getElementById('url-box');
    b.textContent='تم النسخ ✓';
    setTimeout(()=>{ b.textContent=url; }, 2000);
  });
}

async function submitCode(){
  const code = document.getElementById('code-inp').value.trim();
  if(!code) return;
  const btn = document.querySelector('#sec-oauth .btn');
  btn.disabled=true; btn.textContent='جاري التحقق...';
  try{
    const r = await api('POST','/ui/submit-code',{code});
    if(r.success){
      resetOAuth();
      loadStatus();
    } else {
      document.getElementById('code-err').textContent='فشل تسجيل الدخول، تحقق من الكود';
      btn.disabled=false; btn.textContent='تأكيد';
    }
  }catch(e){
    document.getElementById('code-err').textContent = e.message;
    btn.disabled=false; btn.textContent='تأكيد';
  }
}

async function cancelAuth(){
  try{ await api('POST','/ui/cancel-auth'); }catch(e){}
  resetOAuth();
  loadStatus();
}

function resetOAuth(){
  document.getElementById('sec-oauth').style.display='none';
  document.getElementById('sec-btn').style.display='block';
  const btn = document.querySelector('#sec-btn .btn');
  btn.disabled=false; btn.textContent='تسجيل الدخول بـ Claude';
  document.getElementById('code-inp').value='';
  document.getElementById('code-err').textContent='';
}

show('s-login');
</script>
</body>
</html>"""

async def verify_ui(request: Request):
    if request.headers.get("X-UI-Password", "") != UI_PASSWORD:
        raise HTTPException(status_code=403, detail="كلمة مرور خاطئة")

@app.get("/ui", response_class=HTMLResponse)
async def ui_page():
    return _HTML

@app.get("/ui/status", dependencies=[Depends(verify_ui)])
async def ui_status():
    return {
        "authenticated":    is_authenticated(),
        "auth_in_progress": _auth_proc is not None and _auth_proc.returncode is None,
        "auth_url":         _auth_url,
    }

@app.post("/ui/start-login", dependencies=[Depends(verify_ui)])
async def ui_start_login():
    global _auth_proc, _auth_url, _auth_master_fd
    _auth_url = None

    # نفتح pseudo-TTY علشان الـ claude CLI يعتقد إنه في terminal
    master_fd, slave_fd = pty.openpty()

    # Set wide terminal BEFORE starting subprocess so Claude CLI reads correct size at startup
    # 600 cols fits the ~450-char OAuth URL on one line without excess padding
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack('HHHH', 50, 600, 0, 0))

    env = {**os.environ, "HOME": "/home/claude", "USER": "claude", "LOGNAME": "claude"}

    _auth_proc = await asyncio.create_subprocess_exec(
        "claude",
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=os.setsid,
        env=env,
    )
    os.close(slave_fd)
    _auth_master_fd = master_fd

    # non-blocking read
    fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    url_re      = re.compile(r"https://claude\.com/cai/oauth/authorize\S+")
    ansi_re     = re.compile(r'\x1b\[[^a-zA-Z]*[a-zA-Z]|\x1b[^[]')
    output      = b""
    deadline    = asyncio.get_event_loop().time() + 60
    theme_sent  = False
    login_sent  = False

    while asyncio.get_event_loop().time() < deadline:
        try:
            chunk = os.read(master_fd, 1024)
            if chunk:
                output += chunk
                decoded = output.decode(errors="replace")
                log.info(f"Auth pty output: {chunk!r}")

                # اختيار الـ theme تلقائياً (شاشة الإعداد الأولى)
                if not theme_sent and b"Choose" in output:
                    theme_sent = True
                    os.write(master_fd, b"\r")
                    log.info("Auth: auto-selected theme (Enter)")

                # اختيار "Claude account" تلقائياً (شاشة login method)
                if not login_sent and b"Select" in output and b"login" in output:
                    login_sent = True
                    os.write(master_fd, b"\r")
                    log.info("Auth: auto-selected login method 1 (Claude account)")

                # Strip ANSI codes and rejoin URL fragments split by line-wrap
                clean = ansi_re.sub('', decoded)
                clean = re.sub(r'(https://\S+)\r\r\n(\S)', r'\1\2', clean)
                clean = re.sub(r'(https://\S+)\r\r\n(\S)', r'\1\2', clean)
                m = url_re.search(clean)
                if m:
                    _auth_url = m.group(0).rstrip(")")
                    return {"url": _auth_url}
        except BlockingIOError:
            await asyncio.sleep(0.2)
        except OSError:
            break

    decoded = output.decode(errors="replace")
    log.warning(f"Auth: no URL found. Full output: {decoded!r}")
    try:
        os.close(master_fd)
    except OSError:
        pass
    _auth_master_fd = None
    _auth_proc = None
    raise HTTPException(status_code=500, detail=f"لم يتم العثور على رابط OAuth — output: {decoded[:300]}")

@app.post("/ui/submit-code", dependencies=[Depends(verify_ui)])
async def ui_submit_code(request: Request):
    global _auth_proc, _auth_url, _auth_master_fd
    body = await request.json()
    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="الكود مطلوب")
    if not _auth_proc or _auth_proc.returncode is not None:
        raise HTTPException(status_code=400, detail="لا توجد جلسة مصادقة نشطة")

    os.write(_auth_master_fd, (code + "\n").encode())

    try:
        await asyncio.wait_for(_auth_proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="انتهت المهلة")

    try:
        os.close(_auth_master_fd)
    except OSError:
        pass
    success = _auth_proc.returncode == 0
    _auth_proc      = None
    _auth_url       = None
    _auth_master_fd = None
    return {"success": success, "authenticated": is_authenticated()}

@app.post("/ui/cancel-auth", dependencies=[Depends(verify_ui)])
async def ui_cancel_auth():
    global _auth_proc, _auth_url, _auth_master_fd
    if _auth_proc and _auth_proc.returncode is None:
        _auth_proc.kill()
    if _auth_master_fd is not None:
        try:
            os.close(_auth_master_fd)
        except OSError:
            pass
    _auth_proc      = None
    _auth_url       = None
    _auth_master_fd = None
    return {"cancelled": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
