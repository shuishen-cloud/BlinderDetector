"""四层统一接口 —— 入 Frame，出 Announcement 列表。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.contracts import Announcement, Frame


@runtime_checkable
class Layer(Protocol):
    source: str

    async def handle(self, frame: Frame) -> list[Announcement]:
        """处理一帧，产出零到多条播报。"""
        ...
