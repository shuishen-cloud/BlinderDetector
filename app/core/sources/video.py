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


def ffmpeg_exe() -> str | None:
    """找一个可用的 ffmpeg，返回可执行文件路径；都没有则 None。

    先看 PATH；找不到时退回 imageio-ffmpeg 自带的静态二进制。
    ★ 这是**可选**导入 —— requirements.txt 的零编译约束不允许把它写成硬依赖
    （Termux 上没有对应 wheel），装了才生效，没装不影响其它平台。
    """
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


@register("framesource", "video")
class VideoFileSource:
    def __init__(
        self,
        path: str = "data/demo.mp4",
        # path: str = "data/BlinderRoad.mp4",
        fps: float = 1.0,
        source: str = SOURCE_PERCEPTION,
    ) -> None:
        self.path = path
        self.fps = fps
        self.source = source

        if not os.path.isfile(path):
            raise FileNotFoundError(f"视频不存在: {path}")

        # ★ 临时目录先建、再校验 ffmpeg：构造函数中途抛异常时，
        #   __del__ -> cleanup() 也一定能安全地把目录删掉，不留残留。
        self._tmp = tempfile.mkdtemp(prefix="lingmou_frames_")

        exe = ffmpeg_exe()
        if exe is None:
            raise RuntimeError(
                "需要 ffmpeg: 装系统 ffmpeg，或 pip install imageio-ffmpeg"
            )
        self._ffmpeg = exe

        self._files = self._extract()

    def _extract(self) -> list[str]:
        cmd = [
            self._ffmpeg, "-y", "-loglevel", "error",
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
        # ★ 用 getattr 兜底：构造函数提前抛异常时 _tmp 可能还不存在，
        #   而 __del__ 照样会被调用（实测会刷出 AttributeError 堆栈）。
        tmp = getattr(self, "_tmp", None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    def __del__(self) -> None:
        self.cleanup()
