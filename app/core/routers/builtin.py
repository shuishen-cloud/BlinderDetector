"""内置假路网 —— 不依赖网络、零配置。

两条用途：
  1. `ROUTER` 的默认值，保证演示与离线开发不会被网络拖垮；
  2. 真实地图挂掉时的**降级兜底**。

★ 它的 `barriers` 是**作者手写的元数据**，表达「这一段会经过某个障碍」。
  所以基于它做的「已避开 X」是真的 —— 那一步确实被丢掉了。
  真实地图 API 不提供这种元数据，只能扫文本做警告，不能声称避开。

★ 但也要清楚：这条路网本身是**编的**。降级到它时必须如实告诉用户
  「这是演示路网，不是真实地图」—— 让用户按一条编的路线走，
  比听到「已避开天桥」严重得多。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.registry import register


@dataclass
class Barrier:
    """路线上的障碍类型。"""

    kind: str  # overpass | underpass | stairs | steep | no_elevator
    at_step: int = 0
    description: str = ""


#: 内置小路网。每条是 (instruction, maneuver, distance_m, barriers)
_FAKE_NETWORK: list[tuple[str, str, float, list[Barrier]]] = [
    ("沿人行道直行", "straight", 200.0, []),
    ("在天桥前右转，走地面斑马线", "right", 120.0,
     [Barrier("overpass", 0, "已避开天桥")]),
    ("继续直行通过路口", "straight", 260.0, []),
    ("左转进入目的地所在街道", "left", 180.0, []),
    ("目的地在您右侧", "arrive", 60.0, []),
]

#: 路线原始分段（未过滤）。`BuiltinRouter.plan()` 直接返回它。
#: ★ 提成模块常量是为了让测试能拿到**同一份**数据去驱动 `rules/route.py` ——
#:   测试里不许再手抄一份，否则两边迟早漂移。
RAW_STEPS: list[dict] = [
    {
        "instruction": instruction,
        "maneuver": maneuver,
        "distance_m": distance,
        "barriers": barriers,
        # ★ 内置路网**没有坐标**。这个空 `path` 是为了与其他 router 的
        #   分段形状保持一致（前端/规则层不用判断字段在不在）。
        "path": [],
    }
    for instruction, maneuver, distance, barriers in _FAKE_NETWORK
]


@register("router", "builtin")
class BuiltinRouter:
    """内置假路网。永远可用 —— 它是兜底，不该有失败路径。"""

    name = "builtin"
    #: ★ 内置演示路网**没有坐标** —— `_FAKE_NETWORK` 只有文字和距离。
    #: 前端据此显示「内置演示路网没有坐标，画不出路线」，
    #: 而不是画一条凭空连起来的线。
    coord_system = None

    async def plan(self, origin, destination) -> list[dict] | None:
        # 刻意不看 origin / destination：这条路网是固定的，假装它随起终点变化
        # 反而是种误导。真实感由 `rules/route.py` 的警告文本负责表达。
        #
        # ★ 返回**副本**，不能直接给 RAW_STEPS 本体：那是模块级常量，
        #   直接返回会让所有调用方共享同一个可变对象 —— 将来任何一处
        #   在原地上加工（补字段、改单位、按 avoid 删步骤）都会**静默改掉
        #   整个进程的兜底路线**，而兜底恰恰是最不该被污染的路径。
        return [
            {**step, "barriers": list(step.get("barriers") or [])}
            for step in RAW_STEPS
        ]

    async def health(self) -> bool:
        return True
