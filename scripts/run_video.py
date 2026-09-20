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

MARK = {"sent": "\033[32m▶ 发出\033[0m"}


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
    # fps 只有视频源认 —— 图片序列按文件名顺序走，没有抽帧这回事
    kwargs = {"path": args.path, "fps": args.fps} if kind == "video" else {"path": args.path}
    try:
        src = registry.get("framesource", kind, **kwargs)
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

    print("\n" + "=" * 68)
    print(f"共发出 {len(arb.sent)} 条，丢弃 {len(arb.dropped)} 条")
    if arb.dropped:
        print("丢弃原因：")
        for ann, reason in arb.dropped:
            print(f"  ✕ {reason:18s} {ann.text[:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
