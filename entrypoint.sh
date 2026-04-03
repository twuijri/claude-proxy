#!/bin/bash
# صلاح صلاحيات الملفات في الـ volume (في حال كانت مملوكة لـ root)
chown -R claude:claude /home/claude/.claude 2>/dev/null || true
exec /usr/bin/supervisord -n -c /etc/supervisor/supervisord.conf
