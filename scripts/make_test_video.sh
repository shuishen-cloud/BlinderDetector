#!/usr/bin/env bash
# 生成测试视频：assets/*.svg -> data/frames/*.png -> data/demo.mp4
#
#   bash scripts/make_test_video.sh
#
# 矢量素材是手写的，改 assets/*.svg 就能改测试场景。
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-data/demo.mp4}"
HOLD=2          # 每张图停留几秒

# ---- 依赖 ----
for t in rsvg-convert ffmpeg; do
  if ! command -v "$t" >/dev/null 2>&1; then
    case "$t" in
      rsvg-convert) echo "==> 安装 librsvg"; pkg install -y librsvg ;;
      ffmpeg)       echo "==> 安装 ffmpeg";  pkg install -y ffmpeg ;;
    esac
  fi
done

# ---- 1. SVG -> PNG ----
mkdir -p data/frames
echo "==> 转换矢量素材"
for f in assets/*.svg; do
  out="data/frames/$(basename "${f%.svg}").png"
  rsvg-convert -w 640 -h 480 "$f" -o "$out"
  printf '    %-34s -> %s\n' "$f" "$out"
done

# ---- 2. PNG -> MP4 ----
echo "==> 拼接视频（每张停留 ${HOLD}s）"
list="$(mktemp)"
for f in data/frames/*.png; do
  echo "file '$PWD/$f'" >> "$list"
  echo "duration $HOLD" >> "$list"
done
# concat demuxer 要求最后一个文件再写一遍，否则末帧被吞
echo "file '$PWD/$(ls data/frames/*.png | tail -1)'" >> "$list"

ffmpeg -y -loglevel error -f concat -safe 0 -i "$list" \
       -vf "fps=1,format=yuv420p" -c:v libx264 -preset veryfast "$OUT"
rm -f "$list"

echo "==> 完成: $OUT"
ffprobe -v error -show_entries format=duration,size -of default=nw=1 "$OUT" 2>/dev/null || true
