"""VLM 抽象接口。

本期不绑定任何厂商 —— DashScope / 智谱 / OpenAI 都提供 OpenAI 兼容端点，
接哪家只改 .env 的 VLM_BASE_URL / VLM_API_KEY / VLM_MODEL。

接口收的是整个 Frame 而不只是图片字节，因为实现可能需要帧的元信息
（时间戳、来源、extra 里的上下文），而且 mock 实现靠它模拟画面变化。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.contracts import Frame


@runtime_checkable
class VLMTool(Protocol):
    async def describe_frame(self, frame: Frame, image: bytes) -> dict[str, Any]:
        """返回 contracts.vision_detail() 的形状。"""
        ...

    async def health(self) -> bool:
        """能否正常调用。降级通告靠它。"""
        ...
