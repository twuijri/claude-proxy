# Claude Max Proxy

بروكسي يحول حساب Claude Max إلى API متوافق مع OpenAI.  
سجّل دخولك مرة وحدة من الواجهة، وابدأ تستخدمه من أي تطبيق.

[![Build & Push](https://github.com/twuijri/claude-proxy/actions/workflows/build.yml/badge.svg)](https://github.com/twuijri/claude-proxy/actions/workflows/build.yml)

---

## التشغيل السريع (Portainer Stack)

```yaml
version: "3.9"

services:
  claude-proxy:
    image: ghcr.io/twuijri/claude-proxy:latest
    container_name: claude-proxy
    restart: unless-stopped
    environment:
      PROXY_API_KEY: your-secret-key
      UI_PASSWORD: your-ui-password
    volumes:
      - claude-credentials:/home/claude/.claude
    ports:
      - "8080:8080"
    networks:
      - npm_default

volumes:
  claude-credentials:

networks:
  npm_default:
    external: true
```

> إذا ما تستخدم Nginx Proxy Manager، احذف سطري `networks` من الملف.

---

## تسجيل الدخول

بعد تشغيل الـ container، افتح الواجهة:

```
http://YOUR_SERVER_IP:8080/ui
```

1. أدخل `UI_PASSWORD` التي حددتها
2. اضغط **تسجيل الدخول بـ Claude**
3. افتح الرابط الذي يظهر، وافق، وانسخ الكود
4. الصق الكود في الخانة واضغط تأكيد

الـ credentials تُحفظ في الـ volume وتبقى حتى لو أعدت تشغيل الـ container.

---

## الربط مع LiteLLM

```yaml
model_list:
  - model_name: claude-sonnet-4-6
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_base: http://claude-proxy:8080/v1
      api_key: your-secret-key

  - model_name: claude-opus-4-6
    litellm_params:
      model: anthropic/claude-opus-4-6
      api_base: http://claude-proxy:8080/v1
      api_key: your-secret-key
```

> إذا LiteLLM على نفس الشبكة (`npm_default`) استخدم اسم الـ container مباشرة بدل IP.

---

## اختبار الاتصال

```bash
curl http://YOUR_SERVER_IP:8080/health
```

```bash
curl http://YOUR_SERVER_IP:8080/v1/chat/completions \
  -H "Authorization: Bearer your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"مرحبا"}]}'
```

---

## النماذج المتاحة

| الاسم | يُوجَّه إلى |
|---|---|
| `claude-sonnet-4-6` | claude-sonnet-4-6 (الافتراضي) |
| `claude-opus-4-6` | claude-opus-4-6 |
| `claude-haiku-4-5` | claude-haiku-4-5 |
| `gpt-4o` | claude-sonnet-4-6 |
| `gpt-4` | claude-opus-4-6 |

---

## المتغيرات

| المتغير | الوصف | الافتراضي |
|---|---|---|
| `PROXY_API_KEY` | مفتاح الـ API للتطبيقات | `proxy-key-change-me` |
| `UI_PASSWORD` | كلمة مرور واجهة الويب | `admin` |
