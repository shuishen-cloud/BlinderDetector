"""
第一层：环境感知 —— 实时场景描述、OCR 播报、物体识别。

播报文本直接用 VLM 产出的 `scene_description`，端侧拿到就播，
不需要理解 objects / ocr_results 的结构。

去重键按**场景分类**（`rules/scene.py`）而不是按帧：同一个场景不用每帧
重播，场景变了才播。没有这一层，红绿灯持续 40 秒会被播 8 遍。

★ 这一层走云端 VLM，延迟 1–3 秒是可接受的（场景描述不需要秒级反应）。
  但它**不能**用来做避障 —— 那是第二层的事。
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
from app.core.rules import phrasing, scene

#: 场景描述的基准有效期。用户不需要在 2 秒内听到「前方有家咖啡店」。
SCENE_TTL_MS = 5000


def read_image(image_ref: str | None) -> bytes:
    """读帧图像。读不到返回空 —— 不因为缺一张图把整条链路打断。"""
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
        raw = await self.vlm.describe_frame(frame, read_image(frame.image_ref))
        if not raw:
            # 说不出话就必须显式降级，绝不能让用户把「没出声」
            # 理解成「环境安全」。降级通告由 /v1/health 和 main 负责。
            return []

        # 补上场景分类（真实 VLM 不会返回这个字段，它是推导出来的）
        detail = scene.apply(dict(raw))

        text = (detail.get("scene_description") or "").strip()
        if not text:
            return []

        # VLM 会幻觉。置信度低的时候措辞要传达不确定性。
        conf = float(detail.get("scene_conf", 1.0))
        text = phrasing.hedge(text, conf)
        detail["scene_conf"] = conf

        ttl_ms, _ = phrasing.ensure_ttl(SCENE_TTL_MS, text)

        return [
            Announcement(
                text=text,
                ttl_ms=ttl_ms,
                dedup_key=vision_dedup_key(detail),
                source=SOURCE_PERCEPTION,
                priority=PRIORITY_BACKGROUND,
                frame_id=frame.frame_id,
                ts=frame.ts,
                detail=detail,
            )
        ]
