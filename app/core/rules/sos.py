"""
第四层：一键求助 —— 最小可跑。

写实的部分：幂等、升级链、取消、通知范围。不接真实短信/推送通道，
通知只落到状态里。

★ 三条硬规则：

1. **幂等。** `idempotency_key` 重复触发只能产生一次外呼。
   网络重试、用户连按三次，都不能变成三个电话打给家属。

2. **不自动拨 120。** 只做到「通知家属 → 通知网格员 → 提醒用户和家属
   手动拨 120」。自动拨号是法律责任最重的一环，需要指导老师确认后
   才能启用。ESCALATION_CHAIN 里刻意没有急救电话。

3. **升级依据是「没人应答」，不是「检测不到人」。**
   和跌倒检测同一个道理：沉默不等于安全。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.contracts import (
    PRIORITY_CRITICAL,
    SCOPE_FAMILY,
    SCOPE_GRID_WORKER,
    SOURCE_EMERGENCY,
    TRIGGER_FALL,
    Announcement,
    emergency_detail,
)

#: 通知链。★ 刻意不含急救电话 —— 见模块开头的说明。
ESCALATION_CHAIN = ("family", "grid_worker")

#: 一级通知后多久没人应答就升级（毫秒）
DEFAULT_ACK_TIMEOUT_MS = 30_000

#: 各级的中文名
_TARGET_TEXT = {
    "family": "已通知您的家人",
    "grid_worker": "已同步通知社区网格员",
}

_TARGET_SCOPE = {
    "family": SCOPE_FAMILY,
    "grid_worker": SCOPE_GRID_WORKER,
}


@dataclass
class _Sos:
    event_id: str
    idempotency_key: str
    trigger: str
    started_ms: int
    location: dict | None = None
    notified: list[str] = field(default_factory=list)
    acked: bool = False
    resolved: bool = False


class SosMachine:
    def __init__(self, ack_timeout_ms: int = DEFAULT_ACK_TIMEOUT_MS) -> None:
        self.ack_timeout_ms = ack_timeout_ms
        self._active: dict[str, _Sos] = {}
        self._seen_keys: set[str] = set()  # ★ 幂等键

    # ------------------------------------------------------------------

    def trigger(
        self,
        event_id: str,
        *,
        trigger: str,
        now_ms: int,
        idempotency_key: str | None = None,
        location: dict | None = None,
    ) -> list[Announcement]:
        """发起求助。重复的 idempotency_key 会被忽略。"""
        key = idempotency_key or event_id

        if key in self._seen_keys:
            # 幂等：不重复外呼，但要让用户知道仍然是生效的
            return [
                Announcement(
                    text="求助已在进行中，正在联系您的家人。",
                    ttl_ms=5000,
                    dedup_key=f"sos:{event_id}:dup",
                    source=SOURCE_EMERGENCY,
                    priority=PRIORITY_CRITICAL,
                    detail=emergency_detail("notifying", event_id,
                                            contact_scope=SCOPE_FAMILY)
                    | {"duplicate": True},
                )
            ]

        self._seen_keys.add(key)
        sos = _Sos(
            event_id=event_id,
            idempotency_key=key,
            trigger=trigger,
            started_ms=now_ms,
            location=location,
        )
        self._active[event_id] = sos

        return [self._notify(sos, ESCALATION_CHAIN[0], step=0)]

    # ------------------------------------------------------------------

    def tick(self, now_ms: int) -> list[Announcement]:
        """推进时钟：一级通知超时未应答 -> 升级到下一级。"""
        out: list[Announcement] = []
        for sos in list(self._active.values()):
            if sos.resolved or sos.acked:
                continue
            step = len(sos.notified)
            if step >= len(ESCALATION_CHAIN):
                continue  # 链条走完，等人工介入
            if now_ms - sos.started_ms < self.ack_timeout_ms * step:
                continue
            out.append(self._notify(sos, ESCALATION_CHAIN[step], step=step))
        return out

    # ------------------------------------------------------------------

    def acknowledge(self, event_id: str, now_ms: int) -> Announcement | None:
        """家属或网格员回应了，停止升级。"""
        sos = self._active.get(event_id)
        if sos is None or sos.acked:
            return None
        sos.acked = True
        return Announcement(
            text="家人已收到您的求助，正在赶来。",
            ttl_ms=8000,
            dedup_key=f"sos:{event_id}:acked",
            source=SOURCE_EMERGENCY,
            priority=PRIORITY_CRITICAL,
            detail=emergency_detail("acknowledged", event_id,
                                    contact_scope=SCOPE_FAMILY)
            | {"notified": list(sos.notified)},
        )

    def cancel(self, event_id: str) -> bool:
        """用户自己撤销求助。"""
        sos = self._active.get(event_id)
        if sos is None:
            return False
        sos.resolved = True
        self._active.pop(event_id, None)
        return True

    # ------------------------------------------------------------------

    def is_active(self, event_id: str) -> bool:
        sos = self._active.get(event_id)
        return sos is not None and not sos.resolved

    def _notify(self, sos: _Sos, target: str, *, step: int) -> Announcement:
        sos.notified.append(target)
        text = _TARGET_TEXT.get(target, "已发出求助")

        return Announcement(
            text=text,
            ttl_ms=10_000,
            # ★ dedup_key 带 event_id + target，保证「通知家属」和「通知网格员」
            #   不会被去重吞掉其中一条
            dedup_key=f"sos:{sos.event_id}:notify:{target}",
            source=SOURCE_EMERGENCY,
            priority=PRIORITY_CRITICAL,
            haptic="long",
            detail=emergency_detail(
                "notifying",
                sos.event_id,
                contact_scope=_TARGET_SCOPE.get(target, SCOPE_FAMILY),
                location=sos.location,
            )
            | {
                "trigger": sos.trigger,
                "escalation_step": step,
                "notified": list(sos.notified),
                # ★ 通知链走完只「建议」手动拨号，绝不自动拨
                "advise_manual_call": step >= len(ESCALATION_CHAIN) - 1,
                "trigger_was_fall": sos.trigger == TRIGGER_FALL,
            },
        )
