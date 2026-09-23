"""`ASR=none` —— 没有接语音识别（**默认**）。

★ 为什么不叫 `mock`：这个项目里 `mock` 的含义是「假数据、但链路跑通」
  （见 `providers/mock_vlm.py`：按帧序号轮换场景）。语音这里**没有**对应的
  东西 —— 一个编出「太原站」的假识别器比没有识别器危险得多：用户说的可能是
  「北京西站」，而他会照着一句凭空来的地名走下去。所以这里如实叫 `none`，
  并且**抛**异常让上游说出「没有接识别」。

★ `health()` 返回 False：它表示「这个实现不做识别」，让 `/v1/asr` 能给出
  原因码 `asr_unavailable`。它**不**意味着系统故障 —— 理由见包注释。
"""

from __future__ import annotations

from app.core.asr.base import ASRError
from app.core.registry import register


@register("asr", "none")
class NoASR:
    name = "none"
    unavailable_reason = "asr_unavailable"

    async def transcribe(self, audio: bytes, fmt: str) -> str:
        raise ASRError("服务端没有接语音识别（ASR=none）")

    async def health(self) -> bool:
        return False
