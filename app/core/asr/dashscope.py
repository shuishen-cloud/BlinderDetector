"""用云端 Qwen 系列 ASR 做语音识别（DashScope OpenAI 兼容端点）。

    # .env
    ASR=dashscope
    ASR_MODEL=qwen3-asr-flash        # 可省，默认就这个
    # ASR_BASE_URL / ASR_API_KEY 不填则跟着 VLM_BASE_URL / VLM_API_KEY 走

★ 实测（2026-09-23，用仓库里那把 dashscope key）：

    POST {base}/chat/completions
    {"model": "qwen3-asr-flash",
     "messages": [{"role": "user", "content": [
        {"type": "input_audio",
         "input_audio": {"data": "<base64>", "format": "wav"}}]}],
     "stream": false}
    → 200，正文在 choices[0].message.content

  几个从实测里学到的、照文档写会踩的点：

    · `format` **必须**在 `input_audio` 里给。少了它 → 400
      `{"error":{"message":"format is empty","type":"UNSUPPORTED_FORMAT"}}`。
    · `qwen-audio-3.0-asr-flash`（账号下也有）在**兼容端点**上不认这种形状 ——
      同样报 `format is empty`。同一把 key 下 `qwen3-asr-flash` 才通，
      所以默认值是它，不是「看起来更新」的那个。
    · `data` **必须是 data-URL**（`data:;base64,<...>`）。给裸 base64 会 400：
      「The provided URL does not appear to be valid. Ensure it is correctly
      formatted.」—— 厂商把 `data` 当 URL 解析。★ 这条是踩出来的：先按裸
      base64 发，一段真语音就被拒了，而**只有一句厂商报错**，从错误信息里
      看不出该改成什么形状。四种形状都试过才定下来（见
      `tests/test_asr.py::test_asr_request_shape_is_the_one_that_actually_works`）。
    · mime 留空（`data:;base64,`）：厂商只看 `format` 字段，写死 `audio/wav`
      反而会在将来传 m4a 时撒谎。实测空 mime 与 `audio/wav` 都能识别。
    · 静音 / 无语音的音频返回**空串**，不是错误 —— 这正是「没听清」的语义，
      原样透传给端侧（它会说「没听清，请再按住说一次」）。

★ 失败一律**抛 `ASRError`**：超时、401、429、返回不是预期形状。绝不返回空串
  —— 那会让「这一拍根本没在听」被当成「用户没说话」（见包注释）。

★ 凭据缺失**不算**这一类失败：它由 `health()` 返回 False 表达，端点据此报
  `asr_misconfigured`。**不在 `__init__` 里抛异常**是刻意的 ——
  `registry.get()` 就在请求处理路径上，抛出去会变成 500，而降级路径正是为了
  这种情况准备的（`routers/baidu.py` 已经踩过这个坑）。
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from app import config
from app.core.asr.base import ASRError
from app.core.registry import register


def parse_asr_response(payload: dict[str, Any]) -> str:
    """从 OpenAI 兼容响应里取正文。形状不对就抛 `ASRError`。

    ★ 抽成**纯同步函数**是为了能被同步测试覆盖 —— 与 `routers/baidu.py::
      parse_walking_response` 同一个理由：项目里没有任何 HTTP 桩先例，
      也不想为它引入新依赖。
    """
    if not isinstance(payload, dict):
        raise ASRError(f"识别响应不是对象：{type(payload).__name__}")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ASRError(f"识别响应里没有 choices：{str(payload)[:200]}")
    first = choices[0]
    if not isinstance(first, dict):
        raise ASRError("识别响应的 choices[0] 不是对象")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ASRError("识别响应里没有 message")
    content = message.get("content")
    if content is None:
        raise ASRError("识别响应里没有 content")
    if not isinstance(content, str):
        # 有些实现会把 content 塞成 [{type:text, text:...}]，认这一种。
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict)]
            return "".join(parts).strip()
        raise ASRError(f"识别正文不是字符串：{type(content).__name__}")
    return content.strip()


@register("asr", "dashscope")
class DashscopeASR:
    name = "dashscope"
    unavailable_reason = "asr_misconfigured"

    # ★ 没有 `__init__`，凭据缺失靠 `health()` 表达 —— 理由见 `ASRTool.health()`
    #   的说明：`registry.get()` 就在请求路径上，在这里抛异常会直接变成 500，
    #   而降级路径**正是**为了这种情况准备的。

    async def transcribe(self, audio: bytes, fmt: str) -> str:
        body: dict[str, Any] = {
            "model": config.ASR_MODEL,
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "input_audio",
                    "input_audio": {
                        # ★ 必须带 `data:` 前缀 —— 裸 base64 会被厂商当成 URL
                        #   解析并拒掉（原因见文件头）。mime 留空，格式由
                        #   `format` 字段说了算。
                        "data": "data:;base64," + base64.b64encode(audio).decode(),
                        "format": fmt,
                    },
                }],
            }],
            "stream": False,
        }
        url = f"{config.ASR_BASE_URL.rstrip('/')}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=config.ASR_TIMEOUT_MS / 1000) as c:
                r = await c.post(
                    url,
                    headers={"Authorization": f"Bearer {config.ASR_API_KEY}"},
                    json=body,
                )
                r.raise_for_status()
                return parse_asr_response(r.json())
        except httpx.HTTPStatusError as e:
            # ★ 把厂商的原话带出来（「format is empty」这种能直接指向 bug），
            #   但**不带**请求头 —— Authorization 就在里面。
            detail = e.response.text[:200] if e.response is not None else ""
            raise ASRError(f"识别服务返回 {e.response.status_code}：{detail}") from e
        except httpx.HTTPError as e:
            raise ASRError(f"连不上识别服务：{type(e).__name__}") from e
        except ValueError as e:                      # r.json() 失败
            raise ASRError(f"识别服务返回的不是 JSON：{e}") from e

    async def health(self) -> bool:
        return bool(config.ASR_API_KEY and config.ASR_BASE_URL)
