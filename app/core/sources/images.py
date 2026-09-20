"""图片序列输入源 —— 读一个目录里的图片，按文件名排序逐帧产出。

    FRAME_SOURCE=images
    python scripts/run_video.py data/frames
"""

from __future__ import annotations

import os
from typing import Iterator

from app.contracts import SOURCE_PERCEPTION, Frame
from app.core.registry import register

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".ppm", ".webp")


@register("framesource", "images")
class ImageSequenceSource:
    def __init__(self, path: str = "data/frames", source: str = SOURCE_PERCEPTION) -> None:
        self.path = path
        self.source = source
        if not os.path.isdir(path):
            raise FileNotFoundError(f"图片目录不存在: {path}")
        self._files = sorted(
            os.path.join(path, f)
            for f in os.listdir(path)
            if f.lower().endswith(IMAGE_EXTS)
        )

    def __iter__(self) -> Iterator[Frame]:
        for i, fp in enumerate(self._files):
            yield Frame(
                frame_id=f"f{i:04d}",
                ts=1_758_326_400_000 + i * 1000,  # 固定基准 + 每帧 1 秒
                image_ref=fp,
                source=self.source,
                extra={"index": i, "total": len(self._files)},
            )

    def count(self) -> int:
        return len(self._files)
