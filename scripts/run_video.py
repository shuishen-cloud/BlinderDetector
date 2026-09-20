#!/usr/bin/env python3
"""喂一段视频或图片序列，打印每帧产生的播报。

    python scripts/run_video.py data/demo.mp4
    python scripts/run_video.py data/frames --source images

不需要起服务器，也不需要 API key。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import registry  # noqa: E402
from app.core.arbiter import Arbiter  # noqa: E402

MARK = {"spoken": "\033[32m▶ 播报\033[0m", "queued": "\033[33m… 排队\033[0m"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="跑一遍测试素材，看产生了哪些播报")
    p.add_argument("path", nargs="?", default="data/demo.mp4",
                   help="视频文件或图片目录（默认 data/demo.mp4）")
    p.add_argument("--source", choices=["video", "images"], default=None,
                   help="输入源类型，默认按路径后缀猜")
    p.add_argument("--fps", type=float, default=1.0, help="视频抽帧频率")
    p.add_argument("--layers", default="perception,safety",
                   help="跑哪几层，逗号分隔")
    return p.parse_args()


def guess_source(path: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    return "images" if Path(path).is_dir() else "video"


async def main() -> int:
    args = parse_args()
    registry.load_all()

    kind = guess_source(args.path, args.source)
    try:
        src = registry.get("framesource", kind, path=args.path, fps=args.fps)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"无法打开输入源: {e}", file=sys.stderr)
        print("先生成测试素材: bash scripts/make_test_video.sh", file=sys.stderr)
        return 1

    layer_names = [s.strip() for s in args.layers.split(",") if s.strip()]
    layers = [registry.get("layer", n) for n in layer_names]
    arb = Arbiter()

    print(f"输入源 : {kind}  ({src.count()} 帧)")
    print(f"参与层 : {', '.join(layer_names)}")
    print("=" * 68)

    for frame in src:
        print(f"\n[{frame.frame_id}] {Path(frame.image_ref).name}")
        for layer in layers:
            for ann in await layer.handle(frame):
                verdict = arb.submit(ann, frame.ts)
                tag = MARK.get(verdict, f"\033[31m✕ {verdict}\033[0m")
                print(f"  {layer.source:11s} {tag}  {ann.text}")
        # 模拟当前播报在下一帧到来前播完
        nxt = arb.finish_current(frame.ts + 1000)
        if nxt is not None:
            print(f"  {'(接续)':11s} {MARK['spoken']}  {nxt.text}")

    print("\n" + "=" * 68)
    print(f"共播出 {len(arb.spoken)} 条，丢弃 {len(arb.dropped)} 条")
    if arb.dropped:
        print("丢弃原因：")
        for ann, reason in arb.dropped:
            print(f"  ✕ {reason:18s} {ann.text[:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
