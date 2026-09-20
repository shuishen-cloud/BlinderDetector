"""
第三层：智能导航 —— 无障碍路线规划、语音交互、实时纠偏。

本期最小可跑：不接高德/百度 API，用 `rules/route.py` 里的内置路网
把数据流走通。接真实地图时改 `plan()` 即可，本文件不用动。

★ 和第一、二层的边界：
  · 导航指令的优先级是 IMPORTANT(2)，可以打断场景描述，
    但打断不了碰撞预警。
  · 本层的距离来自地图，**可信**，所以允许播报数字
    （「前方 300 米右转」）。第二层那条「障碍物不播数字」的规则
    只约束单目深度估计，不约束这里。
"""

from __future__ import annotations

from app.contracts import SOURCE_NAVIGATION, Announcement, Frame
from app.core.registry import register
from app.core.rules import route as route_rules

#: 默认的无障碍偏好（定义在 rules/route.py，用户可以在 frame.extra 里覆盖）
DEFAULT_AVOID = route_rules.DEFAULT_AVOID

#: 没有定位时的兜底坐标（天安门，方便调试时辨认）
FALLBACK_ORIGIN = (39.9087, 116.3975)


@register("layer", "navigation")
class NavigationLayer:
    source = SOURCE_NAVIGATION

    def __init__(self, planner: str = "builtin", walk_speed_mps: float | None = None) -> None:
        self.planner = planner
        self.walk_speed_mps = walk_speed_mps

    async def handle(self, frame: Frame) -> list[Announcement]:
        extra = frame.extra or {}
        req = route_rules.RouteRequest(
            origin=_origin(frame),
            destination=extra.get("destination") or "目的地",
            avoid=extra.get("avoid", DEFAULT_AVOID),
            walk_speed_mps=self.walk_speed_mps,
        )
        detail = route_rules.plan(req)
        return route_rules.step_announcements(detail, max_steps=extra.get("max_steps", 1))


def _origin(frame: Frame) -> tuple[float, float]:
    """起点坐标。位置统一放在 `frame.extra["geo"]` 里 ——
    Frame 只有五个字段，位置信息属于上下文，不占一个顶级字段。"""
    geo = (frame.extra or {}).get("geo")
    if isinstance(geo, dict) and "lat" in geo and "lng" in geo:
        return (float(geo["lat"]), float(geo["lng"]))
    return FALLBACK_ORIGIN
