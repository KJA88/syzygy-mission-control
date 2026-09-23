#!/usr/bin/env bash
set -u

echo '=== HOST ==='
hostnamectl || true
ip -4 addr show scope global || true
timedatectl || true

echo '=== METRICS ==='
uptime || true
free -h || true
df -h || true
command -v vcgencmd >/dev/null 2>&1 && vcgencmd measure_temp || true

echo '=== PORTS ==='
ss -ltnp || true

echo '=== SERVICES ==='
systemctl list-units --type=service --all 'tv*' 'fitbit*' 'mcp*' 'pi-git*' 'cloudflared*' 'syzygy*' || true
systemctl list-unit-files 'tv*' 'fitbit*' 'mcp*' 'pi-git*' 'cloudflared*' 'syzygy*' || true

echo '=== PROCESSES ==='
ps aux | grep -E 'mcp|cloudflared|uvicorn|python' | grep -v grep || true

echo '=== BEST-EFFORT LOCAL HTTP ==='
for u in \
  http://127.0.0.1:8010/mcp \
  http://127.0.0.1:8010/ \
  http://127.0.0.1:8065/mcp \
  http://127.0.0.1:8065/; do
  curl -sS -o /dev/null -w '%{http_code} %{url_effective}\n' --max-time 3 "$u" || true
done
