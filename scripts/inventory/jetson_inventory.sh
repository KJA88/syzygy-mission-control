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
command -v tegrastats >/dev/null 2>&1 && timeout 3 tegrastats --interval 1000 | head || true

echo '=== PORTS ==='
ss -ltnp || true

echo '=== SERVICES ==='
systemctl list-units --type=service --all 'vision*' 'dhras*' 'jetson*' 'mcp*' 'cloudflared*' 'syzygy*' || true
systemctl list-unit-files 'vision*' 'dhras*' 'jetson*' 'mcp*' 'cloudflared*' 'syzygy*' || true
for u in vision-dashboard.service vision-frontyard.service vision-backyard.service vision-indoor.service; do
  systemctl is-active "$u" || true
  systemctl is-enabled "$u" || true
done

echo '=== PROCESSES ==='
ps aux | grep -E 'vision_service|dashboard.py|mcp|cloudflared|python' | grep -v grep || true

echo '=== DHRAS HTTP ==='
curl -sS --max-time 3 http://127.0.0.1:8080/api/cameras/status || true; echo
curl -sS --max-time 3 http://127.0.0.1:8081/health || true; echo
for cam in frontyard backyard indoor; do
  curl -sS --max-time 3 "http://127.0.0.1:8081/status/$cam" || true; echo
done

echo '=== RUNTIME PATH CHECK ==='
ls /home/KA_JET/robotics/jetson-vision 2>/dev/null | head || true
