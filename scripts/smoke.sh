#!/usr/bin/env bash
# 一键自检：起服务 -> 打全部路由 -> 关服务
#
#   bash scripts/smoke.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# ★ 下面几个内联的 python3 -c 要打 ▶，Windows 控制台默认 GBK 会直接崩。
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

PORT="${PORT:-8000}"
URL="http://127.0.0.1:$PORT"

echo "==> 启动服务 (端口 $PORT)"
# ★ 自检必须**离线、确定**：把实现钉死在 mock 上。
#   `load_dotenv()` 不覆盖已存在的环境变量，所以这几个前缀一定生效。
#
#   为什么必须有（2026-09-23 实测）：开发机一旦配了真 key（VLM_PROVIDER=dashscope），
#   `POST /v1/perception/describe` 这一拍会**真的去打云端** —— 而它没有图像，
#   接口回 400，脚本当场红给你看，问题却不在代码里。顺带也省掉百度那条的配额。
#   要验真实厂商，用 `python scripts/run_video.py <视频>`（那里有真图像）。
VLM_PROVIDER=mock DETECTOR=mock ROUTER=builtin \
  uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --log-level warning &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  curl -sf "$URL/v1/health" >/dev/null 2>&1 && break
  sleep 0.25
done

show() {
  python3 -c "
import json, sys
b = json.load(sys.stdin)
f = b['frame']
print(f\"  frame_id : {f['frame_id']}   (index={f['extra'].get('index')})\")
if not b['announcements']:
    print('  \033[33m（无播报 —— 这一帧没有需要注意的东西）\033[0m')
    sys.exit(0)
a = b['announcements'][0]
print(f\"  source   : {a['source']}   priority={a['priority']}\")
print(f\"  text     : {a['text']}\")
print(f\"  ttl/hapt : {a['ttl_ms']}ms / {a['haptic']}\")
print(f\"  dedup_key: {a['dedup_key']}\")
print(f\"  detail   : kind={a['detail']['kind']}\")
"
}

hit() {  # hit <路径> <说明> [index]
  printf '\n\033[1m--- %s\033[0m  POST %s\n' "$2" "$1"
  curl -sf -X POST "$URL$1" -H 'Content-Type: application/json' \
       -d "{\"frame_id\":\"smoke\",\"ts\":1758326400000,\"extra\":{\"index\":${3:-1}}}" | show
}

printf '\n\033[1m--- 健康检查\033[0m  GET /v1/health\n'
curl -sf "$URL/v1/health" | python3 -m json.tool

hit /v1/perception/describe "第一层 环境感知"     0
hit /v1/safety/analyze      "第二层 安全预警（有台阶）" 1
hit /v1/safety/analyze      "第二层 安全预警（无障碍）" 0
hit /v1/safety/fall         "第二层 跌倒检测"     1
hit /v1/navigation/route    "第三层 智能导航"     1
hit /v1/emergency/sos       "第四层 紧急求助"     1

printf '\n\033[1m--- 第四层 升级链\033[0m  POST /v1/emergency/tick\n'
curl -sf -X POST "$URL/v1/emergency/tick" -H 'Content-Type: application/json' \
     -d '{"now_ms":9999999999999}' | python3 -c "
import json, sys
b = json.load(sys.stdin)
if not b['announcements']:
    print('  （无升级 —— 没有待处理的紧急事件）')
for a in b['announcements']:
    print(f\"  ▶ [{a['detail'].get('contact_scope')}] {a['text']}\")
"

printf '\n\033[32m==> 全部通过\033[0m\n'
