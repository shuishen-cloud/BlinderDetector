"""OpenAI 兼容的 VLM 实现 —— 接真实厂商时用这个。

DashScope / 智谱 / OpenAI 都提供 OpenAI 兼容的 /chat/completions 端点，
所以同一个类换个 BASE_URL 就能用。

    # .env
    VLM_PROVIDER=dashscope
    VLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    VLM_API_KEY=sk-xxx
    VLM_MODEL=qwen-vl-max

★ 本期不接真 key，这个类不会被实例化（VLM_PROVIDER 默认 mock）。
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from app import config
from app.contracts import Frame, vision_detail
from app.core.registry import register

PROMPT = """你是视障人士的出行助手。看这张图，用一句话描述前方路况，要包含：
1. 最近的重要物体是什么、大概多远
2. 有没有需要注意的障碍
只输出那句话，不要解释。控制在 30 字以内。"""


class _OpenAICompatVLM:
    """子类只需覆盖 `name`。"""

    name = "openai_compat"

    def __init__(self) -> None:
        if not config.VLM_API_KEY:
            raise RuntimeError(
                f"VLM_PROVIDER={self.name} 需要 VLM_API_KEY，请在 .env 里填"
            )

    async def describe_frame(self, frame: Frame, image: bytes, prompt: str = PROMPT) -> dict[str, Any]:
        b64 = base64.b64encode(image).decode()
        payload = {
            "model": config.VLM_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            "max_tokens": 200,
        }
        async with httpx.AsyncClient(timeout=config.VLM_TIMEOUT_MS / 1000) as c:
            r = await c.post(
                f"{config.VLM_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {config.VLM_API_KEY}"},
                json=payload,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()

        return vision_detail(text, scene_conf=0.8)

    async def health(self) -> bool:
        return bool(config.VLM_API_KEY and config.VLM_BASE_URL)


@register("vlm", "dashscope")
class DashScopeVLM(_OpenAICompatVLM):
    name = "dashscope"


@register("vlm", "zhipu")
class ZhipuVLM(_OpenAICompatVLM):
    name = "zhipu"


@register("vlm", "openai")
class OpenAIVLM(_OpenAICompatVLM):
    name = "openai"
