"""OpenAI 兼容的 VLM 实现 —— 接真实厂商时用这个。

DashScope / 智谱 / OpenAI 都提供 OpenAI 兼容的 /chat/completions 端点，
所以同一个类换个 BASE_URL 就能用。

    # .env
    VLM_PROVIDER=dashscope
    VLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    VLM_API_KEY=sk-xxx
    VLM_MODEL=qwen-vl-max

★ 为什么要 JSON 而不是「一句话」
  第一层的去重粒度由**场景分类**决定（`rules/scene.py`），而场景分类靠
  `objects` / `ocr_results` 推导 `scene_key`。只让模型说一句话的话，
  这两个字段永远为空 —— 实测后果是**三种完全不同的场景拿到同一个
  dedup_key（`vision:scene:default`）**，于是第一层会反复播同一句、
  场景变了也不播。所以 prompt 必须要求结构化输出，这不是美化。
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from app import config
from app.contracts import Frame, vision_detail
from app.core.registry import register

PROMPT = """你是视障人士的出行助手。看这张图，只输出一个 JSON 对象，不要任何解释文字。

字段要求：
{
  "scene_description": "一句话描述前方路况，包含最近的重要物体和它大概多远，30 字以内",
  "scene_conf": 0.0 到 1.0 的数字，表示你对上面这句描述的信心,
  "objects": [
    {"label": "台阶", "position": "left|center|right",
     "bbox": [x, y, w, h], "distance_m": 2.0, "confidence": 0.9}
  ],
  "ocr_results": [
    {"text": "电梯", "category": "elevator_button|doorplate|menu|sign",
     "bbox": [x, y, w, h], "confidence": 0.93}
  ]
}

规则：
- bbox 是**归一化**坐标，0 到 1，原点左上角。
- objects 只放真正看得见的重要物体，没有就给空数组。
- 看不清或不确定时，把 confidence / scene_conf 调低，**不要编**。"""


class VLMUnavailable(RuntimeError):
    """VLM 暂时不可用（超时 / 限流 / 欠费 / 网络）。

    抛出去让上层显式降级 —— 「沉默不能有歧义」：用户若不知道系统哑了，
    会把「没出声」理解成「环境安全」。
    """


# --------------------------------------------------------------------------
# 解析：把模型的回复变成 vision_detail 形状
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _detail(
    desc: str,
    *,
    scene_conf: float,
    objects: list[dict[str, Any]] | None = None,
    ocr_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造 `vision_detail`，并**去掉 scene_key**。

    ★ 这一步不能省：`scene.apply()` 只在 `scene_key` **缺失**时才去推导
      （见 rules/scene.py），而 `vision_detail()` 默认会填 `"default"` ——
      留着它，分类就永远不会跑，三种场景全都拿到
      `vision:scene:default` 这一个去重键，第一层会反复播同一句。

      mock provider 也是这么做的（`detail.pop("scene_key", None)`）。
    """
    d = vision_detail(desc, scene_conf=scene_conf,
                      objects=objects, ocr_results=ocr_results)
    d.pop("scene_key", None)
    return d


