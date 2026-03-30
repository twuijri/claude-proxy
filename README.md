# 🤖 Claude Max Proxy

> Container واحد يجمع **Claude CLI** + **OpenAI-compatible API**  
> سجّل دخولك مرة وحدة، وابدأ تستخدم Claude Max من أي تطبيق يدعم OpenAI API

[![Build & Push](https://github.com/twuijri/claude-proxy/actions/workflows/build.yml/badge.svg)](https://github.com/twuijri/claude-proxy/actions/workflows/build.yml)

---

## 🏗️ كيف يعمل

```
docker exec -it claude-proxy claude   ← تسجيل الدخول (مرة وحدة)
                  │
                  ▼
         /root/.claude/credentials.json  (داخل الـ volume)
                  │
                  ▼
         FastAPI :8080  ←  LiteLLM / OpenWebUI / أي تطبيق
```

---

## 🚀 طريقة 1 – Portainer (الأسهل)

```
Stacks → Add Stack → Web editor
```

الصق هذا كامل:

```yaml
version: "3.9"

services:
  claude-proxy:
    image: ghcr.io/twuijri/claude-proxy:latest
    container_name: claude-proxy
    restart: unless-stopped
    environment:
      PROXY_API_KEY: ${PROXY_API_KEY:-proxy-key-change-me}
      CLAUDE_ORG_ID: ${CLAUDE_ORG_ID:-}
      CLAUDE_CREDENTIALS_FILE: /root/.claude/credentials.json
    volumes:
      - claude-credentials:/root/.claude
    ports:
      - "${PROXY_PORT:-8080}:8080"

volumes:
  claude-credentials:
```

أضف في **Environment Variables** في Portainer:

| Variable | Value |
|---|---|
| `PROXY_API_KEY` | اختر مفتاح عشوائي (مثال: `mysecretkey123`) |
| `PROXY_PORT` | `8080` (اختياري) |

---

## 🚀 طريقة 2 – docker compose (بناء محلي)

```bash
# 1. استنسخ المشروع
git clone https://github.com/twuijri/claude-proxy.git
cd claude-proxy

# 2. إعداد البيئة
cp .env.example .env
nano .env   # عدّل PROXY_API_KEY

# 3. بناء وتشغيل
docker compose up -d --build
```

---

## 🔐 تسجيل الدخول (مرة وحدة)

بعد تشغيل الـ container:

```bash
docker exec -it claude-proxy claude
```

سيظهر لك رابط – افتحه في المتصفح وسجّل دخولك بحساب Claude Max.  
بعد النجاح، الـ API يشتغل تلقائياً بدون restart.

> **الـ credentials محفوظة في Volume** – تبقى حتى لو حذفت الـ container وأعدت تشغيله.

### التحقق من الاتصال:

```bash
curl http://YOUR_SERVER_IP:8080/
```

الرد المتوقع بعد تسجيل الدخول:
```json
{
  "service": "Claude Max Proxy",
  "authenticated": true,
  "auth_source": "claude-cli"
}
```

---

## 🔗 إضافته في LiteLLM

في `litellm-config.yaml`:

```yaml
model_list:
  - model_name: claude-sonnet-4-6
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_base: http://YOUR_SERVER_IP:8080/v1
      api_key: YOUR_PROXY_API_KEY

  - model_name: claude-opus-4-6
    litellm_params:
      model: anthropic/claude-opus-4-6
      api_base: http://YOUR_SERVER_IP:8080/v1
      api_key: YOUR_PROXY_API_KEY
```

> إذا LiteLLM على **نفس السيرفر** ← `http://localhost:8080/v1`

---

## 🛡️ النماذج المتاحة

| Model Name | الموديل |
|---|---|
| `claude-sonnet-4-6` | ✅ الافتراضي – الأحدث |
| `claude-opus-4-6` | الأقوى |
| `claude-sonnet-4-5` | جيل سابق |
| `claude-opus-4-5` | جيل سابق |
| `claude-haiku-4-5` | الأسرع |
| `claude-sonnet` | → claude-sonnet-4-6 |
| `claude-opus` | → claude-opus-4-6 |
| `gpt-4` | → claude-opus-4-6 |
| `gpt-4o` | → claude-sonnet-4-6 |

---

## 📡 Endpoints

| Endpoint | الوصف |
|---|---|
| `GET /` | الحالة العامة + حالة المصادقة |
| `GET /health` | health check |
| `POST /auth/refresh` | إعادة تحميل الـ token بدون restart |
| `GET /v1/models` | قائمة النماذج |
| `POST /v1/chat/completions` | OpenAI-compatible chat |

---

## 🔄 تجديد المصادقة (إذا انتهت الجلسة)

```bash
docker exec -it claude-proxy claude logout
docker exec -it claude-proxy claude
curl -X POST http://YOUR_SERVER_IP:8080/auth/refresh
```

---

## 🧪 اختبار سريع

```bash
curl http://YOUR_SERVER_IP:8080/v1/chat/completions \
  -H "Authorization: Bearer YOUR_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"مرحبا!"}]}'
```

---

## 🐙 GitHub Actions

عند كل push على `main` يبني تلقائياً:
- `ghcr.io/twuijri/claude-proxy:latest` (amd64 + arm64)

**قبل الـ push:** فعّل صلاحية الكتابة:
> GitHub → Settings → Actions → General → Workflow permissions → ✅ **Read and write permissions**
