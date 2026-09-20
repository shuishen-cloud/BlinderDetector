"""第二层：安全预警 —— 障碍物检测、单目深度估计、跌倒检测。

本期是 mock：按帧序号轮换场景，模拟画面变化。真实实现要点（免得组员踩坑）：

  · 这一层是热路径，延迟预算 <200ms。
    云端 VLM 单次调用 1-3 秒，所以本层绝不能走 VLM —— 必须用轻量检测模型。
    这也是为什么第一层和第二层是两条独立流水线。

  · 单目深度估计误差 30-50%，distance_sigma_m 必须如实填。

  · 播报措辞建议用粗档（「就在脚前 / 很近 / 几米外」）而不是「2.0 米」。
    虚假的精确感会让用户按错误预期迈步 —— 误差往低估的方向是有害的。

  · 每条障碍物播报的 dedup_key 必须用 contracts.make_dedup_key() 构造。
    它把 risk 和距离档位编进键里，防止风险升级被去重吞掉。
"""

from __future__ import annotations

from dataclasses import replace

from app.contracts import SOURCE_SAFETY, Announcement, Frame
from app.core.registry import register
from app.mock import fixtures as F


@register("layer", "safety")
class SafetyLayer:
    source = SOURCE_SAFETY

    def __init__(self, analyzer: str = "mock") -> None:
        self.analyzer = analyzer

    async def handle(self, frame: Frame) -> list[Announcement]:
        index = int(frame.extra.get("index", 0))
        return [
            replace(a, frame_id=frame.frame_id, ts=frame.ts)
            for a in F.safety_for_index(index)
        ]
