"""障碍物检测器接口。

实现只负责「看到什么」，**不负责风险分级和措辞** —— 那些在
`app/core/rules/risk.py` 和 `phrasing.py` 里，所有实现共用同一套规则。

这样换检测模型（YOLOv8 / 端侧 ONNX / 真实深度估计）时，安全策略不会变。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.contracts import Frame


@runtime_checkable
class ObstacleDetector(Protocol):
    async def detect(self, frame: Frame) -> list[dict[str, Any]]:
        """返回原始检测结果，每条：

            {"type": str,               # 见 contracts.OBSTACLE_TYPES
             "position": str,           # left | center | right
             "distance_m": float,
             "distance_sigma_m": float, # ★ 单目深度误差，必须带
             "confidence": float,
             "closing_speed_mps": float | None,  # 正在接近时的速度
             "track_id": int | None}

        ★ 实现不应在这里做置信度过滤或风险分级 —— 交给规则层，
          这样阈值可以集中调，也能被测试覆盖。
        """
        ...

    async def health(self) -> bool: ...
