"""
第二层：安全预警 —— 障碍物检测、深度估计、跌倒检测。

分工：检测器只负责「看到什么」（`app/core/detectors/`），本层负责
「怎么判断危险、怎么说出来」（`app/core/rules/`）。这样换检测模型时
安全策略不会跟着变。

★ 这一层是热路径，延迟预算 <200ms。云端 VLM 单次调用 1–3 秒，
  所以本层**绝不能走 VLM**。这也是为什么第一层和第二层是两条
  独立流水线。
"""

from __future__ import annotations

from app.contracts import (
    PRIORITY_CRITICAL,
    PRIORITY_IMPORTANT,
    RISK_DANGER,
    RISK_WARNING,
    SOURCE_SAFETY,
    SOURCE_SYSTEM,
    Announcement,
    Frame,
    make_dedup_key,
    safety_detail,
    system_detail,
)
from app.core.registry import get, register
from app.core.rules import phrasing
from app.core.rules import risk as risk_rules

_RISK_PRIORITY = {
    RISK_DANGER: PRIORITY_CRITICAL,
    RISK_WARNING: PRIORITY_IMPORTANT,
}
_RISK_RANK = {RISK_DANGER: 2, RISK_WARNING: 1}


@register("layer", "safety")
class SafetyLayer:
    source = SOURCE_SAFETY

    def __init__(
        self,
        detector: str | None = None,
        walk_speed_mps: float | None = None,
    ) -> None:
        from app import config

        self.detector = get("detector", detector or config.DETECTOR)
        # 步行速度影响 TTL：走得慢的用户，同一个障碍物的有效窗口更长
        self.walk_speed_mps = walk_speed_mps

    async def handle(self, frame: Frame) -> list[Announcement]:
        """检测 → 分级 → 措辞。

        ★ 检测器抛异常时**不能**当成「前方没有障碍」—— 见下面 except 里的说明。
        """
        try:
            raw = await self.detector.detect(frame)
        except Exception as e:
            # ★ 检测器哑了 ≠ 前方没有障碍。
            #
            #   返回空列表会让「这一拍没看见东西」和「这一拍根本没在看」长得
            #   一模一样 —— 而用户听不出区别，会把沉默当成安全。所以这里必须
            #   **说出声**：转成一条系统降级播报（`source=system`，前端按「系统」
            #   渲染并计入意外统计）。
            #
            #   异常类型和信息一并带进 detail：吞掉它们等于把排查线索也吞了。
            #   重复的降级由闸门按 dedup_key 去重，不会每拍刷一次。
            return [self._degraded(e, frame)]

        obstacles = self._grade(raw)
        if not obstacles:
            # 前方无障碍 —— 不出声是对的，但「没出声」必须能和
            # 「系统哑了」区分开，那由 /v1/health 和降级通告负责
            return []
        return [self._announce(obstacles, frame)]

    def _degraded(self, err: Exception, frame: Frame) -> Announcement:
        """把「这一拍没在看」说出来。

        ★ 文本只说结论和该怎么做，不播异常原文 —— 听的人要的是「现在我该怎么办」。
          原文进 `detail`，给看日志的人。
        """
        reason = f"detector_error:{type(err).__name__}"
        return Announcement(
            text="安全预警暂时不可用，请放慢脚步",
            ttl_ms=10_000,
            dedup_key=f"system:degraded:{reason}",
            source=SOURCE_SYSTEM,
            priority=PRIORITY_IMPORTANT,
            frame_id=frame.frame_id,
            ts=frame.ts,
            detail=system_detail(reason, message=str(err)[:200]),
        )

    # ------------------------------------------------------------------

    def _grade(self, raw: list[dict]) -> list[dict]:
        """给原始检测结果定风险等级。置信度不够的直接丢掉。

        ★ 丢掉比播出去好：每次误报都在消耗用户对系统的信任，
          而信任是消耗品 —— 消耗光了，真的那次也会被忽略。
        """
        graded: list[dict] = []
        for d in raw:
            risk = risk_rules.classify(
                d.get("type", "other"),
                d.get("distance_m", 0.0),
                d.get("distance_sigma_m"),
                d.get("confidence", 0.0),
                d.get("closing_speed_mps"),
            )
            if risk is None:
                continue
            graded.append({**d, "risk": risk})
        return graded

    def _announce(self, obstacles: list[dict], frame: Frame) -> Announcement:
        top = max(
            obstacles,
            key=lambda o: (_RISK_RANK.get(o["risk"], 0), -o["distance_m"]),
        )
        nearest = min(o["distance_m"] for o in obstacles)

        # 用户走到最近障碍物所需的时间 —— 决定了这条预警能有多长
        window_ms = risk_rules.ttl_for(nearest, self.walk_speed_mps)

        # ★ 在窗口内挑一条播得完的文本。2 米外的台阶只有 1.8 秒，
        #   而完整句子要 3.7 秒 —— 物理上说不完，必须用最短形式。
        text, complete = phrasing.fit_text(obstacles, window_ms)

        # TTL 不小于播报耗时，否则会「播到一半过期」
        ttl_ms = max(window_ms, phrasing.speech_est_ms(text))

        return Announcement(
            text=text,
            ttl_ms=ttl_ms,
            # ★ 去重键含 risk 和距离档位，任一维度升级就突破去重窗口。
            #   否则 danger 会被当成 info 那条的「重复」而被吞掉。
            dedup_key=make_dedup_key(
                "obstacle",
                top["type"],
                top.get("position", "center"),
                top["risk"],
                top["distance_m"],
            ),
            source=SOURCE_SAFETY,
            priority=_RISK_PRIORITY.get(top["risk"], PRIORITY_IMPORTANT),
            interrupt=top["risk"] == RISK_DANGER,
            haptic=phrasing.haptic_for(top["risk"]),
            frame_id=frame.frame_id,
            ts=frame.ts,
            detail=safety_detail(
                top["risk"],
                [
                    {
                        "type": o["type"],
                        "position": o.get("position", "center"),
                        # ★ 单目深度误差必须如实带出，不能只给一个精确数字
                        "distance_m": o["distance_m"],
                        "distance_sigma_m": o.get("distance_sigma_m"),
                        "risk": o["risk"],
                        "confidence": o["confidence"],
                        "track_id": o.get("track_id"),
                    }
                    for o in obstacles
                ],
            )
            | {
                "window_ms": window_ms,
                "text_is_terse": not complete,
                "suppressed_count": 0,
            },
        )
