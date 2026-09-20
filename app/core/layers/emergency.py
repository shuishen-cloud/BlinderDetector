"""第四层：紧急求助 —— 一键求助、跌倒检测、家属联动。

★ 安全关键：本期只出骨架，两条硬规则必须保留：

  1. `suspected` 态永不自动外呼。
     真跌倒和「把手机扔到床上」在加速度计上几乎无法区分。必须等
     confirm_deadline_ts 超时、用户没响应，才升到 confirmed。

  2. 取消不能依赖网络。
     用户说「我没事」时如果 WS 正好断了，取消仍然必须生效。
     所以端侧本地就能完成取消，服务端只是通知者。

触发方式的限制（写在这里免得白做）：任务书里的「长按手机侧键 3 秒」
在微信小程序和 Web 上都没有对应 API。如果最终载体是这两者，要改成
屏幕长按或语音触发。见 EmergencyKind 的取值。
"""

from __future__ import annotations

from dataclasses import replace

from app.contracts import FALL_SUSPECTED, SOURCE_EMERGENCY, Announcement, Frame
from app.core.registry import register
from app.mock import fixtures as F


@register("layer", "emergency")
class EmergencyLayer:
    source = SOURCE_EMERGENCY

    def __init__(self) -> None:
        self._active: dict[str, dict] = {}  # event_id -> 状态

    async def handle(self, frame: Frame) -> list[Announcement]:
        ann = replace(F.FALL_SUSPECTED_ANN, frame_id=frame.frame_id, ts=frame.ts)
        self._active[ann.detail["event_id"]] = {"state": FALL_SUSPECTED, "ts": frame.ts}
        return [ann]

    def cancel(self, event_id: str) -> bool:
        """用户显式取消 —— 端侧本地调用，不依赖网络。"""
        if event_id in self._active:
            self._active.pop(event_id)
            return True
        return False
