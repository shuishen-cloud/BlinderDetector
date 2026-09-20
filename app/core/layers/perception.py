"""第一层：环境感知 —— 实时场景描述、OCR 播报、物体识别。

播报文本由 VLM 产出的 detail 里的 scene_description 直接充当，
端侧拿到就播，不需要理解 objects/ocr_results 的结构。

去重键按场景分类（vision:scene:<scene_key>）：同一个场景不用每帧重播，
场景变了才播。否则红绿灯持续 40 秒会被播 8 遍。

接真模型：把 .env 的 VLM_PROVIDER 改成 dashscope / zhipu / openai，本文件不用动。
"""

from __future__ import annotations

import os

from app.contracts import (
    PRIORITY_BACKGROUND,
    SOURCE_PERCEPTION,
    Announcement,
    Frame,
    vision_dedup_key,
)
from app.core.registry import get, register

SCENE_TTL_MS = 5000


def read_image(image_ref: str | None) -> bytes:
    """读帧图像。读不到就返回空 —— 不因为缺图把整条链路打断。"""
    if not image_ref or not os.path.isfile(image_ref):
        return b""
    with open(image_ref, "rb") as f:
        return f.read()


@register("layer", "perception")
class PerceptionLayer:
    source = SOURCE_PERCEPTION

    def __init__(self, vlm_provider: str | None = None) -> None:
        from app import config

        self.vlm = get("vlm", vlm_provider or config.VLM_PROVIDER)

    async def handle(self, frame: Frame) -> list[Announcement]:
        detail = await self.vlm.describe_frame(frame, read_image(frame.image_ref))

        text = detail.get("scene_description", "").strip()
        if not text:
            # 说不出话就必须显式降级，不能让用户以为「没出声 == 安全」
            return []

        return [
            Announcement(
                text=text,
                ttl_ms=SCENE_TTL_MS,
                dedup_key=vision_dedup_key(detail),
                source=SOURCE_PERCEPTION,
                priority=PRIORITY_BACKGROUND,
                frame_id=frame.frame_id,
                ts=frame.ts,
                detail=detail,
            )
        ]
