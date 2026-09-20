"""第三层：智能导航 —— 无障碍路线规划、语音交互、实时纠偏。

本期只出边界。组员实现要点：

  · 路线规划调高德/百度 API，要过滤天桥、地下通道、无电梯路段。
  · 导航指令的优先级是 PRIORITY_IMPORTANT(2)。
  · 第三层的距离来自地图/GPS，误差量级和单目深度完全不同，
    ★ 这一层可以播报具体数字（「前方 300 米右转」）。
    「障碍物不播数字」那条规则只约束第二层。
  · 只消费第一层的 objects，不要消费第二层的 SafetyAlert（热路径，不共享时钟）。
"""

from __future__ import annotations

from dataclasses import replace

from app.contracts import SOURCE_NAVIGATION, Announcement, Frame
from app.core.registry import register
from app.mock import fixtures as F


@register("layer", "navigation")
class NavigationLayer:
    source = SOURCE_NAVIGATION

    def __init__(self, planner: str = "mock") -> None:
        self.planner = planner

    async def handle(self, frame: Frame) -> list[Announcement]:
        ann = replace(F.NAVIGATION, frame_id=frame.frame_id, ts=frame.ts)
        return [ann]
