"""用云端 Qwen-VL 当障碍物检测器。

    # .env
    DETECTOR=qwen_vl
    DETECTOR_MODEL=qwen-vl-max      # 可省，默认跟 VLM_MODEL 走

★ 用之前请先读完这一段：**这个实现违反 design.md D2。**

  D2 的原话是「第二层永远不能调 VLM」，理由是延迟量级：第二层是热路径，
  预算 <200ms，而云端 VLM 单次 1–3 秒。拿它做避障，播报出来时用户已经
  走过去了 —— 那是这套系统最不能接受的失效。

  那为什么还留着它：

    ① 联调期手边只有 VLM，没有端侧/本地的轻量检测模型。用它能把第二层
       整条「检测 → 分级 → 措辞」链路**真的跑起来**（而不是 mock 的固定
       场景），规则层那部分因此得到真输入。
    ② 它还是「为什么不能一个模型全干」最直观的**对照实验**：同一段视频，
       `DETECTOR=mock` 与 `DETECTOR=qwen_vl` 各跑一遍，延迟差一个数量级。
       答辩要讲 D2 时，这就是证据，而不是一句断言。

  所以它必须是**显式选择**，任何时候都不是默认值（默认仍是 `mock`）。
  接端侧/本地检测模型时，这个文件一行都不用动 —— 那是另一个实现的事，
  规则层和措辞层共用。

★ 距离是**问模型要的估计值**，不是量出来的。所以每条都带
  `distance_sigma_m = SIGMA_RATIO × distance_m`，由 `rules/risk.py` 的
  悲观分级（`distance - sigma`）去兜底 —— 那正是 design.md D3 的用法。

★ 失败必须看得见：调用超时 / 401 / 429 / 返回的不是 JSON，一律**抛异常**，
  绝不返回空列表。返回空列表等于告诉上游「前方没有障碍」，而真相是
  「这一拍根本没在看」—— 用户听不出区别，会把沉默当成安全。
  `layers/safety.py` 接住这个异常，转成一条**听得见**的降级播报。
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from app import config
from app.contracts import OBSTACLE_TYPES, Frame
from app.core.images import image_mime, read_image
from app.core.registry import register

#: 距离估计的**相对**误差。对齐 design.md D3 的「单目深度估计误差 30–50%」。
#: ★ 不要为了「看起来更准」把它调小：分级用的是 `distance - sigma`，
#:   调小 sigma 就是让分级变乐观，正好把 D3 要防的那类事故放进来。
SIGMA_RATIO = 0.40

#: 报出来超过这个距离的「障碍物」对避障没有意义，多半是模型在描述远处景物 —— 丢。
MAX_DISTANCE_M = 20.0

#: 模型没给 confidence 时按这个值算。正好卡在 `rules/risk.py` 的门限（0.50）上：
#: 要不要说由规则层定，不在这里替它拍板。
DEFAULT_CONFIDENCE = 0.50

POSITIONS = ("left", "center", "right")

PROMPT = """你在给视障人士做**近距离避障**。看这一帧画面，只列出用户正前方 \
6 米以内、会挡路或让人踩空的东西。

只输出一个 JSON 数组，不要解释、不要 markdown 代码块。每一项四个字段：
{"type": "step_down|step_up|pothole|curb|vehicle|bicycle|pole|person|other",
 "position": "left|center|right",
 "distance_m": 1.8,
 "confidence": 0.7}

