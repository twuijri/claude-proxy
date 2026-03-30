# 🤖 Claude Max Proxy

> بروكسي يتصل بحساب **Claude Max** على claude.ai ويعرض **OpenAI-compatible API**  
> تربطه في LiteLLM أو أي تطبيق يدعم OpenAI API

[![Build & Push](https://github.com/YOUR_USERNAME/claude-max-proxy/actions/workflows/build.yml/badge.svg)](https://github.com/YOUR_USERNAME/claude-max-proxy/actions/workflows/build.yml)

---

## 📁 الملفات

| الملف | الاستخدام |
|---|---|
| `docker-compose.yml` | بناء الـ images محلياً وتشغيلها |
| `stack.yml` | سحب الـ images الجاهزة من ghcr.io وتشغيلها |
| `.github/workflows/build.yml` | GitHub Actions يبني الـ images تلقائياً عند كل push |

---

## 🏗️ كيف يعمل

```
LiteLLM / أي تطبيق
        │  OpenAI API
        ▼
  claude-proxy :8080
        │  يقرأ OAuth token تلقائياً
        ▼
  Volume: claude-credentials
        │  يحفظ فيه التوكن
        ▼
  claude-auth (Claude CLI)   ← تسجّل دخولك هنا مرة وحدة
        │
        ▼
    claude.ai ← حسابك Claude Max
```

---

## 🚀 طريقة 1 – docker-compose.yml (بناء محلي)

استخدم هذا إذا تبي تبني الـ image على نفس السيرفر.

```bash
# 1. استنسخ المشروع
git clone https://github.com/YOUR_USERNAME/claude-max-proxy.git
cd claude-max-proxy

# 2. إعداد البيئة
cp .env.example .env
nano .env          # عدّل PROXY_API_KEY فقط

# 3. بناء وتشغيل
docker compose up -d --build

# 4. تسجيل الدخول (مرة وحدة)
docker exec -it claude-auth claude

# 5. تحقق
curl http://localhost:8080/
```

---

## 🚀 طريقة 2 – stack.yml (images جاهزة من ghcr.io)

استخدم هذا في **Portainer** أو على أي سيرفر بدون بناء محلي.  
**ملاحظة:** يلزم أولاً رفع المشروع على GitHub وانتهاء GitHub Actions من البناء.

### عبر Command Line

```bash
# عدّل YOUR_GITHUB_USERNAME في stack.yml أولاً
# ثم:
export GITHUB_USERNAME=your_github_username
export PROXY_API_KEY=your-proxy-key

docker compose -f stack.yml up -d

# تسجيل الدخول
docker exec -it claude-auth claude
```

### عبر Portainer

```
Stacks → Add Stack → Web editor
```
الصق محتوى `stack.yml` ثم أضف في **Environment Variables**:

| Variable | Value |
|---|---|
| `GITHUB_USERNAME` | اسمك على GitHub |
| `PROXY_API_KEY` | المفتاح الذي اخترته |

---

## 🔗 إضافته في LiteLLM

في `litellm-config.yaml` الخاص بك:

```yaml
model_list:
  - model_name: claude-max
    litellm_params:
      model: openai/claude-max
      api_base: http://YOUR_SERVER_IP:8080/v1
      api_key: YOUR_PROXY_API_KEY

  - model_name: claude-sonnet
    litellm_params:
      model: openai/claude-sonnet-4-5
      api_base: http://YOUR_SERVER_IP:8080/v1
      api_key: YOUR_PROXY_API_KEY
```

> إذا LiteLLM على **نفس السيرفر** ← `http://localhost:8080/v1`

---

## 🐙 GitHub Actions – البناء التلقائي

**قبل الـ push – فعّل صلاحية الكتابة:**
> GitHub → Settings → Actions → General → Workflow permissions → ✅ **Read and write permissions**

عند كل push على `main` يبني تلقائياً:
- `ghcr.io/YOUR_USERNAME/claude-proxy:latest`
- `ghcr.io/YOUR_USERNAME/claude-auth:latest`

يدعم: `linux/amd64` + `linux/arm64`

---

## 🛡️ النماذج المتاحة

| Model Name | يشير إلى |
|---|---|
| `claude-max` | claude-opus-4-5 (الأقوى) |
| `claude-opus-4-5` | claude-opus-4-5 |
| `claude-sonnet-4-5` | claude-sonnet-4-5 |
| `claude-haiku-4-5` | claude-haiku-4-5 |

---

## 📡 Endpoints

| Endpoint | الوصف |
|---|---|
| `GET /` | الحالة العامة + حالة المصادقة |
| `GET /health` | health check |
| `POST /auth/refresh` | إعادة تحميل التوكن بدون restart |
| `GET /v1/models` | قائمة النماذج |
| `POST /v1/chat/completions` | إرسال طلب (OpenAI format) |

---

## 🔄 تجديد المصادقة

```bash
docker exec -it claude-auth claude logout
docker exec -it claude-auth claude
curl -X POST http://YOUR_SERVER_IP:8080/auth/refresh
```

---

## 🧪 اختبار سريع

```bash
curl http://YOUR_SERVER_IP:8080/v1/chat/completions \
  -H "Authorization: Bearer YOUR_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-max","messages":[{"role":"user","content":"مرحبا!"}]}'
```
