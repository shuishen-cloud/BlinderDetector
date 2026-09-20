"""Mock VLM —— 不调任何外部服务，按帧序号轮换场景。

    VLM_PROVIDER=mock    （默认）

轮换是为了模拟「画面在变」。如果每帧都返回一样的文本，会全被仲裁器
当成重复丢掉，演示时什么都看不到。

★ 同一帧序号永远得到同一场景，所以「重复提交同一帧会被去重」这条
  行为在测试里是可复现的。
"""

from __future__ import annotations

from typing import Any

from app.contracts import Frame
from app.core.registry import register
from app.mock import fixtures as F


@register("vlm", "mock")
class MockVLM:
    async def describe_frame(self, frame: Frame, image: bytes) -> dict[str, Any]:
        index = int(frame.extra.get("index", 0))
        detail = dict(F.perception_for_index(index).detail)
        # ★ 真实 VLM 不会返回我们内部的 scene_key —— 它是从 objects/ocr
        #   推导出来的。这里也去掉，让 layer 真正跑一遍分类逻辑。
        detail.pop("scene_key", None)
        return detail

    async def health(self) -> bool:
        return True
