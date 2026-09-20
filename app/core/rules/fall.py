"""
跌倒检测状态机 —— 安全关键。

这是整个系统里唯一一个「假阳性有真实代价、假阴性有生命代价」的功能，
所以绝不能用布尔值表达。用 `fall_detected: bool` 意味着只有一个阈值旋钮，
而这个旋钮往任何一边拧都是错的。

状态流转：

    idle ──冲击特征──> suspected ──无条件──> confirming ──倒计时归零──> confirmed
                            │                     │
                            │                     └──显式取消──> cancelled
                            └──步行节律──> cancelled

两条绝不能违反的规则：

1. **`suspected` / `confirming` 态永不自动外呼。**
   真跌倒和「把手机扔到床上」「快速坐下」在加速度计上几乎无法区分。
   必须等倒计时归零、用户始终没响应，才升到 `confirmed`。

2. **沉默 ≠ 安全。** 取消**必须**要求显式动作（语音「我没事」、按键、
   点击、摇一摇）。绝不能因为「没检测到后续运动」就自动取消 ——
   失去意识的人同样没有运动。

   推论：手机掉了但人没摔的情况下，设备躺在地上测不到人的步态，
   `movement_class` 会是 still，但这不能作为「安全」的证据。
   照常升级。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.contracts import (
    FALL_CANCELLED,
    FALL_CONFIRMED,
    FALL_SUSPECTED,
    PRIORITY_CRITICAL,
    SOURCE_EMERGENCY,
    Announcement,
    emergency_detail,
)
from app.core.rules import phrasing

# --------------------------------------------------------------------------
# 门限
# --------------------------------------------------------------------------

#: 自由落体持续多久才可能是真跌倒（毫秒）
MIN_FREE_FALL_MS = 80

#: 冲击峰值加速度门限（g）
MIN_IMPACT_G = 2.5

#: 二次确认窗口。这段时间里用户可以做显式取消。
DEFAULT_CONFIRM_MS = 15_000

#: 通知链。★ 最后一级不是自动拨 120 —— 那是法律责任最重的一环，
#: 需要指导老师确认后才启用。当前只做到「提醒用户和家属手动拨」。
ESCALATION_CHAIN = ("family", "grid_worker")


@dataclass
class FallSignal:
    """端侧上报的传感器窗口。"""

    peak_g: float
    free_fall_ms: int = 0
    posture: str = "unknown"  # lying | sitting | standing | unknown
    post_impact_still_ms: int = 0
    movement_class: str = "unknown"  # still | walking | vehicle | handheld | unknown
    on_charger: bool = False
    screen_on: bool = True
    trigger: str = "auto"


@dataclass
class _Active:
    event_id: str
    started_ms: int
    deadline_ms: int
    signal: FallSignal
    location: dict | None = None
    notified: list[str] = field(default_factory=list)


def looks_like_fall(s: FallSignal) -> bool:
    """冲击特征是否像跌倒。宽松判定 —— 宁可多问一句，不可漏掉。"""
    return s.free_fall_ms >= MIN_FREE_FALL_MS and s.peak_g >= MIN_IMPACT_G


def is_walking(s: FallSignal) -> bool:
    """步行节律是最常见的强假阳性：手机在手里被甩、跳一下、快走。"""
    return s.movement_class == "walking"


def is_benign_context(s: FallSignal) -> bool:
    """充电中 + 屏幕亮着的长时间静止，多半是手机被碰掉了。

    注意这**只是提高门槛的参考**，不构成取消理由 —— 判定为真时
    仍然走完整的二次确认流程。
    """
    return s.on_charger and s.screen_on and s.post_impact_still_ms > 60_000


class FallMachine:
    """一次跌倒事件的完整生命周期。服务端实例。"""

    def __init__(self, confirm_ms: int = DEFAULT_CONFIRM_MS) -> None:
        self.confirm_ms = confirm_ms
        self._active: dict[str, _Active] = {}
        self._resolved: dict[str, str] = {}  # event_id -> 终态

    # ------------------------------------------------------------------
    # 上报
    # ------------------------------------------------------------------

    def on_signal(
        self,
        signal: FallSignal,
        event_id: str,
        now_ms: int,
        location: dict | None = None,
    ) -> list[Announcement]:
        """处理一次传感器窗口上报，返回要推出的播报。"""
        if event_id in self._resolved or event_id in self._active:
            return []  # 幂等：同一次事件不重复起流程

        # 步行节律 -> 直接判为误报，但**不做任何外呼**
        if is_walking(signal):
            self._resolved[event_id] = FALL_CANCELLED
            return []

        if not looks_like_fall(signal):
            return []

        # 上下文可疑时把二次确认窗口拉长，给用户更多时间取消
        window = self.confirm_ms * 2 if is_benign_context(signal) else self.confirm_ms
        deadline = now_ms + window

        self._active[event_id] = _Active(
            event_id=event_id,
            started_ms=now_ms,
            deadline_ms=deadline,
            signal=signal,
            location=location,
        )

        # ★ 这里只询问，绝不外呼
        return [self._ask(event_id, window, location, PRIORITY_CRITICAL)]

    # ------------------------------------------------------------------
    # 时间推进
    # ------------------------------------------------------------------

    def tick(self, now_ms: int) -> list[Announcement]:
        """推进时钟。倒计时归零且用户没响应 -> 才升级。

        ★ 注意：这里的升级依据是「用户没有显式取消」，
          而不是「检测到人还在动」。
        """
        out: list[Announcement] = []
        for event_id, act in list(self._active.items()):
            if now_ms < act.deadline_ms:
                continue
            self._active.pop(event_id)
            self._resolved[event_id] = FALL_CONFIRMED
            out.extend(self._notify(act))
        return out

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def cancel(
        self,
        event_id: str,
        now_ms: int,
        method: str = "voice",
    ) -> Announcement | None:
        """用户显式取消。

        ★ 这个方法不访问网络 —— 断网时取消也必须生效。
          端侧本地就能判，服务端只是同步状态。
        """
        act = self._active.pop(event_id, None)
        if act is None:
            return None
        self._resolved[event_id] = FALL_CANCELLED

        elapsed_ms = now_ms - act.started_ms
        return Announcement(
            text="好的，已取消。如有需要请随时呼救。",
            ttl_ms=4000,
            dedup_key=f"fall:{event_id}:cancelled",
            source=SOURCE_EMERGENCY,
            priority=PRIORITY_CRITICAL,
            haptic="short",
            detail=emergency_detail(
                FALL_CANCELLED,
                event_id,
                contact_scope="none",
            )
            | {"cancel_method": method, "elapsed_ms": elapsed_ms},
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def active_events(self) -> list[str]:
        return list(self._active)

    def state_of(self, event_id: str) -> str | None:
        if event_id in self._active:
            return FALL_SUSPECTED
        return self._resolved.get(event_id)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _ask(
        self,
        event_id: str,
        window_ms: int,
        location: dict | None,
        priority: int,
    ) -> Announcement:
        seconds = max(1, window_ms // 1000)
        text = phrasing.fall_confirm_text(seconds)
        ttl, _ = phrasing.ensure_ttl(int(window_ms), text)

        return Announcement(
            text=text,
            ttl_ms=ttl,
            dedup_key=f"fall:{event_id}:ask",
            source=SOURCE_EMERGENCY,
            priority=priority,
            interrupt=True,
            haptic="long",
            detail=emergency_detail(
                FALL_SUSPECTED,
                event_id,
                confirm_deadline_ts=None,  # 由调用方按 now_ms 填
                contact_scope="family",
                location=location,
            )
            | {
                "countdown_ms": window_ms,
                # ★ 端侧本地自减渲染倒计时，不靠服务端 tick 驱动 ——
                #   断网时倒计时仍然要走完。
                "cancel_methods": ["voice", "screen_tap", "hardware_key", "shake"],
            },
        )

    def _notify(self, act: _Active) -> list[Announcement]:
        """确认跌倒 -> 按通知链外呼。

        idempotency_key 用 event_id，防止重试导致重复呼叫家属。
        """
        out: list[Announcement] = []

        for i, target in enumerate(ESCALATION_CHAIN):
            act.notified.append(target)
            scope = "family" if target == "family" else "grid_worker"
            text = (
                "已通知您的家人，请保持冷静，不要移动。"
                if target == "family"
                else "家人暂未应答，已同步通知社区网格员。"
            )
            out.append(
                Announcement(
                    text=text,
                    ttl_ms=10_000,
                    dedup_key=f"fall:{act.event_id}:notify:{target}",
                    source=SOURCE_EMERGENCY,
                    priority=PRIORITY_CRITICAL,
                    haptic="long",
                    detail=emergency_detail(
                        FALL_CONFIRMED,
                        act.event_id,
                        contact_scope=scope,
                        location=act.location,
                    )
                    | {
                        "escalation_step": i,
                        "notified": list(act.notified),
                        # ★ 不自动拨 120 —— 只提醒手动拨
                        "advise_manual_call": target == ESCALATION_CHAIN[-1],
                    },
                )
            )
        return out