规则：
- 正前方没有挡路的东西就输出 []。
- distance_m 是**你估计的**米数，估不准也要给一个数，不要写 null。
- confidence 是你有多确定，0 到 1。
- 只报会影响走路的：不要报天上的、楼上的、马路对面很远的、以及正在远处走过的人。
"""


class DetectorError(RuntimeError):
    """检测器这一拍没能工作。

    ★ 与「前方没有障碍」是两件事，所以是异常而不是空列表 —— 见文件头。
    """


def _extract_json_array(text: str) -> list[Any]:
    """从模型的话里挖出 JSON 数组。纯函数，好测。

    模型不总是听话：常见的是包一层 ```json 代码块，或者前面先来一句
    「好的，这是结果：」。所以先直接试，失败再退到「取第一个 [ 到最后一个 ]」。
    """
    candidates: list[str] = [text.strip()]
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for cand in candidates:
        try:
            data = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(data, list):
            return data
        raise DetectorError(f"模型返回的不是数组：{cand[:200]}")

    raise DetectorError(f"模型返回的不是 JSON：{text[:200]}")


def parse_obstacles(text: str) -> list[dict[str, Any]]:
    """把模型的话解析成契约形状的检测结果。纯函数，好测。

    ★ 逐条校验、**不合规就丢**，不要「修一修再用」：
      模型会编出不存在的类型（「stairs」「traffic_cone」）、把距离写成字符串、
      把 position 写成「前方」。这些进了规则层只会变成莫名其妙的播报 ——
      而按 D3 的态度，误报比漏报更伤（信任是消耗品）。
    """
    out: list[dict[str, Any]] = []
    for item in _extract_json_array(text):
        if not isinstance(item, dict):
            continue
        otype = item.get("type")
        if otype not in OBSTACLE_TYPES:
            continue
        try:
            distance = float(item.get("distance_m"))
        except (TypeError, ValueError):
            continue
        if not 0.0 < distance <= MAX_DISTANCE_M:
            continue
        try:
            confidence = float(item.get("confidence", DEFAULT_CONFIDENCE))
        except (TypeError, ValueError):
            confidence = DEFAULT_CONFIDENCE
        position = item.get("position")
        out.append({
            "type": otype,
            "position": position if position in POSITIONS else "center",
            "distance_m": round(distance, 2),
            # ★ 单目估计必须带误差，不能只给一个精确数字（contracts 里写了）。
            "distance_sigma_m": round(distance * SIGMA_RATIO, 2),
            "confidence": min(max(confidence, 0.0), 1.0),
            # 一帧里量不出接近速度，也追不出 track_id —— 不编。
            # rules/risk.py 对 None 是保守处理的（不走 TTC，只看距离档位）。
            "closing_speed_mps": None,
            "track_id": None,
        })
    return out


@register("detector", "qwen_vl")
class QwenVlDetector:
    """让 Qwen-VL 直接吐障碍物列表。★ 显式选择：`DETECTOR=qwen_vl`。"""

    def __init__(self, model: str | None = None) -> None:
        if not config.VLM_API_KEY:
            raise RuntimeError("DETECTOR=qwen_vl 需要 VLM_API_KEY，请在 .env 里填")
        if not config.VLM_BASE_URL:
            raise RuntimeError("DETECTOR=qwen_vl 需要 VLM_BASE_URL，请在 .env 里填")
        self.base_url = config.VLM_BASE_URL.rstrip("/")
        self.model = model or config.DETECTOR_MODEL or config.VLM_MODEL or "qwen-vl-max"
        #: 这一层的预算本来是 200ms，所以超时**故意**短于第一层的
        #: `VLM_TIMEOUT_MS`：宁可这一拍失败得看得见，也不要让一帧卡 8 秒。
        self.timeout_s = config.DETECTOR_TIMEOUT_MS / 1000

    async def detect(self, frame: Frame) -> list[dict[str, Any]]:
        image = read_image(frame.image_ref)
        if not image:
            # 「没有图」不等于「前方没有障碍」，按失败处理。
            raise DetectorError(f"读不到帧图像：{frame.image_ref!r}")
        return parse_obstacles(await self._ask(image))

    async def _ask(self, image: bytes) -> str:
        b64 = base64.b64encode(image).decode()
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{image_mime(image)};base64,{b64}"}},
                ],
            }],
            "max_tokens": 500,
            # 检测要的是可复现，不是文采 —— 但这也意味着同一帧的结果稳定，
            # 闸门的去重因此更容易命中（这正是我们要的：场景没变就别重播）。
            "temperature": 0,
        }
        url = f"{self.base_url}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                r = await c.post(
                    url,
                    headers={"Authorization": f"Bearer {config.VLM_API_KEY}"},
                    json=payload,
                )
        except Exception as e:                      # 超时 / DNS / 连接被拒
            raise DetectorError(f"调用失败：{type(e).__name__}: {e}") from e

        if r.status_code != 200:
            raise DetectorError(f"HTTP {r.status_code}：{r.text[:200]}")
        try:
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            raise DetectorError(f"响应结构不对：{r.text[:200]}") from e

    async def health(self) -> bool:
        """配置齐了就算可用 —— **不发网络请求**。

        /v1/health 会被前端定时轮询，在这里打厂商接口等于按秒烧配额。
        真实可用性由每次 `detect()` 的异常暴露（见 layers/safety.py）。
        """
        return bool(config.VLM_API_KEY and config.VLM_BASE_URL)
