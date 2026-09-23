"""
第四层：紧急求助 —— 一键求助、跌倒检测、家属联动。

本层是编排层，两个状态机分别在：
  · 跌倒  `rules/fall.py`
  · 求助  `rules/sos.py`

★ 触发方式的能力限制（写在这里免得白做）：
  任务书里的「长按手机侧键 3 秒」在微信小程序和 Web 上**都没有对应 API**。
  所以 `trigger` 是一个集合而不是常量，端上有什么能力就上报什么。
  冗余触发是安全系统的基本要求 —— 单一触发通道等于单点故障。
"""

from __future__ import annotations

from dataclasses import fields

from app.contracts import (
    PRIORITY_CRITICAL,
    SCOPE_FAMILY,
    SOURCE_EMERGENCY,
    TRIGGER_SCREEN_LONG,
    Announcement,
    Frame,
    emergency_detail,
)
from app.core.registry import register
from app.core.rules.fall import DEFAULT_CONFIRM_MS, FallMachine, FallSignal
from app.core.rules.sos import SosMachine

#: 没给传感器数据时用的默认信号。
#: 真实场景里这些值来自端上的加速度计窗口。
DEFAULT_FALL_SIGNAL: dict = {
    "peak_g": 3.2,
    "free_fall_ms": 120,
    "posture": "lying",
    "post_impact_still_ms": 5000,
    "movement_class": "still",
    "on_charger": False,
    "screen_on": True,
}


@register("layer", "emergency")
class EmergencyLayer:
    source = SOURCE_EMERGENCY

    def __init__(self, confirm_ms: int = DEFAULT_CONFIRM_MS) -> None:
        self.fall = FallMachine(confirm_ms=confirm_ms)
        self.sos = SosMachine()

    async def handle(self, frame: Frame) -> list[Announcement]:
        extra = frame.extra or {}
        kind = extra.get("kind", "fall_signal")

        if kind == "sos":
            return self._sos(frame, extra)
        if kind == "fall_signal":
            return self._fall(frame, extra)
        if kind == "cancel":
            return self._cancel(frame, extra)
        return []

    def tick(self, now_ms: int) -> list[Announcement]:
        """推进两个状态机的时钟。由调用方定期驱动。

        ★ 注意 tick 只负责「倒计时归零 -> 升级」。
          端侧的倒计时是**本地自减**的，不靠这里的 tick 驱动 ——
          断网时倒计时仍然要走完。
        """
        return self.fall.tick(now_ms) + self.sos.tick(now_ms)

    # ------------------------------------------------------------------

    def _fall(self, frame: Frame, extra: dict) -> list[Announcement]:
        raw = {**DEFAULT_FALL_SIGNAL, **(extra.get("signal") or {})}
        allowed = {f.name for f in fields(FallSignal)}
        signal = FallSignal(**{k: v for k, v in raw.items() if k in allowed})

        event_id = extra.get("event_id") or f"fall_{frame.frame_id}"
        return self.fall.on_signal(
            signal, event_id, frame.ts, location=extra.get("location")
        )

    def _sos(self, frame: Frame, extra: dict) -> list[Announcement]:
        event_id = extra.get("event_id") or f"sos_{frame.frame_id}"
        return self.sos.trigger(
            event_id,
            # 默认用屏幕长按 —— 侧键在小程序/Web 上根本拿不到
            trigger=extra.get("trigger", TRIGGER_SCREEN_LONG),
            now_ms=frame.ts,
            idempotency_key=extra.get("idempotency_key"),
            location=extra.get("location"),
        )

    def _cancel(self, frame: Frame, extra: dict) -> list[Announcement]:
        event_id = extra.get("event_id") or ""
        method = extra.get("method", "voice")

        ann = self.fall.cancel(event_id, frame.ts, method=method)
        if ann is not None:
            return [ann]

        # 不是跌倒事件，试试求助
        if self.sos.cancel(event_id):
            return [
                Announcement(
                    text="已取消求助。",
                    ttl_ms=4000,
                    dedup_key=f"sos:{event_id}:cancelled",
                    source=SOURCE_EMERGENCY,
                    priority=PRIORITY_CRITICAL,
                    detail=emergency_detail("cancelled", event_id,
                                            contact_scope=SCOPE_FAMILY),
                )
            ]
        return []

    # ------------------------------------------------------------------

    def active_events(self) -> dict[str, list[str]]:
        return {"fall": self.fall.active_events()}
