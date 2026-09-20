"""
第三层：无障碍路线规划 —— 最小可跑。

本期不接高德/百度 API，用一个内置的小路网把数据流和状态机走通。
真实实现时把 `plan()` 换成地图 SDK 调用即可，下游（步骤播报、
无障碍过滤）不用动。

★ 和第一、二层的关键区别：**这一层的距离是可信的**，来自地图而非
  单目深度估计，所以**允许播报具体数字**（「前方 300 米右转」）。
  「障碍物不播数字」那条规则只约束第二层。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.contracts import (
    PRIORITY_IMPORTANT,
    SOURCE_NAVIGATION,
    Announcement,
    route_detail,
)
from app.core.rules import phrasing


@dataclass
class Barrier:
    """路线上的障碍类型。"""

    kind: str  # overpass | underpass | stairs | steep | no_elevator
    at_step: int = 0
    description: str = ""


@dataclass
class RouteRequest:
    origin: tuple[float, float]
    destination: str
    avoid: list[str] = field(default_factory=list)
    walk_speed_mps: float | None = None


#: 内置小路网。每条是 (instruction, maneuver, distance_m, barriers)
_FAKE_NETWORK: dict[str, list[tuple[str, str, float, list[Barrier]]]] = {
    "default": [
        ("沿人行道直行", "straight", 200.0, []),
        ("在天桥前右转，走地面斑马线", "right", 120.0,
         [Barrier("overpass", 0, "已避开天桥")]),
        ("继续直行通过路口", "straight", 260.0, []),
        ("左转进入目的地所在街道", "left", 180.0, []),
        ("目的地在您右侧", "arrive", 60.0, []),
    ],
}

#: 用户声明要避开的障碍 -> 该障碍的中文名
AVOID_LABELS = {
    "overpass": "天桥",
    "underpass": "地下通道",
    "stairs": "台阶",
    "steep": "陡坡",
    "no_elevator": "无电梯路段",
}

#: 默认的无障碍偏好。任务书要求过滤天桥、地下通道、无电梯路段。
DEFAULT_AVOID = ["overpass", "underpass", "stairs"]


def plan(req: RouteRequest) -> dict:
    """生成一条路线。返回 route_detail 的形状。"""
    raw = _FAKE_NETWORK["default"]

    steps: list[dict] = []
    warnings: list[str] = []
    total = 0.0

    for instruction, maneuver, distance, barriers in raw:
        blocked = [b for b in barriers if b.kind in req.avoid]
        if blocked:
            warnings.append(
                f"已避开{'、'.join(AVOID_LABELS.get(b.kind, b.kind) for b in blocked)}"
            )
            continue

        steps.append({
            "instruction": instruction,
            "maneuver": maneuver,
            "distance_m": distance,
        })
        total += distance

    return route_detail(
        route_id=f"r_{abs(hash(req.destination)) % 10**6:06d}",
        steps=steps,
        total_distance_m=total,
        total_duration_s=total / (req.walk_speed_mps or 1.1),
        warnings=warnings,
    )


def step_announcements(
    detail: dict,
    *,
    max_steps: int = 1,
) -> list[Announcement]:
    """把路线的前几步转成播报。

    一次只播 max_steps 步 —— 把五步一口气念完，用户一步都记不住。
    余下的步骤由端侧在到达后请求下一步。
    """
    out: list[Announcement] = []
    for i, step in enumerate(detail.get("steps", [])[:max_steps]):
        dist = step["distance_m"]
        # ★ 这里可以播数字：来自地图，误差量级和单目深度完全不同
        text = f"{step['instruction']}，约 {int(dist)} 米"
        ttl, _ = phrasing.ensure_ttl(15_000, text)

        out.append(
            Announcement(
                text=text,
                ttl_ms=ttl,
                dedup_key=f"nav:{detail['route_id']}:step:{i}",
                source=SOURCE_NAVIGATION,
                priority=PRIORITY_IMPORTANT,
                haptic="short" if step["maneuver"] != "straight" else "none",
                detail=detail | {"step_index": i},
            )
        )

    for w in detail.get("warnings", []):
        out.append(
            Announcement(
                text=w,
                ttl_ms=8000,
                dedup_key=f"nav:{detail['route_id']}:warn:{w}",
                source=SOURCE_NAVIGATION,
                priority=PRIORITY_IMPORTANT,
                detail=detail | {"warning": w},
            )
        )
    return out
