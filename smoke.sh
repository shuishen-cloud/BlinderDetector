#!/usr/bin/env bash
# 一键自检：起服务 -> 打全部路由 -> 收工。
# 不依赖 ffmpeg / librsvg，纯 curl。
set -uo pipefail

PORT="${PORT:-8010}"
BASE="http://127.0.0.1:${PORT}"
PY="${PY:-python3}"
FAIL=0

"$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" > /tmp/lmpt_smoke.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT

for _ in $(seq 1 40); do
    curl -sf "$BASE/v1/health" > /dev/null && break
    sleep 0.25
done

hit() {  # hit <方法> <路径> <JSON体>
    local m="$1" p="$2" body="${3:-{\}}"
    local code
    code=$(curl -s -o /tmp/lmpt_out.json -w '%{http_code}' -X "$m" "$BASE$p" \
                -H 'Content-Type: application/json' -d "$body")
    if [ "$code" = "200" ]; then
        printf '  ok   %-6s %-28s %s\n' "$m" "$p" \
            "$(head -c 90 /tmp/lmpt_out.json)"
    else
        printf '  FAIL %-6s %-28s HTTP %s\n' "$m" "$p" "$code"
        FAIL=1
    fi
}

echo "== 健康检查 =="
curl -s "$BASE/v1/health" | head -c 200; echo

echo "== 七条信封路由 =="
FRAME='{"frame_id":"smoke","extra":{"index":1}}'
hit POST /v1/perception/describe "$FRAME"
hit POST /v1/safety/analyze      "$FRAME"
hit POST /v1/safety/fall         '{"frame_id":"smoke","extra":{"kind":"fall_signal"}}'
hit POST /v1/navigation/route    "$FRAME"
hit POST /v1/emergency/sos       '{"frame_id":"smoke","extra":{"kind":"sos"}}'
hit POST /v1/emergency/cancel    '{"frame_id":"smoke","extra":{"kind":"cancel"}}'
hit POST /v1/emergency/tick      '{"now_ms":1000}'

echo
if [ "$FAIL" = "0" ]; then echo "全部通过。"; else echo "有失败项，看 /tmp/lmpt_smoke.log"; fi
exit "$FAIL"
