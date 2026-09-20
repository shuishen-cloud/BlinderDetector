"""视频文件输入源。

用 ffmpeg 抽帧到临时目录，再按序产出 Frame —— 产出的 Frame 和
ImageSequenceSource 完全一致，下游无感。

    FRAME_SOURCE=video
    python scripts/run_video.py data/demo.mp4
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Iterator

from app.contracts import SOURCE_PERCEPTION, Frame
from app.core.registry import register


@register("framesource", "video")
class VideoFileSource:
    def __init__(
        self,
        path: str = "data/demo.mp4",
        fps: float = 1.0,
        source: str = SOURCE_PERCEPTION,
    ) -> None:
        self.path = path
        self.fps = fps
        self.source = source

        if not os.path.isfile(path):
            raise FileNotFoundError(f"视频不存在: {path}")

        if shutil.which("ffmpeg") is None:
            raise RuntimeError("需要 ffmpeg: pkg install ffmpeg")

        self._tmp = tempfile.mkdtemp(prefix="lingmou_frames_")
        self._files = self._extract()

    def _extract(self) -> list[str]:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", self.path,
            "-vf", f"fps={self.fps}",
            os.path.join(self._tmp, "%04d.png"),
        ]
        subprocess.run(cmd, check=True)
        return sorted(
            os.path.join(self._tmp, f)
            for f in os.listdir(self._tmp)
            if f.endswith(".png")
        )

    def __iter__(self) -> Iterator[Frame]:
        for i, fp in enumerate(self._files):
            yield Frame(
                frame_id=f"f{i:04d}",
                ts=1_758_326_400_000 + int(i * 1000 / self.fps),
                image_ref=fp,
                source=self.source,
                extra={"index": i, "total": len(self._files)},
            )

    def count(self) -> int:
        return len(self._files)

    def cleanup(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __del__(self) -> None:
        self.cleanup()