def _clamp(x: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return default


def _norm_objects(raw: Any) -> list[dict[str, Any]]:
    """只保留能用的条目 —— 模型偶尔会少字段或给错类型。"""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for o in raw:
        if not isinstance(o, dict):
            continue
        label = str(o.get("label") or "").strip()
        if not label:
            continue  # 没标签就没法做场景分类，直接丢
        pos = str(o.get("position") or "center")
        out.append({
            "label": label,
            "confidence": _clamp(o.get("confidence"), 0.8),
            "position": pos if pos in ("left", "center", "right") else "center",
            "bbox": o.get("bbox") if isinstance(o.get("bbox"), list) else None,
            "distance_m": o.get("distance_m"),
            "distance_sigma_m": o.get("distance_sigma_m"),
            "track_id": o.get("track_id"),
            "is_known": bool(o.get("is_known", False)),
            "face_id": o.get("face_id"),
        })
    return out


def _norm_ocr(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for o in raw:
        if not isinstance(o, dict):
            continue
        text = str(o.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "text": text,
            "confidence": _clamp(o.get("confidence"), 0.8),
            "category": str(o.get("category") or "sign"),
            "bbox": o.get("bbox") if isinstance(o.get("bbox"), list) else None,
        })
    return out


def parse_reply(text: str) -> dict[str, Any]:
    """把模型回复解析成 `vision_detail()` 形状。

    ★ 容错优先：解析不出来就**退化成「整段当场景描述」**。
      宁可少给结构（去重粒度变粗），也不能因为格式不对就让整条链路哑掉 ——
      前者是「播得啰嗦」，后者是「用户以为环境安全」。
    """
    raw = (text or "").strip()
    if not raw:
        return {}

    candidate = raw
    m = _FENCE.search(raw)  # 模型爱把 JSON 包在 ```json 里
    if m:
        candidate = m.group(1).strip()
    else:
        i, j = raw.find("{"), raw.rfind("}")
        if i != -1 and j > i:
            candidate = raw[i:j + 1]

    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return _detail(raw, scene_conf=0.8)  # 退化成纯文本，至少还能播

    if not isinstance(data, dict):
        return _detail(raw, scene_conf=0.8)

    desc = str(data.get("scene_description") or "").strip()
    if not desc:
        return _detail(raw, scene_conf=0.8)

    return _detail(
        desc,
        scene_conf=_clamp(data.get("scene_conf"), 0.8),
        objects=_norm_objects(data.get("objects")),
        ocr_results=_norm_ocr(data.get("ocr_results")),
    )


# --------------------------------------------------------------------------


class _OpenAICompatVLM:
    """子类只需覆盖 `name`。"""

    name = "openai_compat"

    def __init__(self) -> None:
        if not config.VLM_API_KEY:
            raise RuntimeError(
                f"VLM_PROVIDER={self.name} 需要 VLM_API_KEY，请在 .env 里填"
            )
        # 复用连接：每帧新建 AsyncClient 会重做一次 TCP+TLS 握手，
        # 而 VLM 本来就要 1–3 秒，不该再把握手叠上去。
        self._client = httpx.AsyncClient(timeout=config.VLM_TIMEOUT_MS / 1000)

    async def describe_frame(
        self, frame: Frame, image: bytes, prompt: str = PROMPT
    ) -> dict[str, Any]:
        if not image:
            raise VLMUnavailable("没有图像字节（image_ref 读不到？）")

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
            "max_tokens": 600,
        }

        try:
            r = await self._client.post(
                f"{config.VLM_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {config.VLM_API_KEY}"},
                json=payload,
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise VLMUnavailable(
                f"VLM 返回 {e.response.status_code}"
                f"（401 查 key、429 限流、5xx 换时段重试）"
            ) from e
        except httpx.HTTPError as e:
            raise VLMUnavailable(f"VLM 网络错误：{type(e).__name__}") from e

        try:
            content = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise VLMUnavailable(f"VLM 回包结构不认识：{type(e).__name__}") from e

        return parse_reply(content or "")

    async def health(self) -> bool:
        """不真的打网络 —— /v1/health 每 5 秒被轮询一次，不能每次都去 ping。"""
        return bool(config.VLM_API_KEY and config.VLM_BASE_URL and config.VLM_MODEL)

    async def aclose(self) -> None:
        await self._client.aclose()


@register("vlm", "dashscope")
class DashScopeVLM(_OpenAICompatVLM):
    name = "dashscope"


@register("vlm", "zhipu")
class ZhipuVLM(_OpenAICompatVLM):
    name = "zhipu"


@register("vlm", "openai")
class OpenAIVLM(_OpenAICompatVLM):
    name = "openai"
