"""输入源接口。

所有实现产出的 Frame 形状完全一致，所以下游四层不需要知道
输入到底是视频文件还是图片序列。
"""

from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable

from app.contracts import Frame


@runtime_checkable
class FrameSource(Protocol):
    def __iter__(self) -> Iterator[Frame]: ...

    def count(self) -> int:
        """总帧数，未知返回 -1。"""
        ...
