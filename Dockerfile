# ══════════════════════════════════════════════════════════════════════════════
#  Claude Max Proxy – Single Container
#  Python 3.12 (FastAPI) + Node 20 (Claude CLI) + supervisord
# ══════════════════════════════════════════════════════════════════════════════

FROM python:3.12-slim

WORKDIR /app

# ── 1. تثبيت Node.js 20 (لـ Claude CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        supervisor \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── 2. تثبيت Claude CLI
RUN npm install -g @anthropic-ai/claude-code

# ── 3. تثبيت Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── 4. نسخ الكود
COPY main.py .

# ── 5. إنشاء مستخدم غير root (لأن --dangerously-skip-permissions محظور على root)
RUN useradd -m -s /bin/bash claude \
    && mkdir -p /home/claude/.claude \
    && chown -R claude:claude /home/claude /app

# ── 6. إعداد supervisord
COPY supervisord.conf /etc/supervisor/conf.d/claude-proxy.conf

# ── 7. entrypoint لإصلاح صلاحيات الـ volume
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

CMD ["/entrypoint.sh"]
