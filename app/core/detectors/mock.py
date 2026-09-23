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

#: 每个帧序号对应的原始检测结果。六轮循环，模拟视频画面变化（2026-09-23 由 3 轮扩到 6 轮）。
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
    # ---- 以下为 2026-09-23 追加 -------------------------------------------------
    # ★ 一律**追加在末尾**：`index % len(...)` 是轮换的基础，插在中间会让
    #   index=0（无障碍）/1（台阶）/2（自行车）的既有语义整体错位，
    #   而 tests/ 里多处按 index 取值断言（BODY 用 index=1 等）。
    #   追加则只把轮换周期从 3 拉长到 6，重复感随之下降。
    [
        # 3: 路缘石 —— 窄但低于脚踝，容易踩空
        {"type": "curb", "position": "center",
         "distance_m": 1.5, "distance_sigma_m": 0.4,
         "confidence": 0.82, "closing_speed_mps": None, "track_id": 11},
    ],
    [
        # 4: 两个低危目标 —— 考验「挑最紧急的一条说」而不是全念一遍
        {"type": "person", "position": "left",
         "distance_m": 2.5, "distance_sigma_m": 0.8,
         "confidence": 0.90, "closing_speed_mps": None, "track_id": 12},
        {"type": "pole", "position": "right",
         "distance_m": 3.0, "distance_sigma_m": 0.6,
         "confidence": 0.85, "closing_speed_mps": None, "track_id": 13},
    ],
    [
        # 5: 危险 + 次要同时出现 —— 一条要抢在另一条前面
        {"type": "step_up", "position": "center",
         "distance_m": 0.8, "distance_sigma_m": 0.3,
         "confidence": 0.90, "closing_speed_mps": None, "track_id": 14},
        {"type": "vehicle", "position": "right",
         "distance_m": 5.0, "distance_sigma_m": 2.0,
         "confidence": 0.83, "closing_speed_mps": 8.0, "track_id": 15},
    ],
]


@register("detector", "mock")
class MockDetector:
    async def detect(self, frame: Frame) -> list[dict[str, Any]]:
        index = int(frame.extra.get("index", 0))
        return DETECTION_SCENES[index % len(DETECTION_SCENES)]

    async def health(self) -> bool:
        return True
