FROM node:20-slim

WORKDIR /root

# تثبيت Claude CLI الرسمي
RUN npm install -g @anthropic-ai/claude-code

# مجلد بيانات المصادقة (سيتم mount هذا المجلد)
RUN mkdir -p /root/.claude

# نقطة الدخول: اعرض تعليمات تسجيل الدخول عند بدء التشغيل
CMD ["/bin/sh", "-c", "\
  echo ''; \
  echo '══════════════════════════════════════════════'; \
  echo '  Claude Auth Container'; \
  echo '══════════════════════════════════════════════'; \
  if [ -f /root/.claude/credentials.json ]; then \
    echo '  ✅ مصادق بالفعل!'; \
    echo '  لتسجيل الخروج: docker exec -it claude-auth claude logout'; \
  else \
    echo '  ⚠️  لم تسجّل دخولك بعد!'; \
    echo ''; \
    echo '  شغّل الأمر التالي لتسجيل الدخول:'; \
    echo '  docker exec -it claude-auth claude'; \
    echo '══════════════════════════════════════════════'; \
  fi; \
  tail -f /dev/null \
"]
