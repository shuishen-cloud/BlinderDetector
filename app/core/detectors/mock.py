"""Mock 检测器 —— 按帧序号返回预设的原始检测结果。

    DETECTOR=mock    （默认）

刻意返回**未分级、未措辞、未过滤**的原始结果，让 safety layer 真正
跑一遍规则（置信度门限、悲观距离分级、措辞生成、TTL 计算），
否则测试覆盖不到安全关键逻辑。
"""

from __future__ import annotations

from typing import Any

from app.contracts import Frame
from app.core.registry import register

#: 每个帧序号对应的原始检测结果。三轮循环，模拟视频画面变化。
#
#  0 -> 前方无障碍
#  1 -> 正前方有向下的台阶
#  2 -> 右前方来车 + 一条低置信度的误检（应当被门限滤掉）
DETECTION_SCENES: list[list[dict[str, Any]]] = [
    [],
    [
        {"type": "step_down", "position": "center",
         "distance_m": 2.0, "distance_sigma_m": 0.7,
         "confidence": 0.86, "closing_speed_mps": None, "track_id": 3},
    ],
    [
        {"type": "bicycle", "position": "right",
         "distance_m": 4.0, "distance_sigma_m": 1.6,
         "confidence": 0.79, "closing_speed_mps": 3.0, "track_id": 5},
        {"type": "vehicle", "position": "right",
         "distance_m": 7.0, "distance_sigma_m": 3.0,
         "confidence": 0.41, "closing_speed_mps": 6.0, "track_id": 6},
        {"type": "pole", "position": "left",
         "distance_m": 1.2, "distance_sigma_m": 0.5,
         "confidence": 0.88, "closing_speed_mps": None, "track_id": 7},
    ],
]


@register("detector", "mock")
class MockDetector:
    async def detect(self, frame: Frame) -> list[dict[str, Any]]:
        index = int(frame.extra.get("index", 0))
        return DETECTION_SCENES[index % len(DETECTION_SCENES)]

    async def health(self) -> bool:
        return True
