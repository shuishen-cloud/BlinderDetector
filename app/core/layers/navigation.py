"""
第三层：智能导航 —— 无障碍路线规划、语音交互、实时纠偏。

路线数据来自 `app/core/routers/`（靠 `.env` 的 `ROUTER` 切换：
`builtin` 内置假路网 / `baidu` 百度地图）。本层只做**编排**：
选 router → 拿到了就交给规则层加工 → 拿不到就降级并**如实通告**。

★ 和第一、二层的边界：
  · 导航指令的优先级是 IMPORTANT(2)，可以打断场景描述，
    但打断不了碰撞预警。
  · 本层的距离来自地图，**可信**，所以允许播报数字
    （「前方 300 米右转」）。第二层那条「障碍物不播数字」的规则
    只约束单目深度估计，不约束这里。

★★★★★ 降级必须是**出声**的 ★★★★★

  真实地图挂掉、或没拿到目的地坐标时，会改用**内置演示路网** ——
  而那条路网是**编的**。用户若不知道，会照着一条假路线走。
  所以降级时除了给路线，还要额外播一条说明（见 `rules/route.py`
  的 `route_degraded_announcement`）。这与 design.md 反复强调的
  「沉默不能有歧义」是同一条原则。
"""

from __future__ import annotations

import logging

from app import config
from app.contracts import SOURCE_NAVIGATION, Announcement, Frame
from app.core import registry
from app.core.registry import get, register
from app.core.rules import route as route_rules

_log = logging.getLogger(__name__)

#: 默认的无障碍偏好（定义在 rules/route.py，用户可以在 frame.extra 里覆盖）
DEFAULT_AVOID = route_rules.DEFAULT_AVOID

#: 没有定位时的兜底坐标（太原工业学院东区）。
#:
#: ★ 与调试台坐标输入框的默认值是**同一组**：这样「前端没传 geo」和
#:   「前端传了默认值」算出来的路线一致，不会让人误以为哪里出了问题。
#: ★ 但这**不是**真的定位 —— 系统目前没有定位能力（见 `工作管理.md` 待办）。
#:   真实地图下没收到 geo 时，图层会额外播一条「没有收到定位」的通告。
FALLBACK_ORIGIN = (37.957919, 112.543633)

#: 兜底路网的名字。降级时用它，也用它判断「是不是已经在兜底了」。
FALLBACK_ROUTER = "builtin"


@register("layer", "navigation")
class NavigationLayer:
    source = SOURCE_NAVIGATION

    def __init__(
        self,
        planner: str | None = None,
        walk_speed_mps: float | None = None,
    ) -> None:
        # ★ 默认值必须是 `None`，不能写死 "builtin"。
        #   下面用的是 `planner or config.ROUTER`，而 "builtin" 是 truthy ——
        #   一旦写成 `planner: str = "builtin"`，`ROUTER=baidu` 就**永远不生效**，
        #   而且不报任何错。这是个很安静的坑，所以在这里点明。
        #
        # ★ `or FALLBACK_ROUTER` 兜的是 `ROUTER=` 留空的情况（`.env.example`
        #   里 `BAIDU_AK=` / `BAIDU_FIXTURE=` 都是留空的，很容易照抄成空）。
        configured = planner or config.ROUTER or FALLBACK_ROUTER

        # ★ router 实例在这里取一次并缓存。`registry.get()` 无缓存、每次 new
        #   （见 registry.py），放进 `handle()` 就变成每个请求实例化一次；
        #   而真实 router 的构造可能带校验，代价不为零。
        self.fallback = get("router", FALLBACK_ROUTER)

        # ★ 配错名字（`ROUTER=amap` 但还没实现）不能把服务拖垮。
        #   `get()` 抛的 KeyError 发生在 `create_app()` 的图层构造期 ——
        #   整个 uvicorn 直接退出，连 `/v1/health` 都打不开。这与本项目
        #   「配置不对也要降级并如实播报」的立身之本相反。
        #   所以：降级到兜底 + 记日志 + **每次请求都播报降级原因**（不静默）。
        self.config_error: str | None = None
        try:
            self.router = get("router", configured)
            self.router_name = configured
        except KeyError:
            _log.warning(
                "ROUTER=%r 不是已注册的实现（已注册的有: %s），降级回 %s",
                configured, ", ".join(registry.names("router")), FALLBACK_ROUTER,
            )
            self.router = self.fallback
            self.router_name = FALLBACK_ROUTER
            # ★ 用**独立**的原因码，不能复用 `router_unavailable` ——
            #   那是给「地图服务真的挂了」用的措辞（「地图服务暂时不可用」）。
            #   配置拼错说成服务挂了，会把排查的人引向网络而不是 .env。
            self.config_error = "router_misconfigured"

        self.walk_speed_mps = walk_speed_mps

    async def handle(self, frame: Frame) -> list[Announcement]:
        extra = frame.extra or {}
        origin = _geo(extra.get("geo"))

        req = route_rules.RouteRequest(
            origin=origin or FALLBACK_ORIGIN,
            destination=extra.get("destination") or "目的地",
            avoid=_avoid_list(extra.get("avoid")),
            walk_speed_mps=self.walk_speed_mps,
            destination_geo=_geo(extra.get("destination_geo")),
        )

        raw, degrade_reason = await self._route(req)
        # route_id 里带上实际出路线的那一方：降级后的内置路线与原本的
        # 百度路线是两个不同的 id，去重键自然分开，用户能听到降级后的新路线。
        effective = FALLBACK_ROUTER if degrade_reason else self.router_name
        # ★ 坐标系要取自**实际出路线的那一方**，不能取 `self.router`：
        #   「配了 baidu 但降级回内置路网」时，内置路线没有坐标，
        #   而看 `self.router` 会让我们声称 "bd09ll" —— payload 在撒谎。
        # ★ 用 getattr 兜底：`coord_system` 按约定不进 Protocol，
        #   第三方 router 忘了写不该在请求路径上炸。
        router_used = self.fallback if degrade_reason else self.router
        detail = route_rules.build_detail(
            req, raw,
            router_name=effective,
            coord_system=getattr(router_used, "coord_system", None),
        )

        anns = route_rules.step_announcements(
            detail, max_steps=_positive_int(extra.get("max_steps"), default=1)
        )

        # ★ 通告一律 append 在**路线播报之后**：先给可用信息，再给免责说明。
        #   也让 announcements[0] 保持是导航步骤（端侧和测试都这么期待）。
        route_id = detail["route_id"]
        if degrade_reason:
            anns.append(route_rules.route_degraded_announcement(route_id, degrade_reason))
        if not raw:
            anns.append(route_rules.route_no_route_announcement(route_id))
        if origin is None and effective != FALLBACK_ROUTER:
            # 只有真实地图才在乎起点。内置路网根本不做起点，对它发这条是噪音。
            anns.append(route_rules.route_no_origin_announcement(route_id))
        return anns

    async def _route(self, req: route_rules.RouteRequest) -> tuple[list[dict], str | None]:
        """拿原始分段。返回 `(steps, 降级原因)`，原因为 None 表示没降级。"""
        if self.config_error:
            # ROUTER 配置写错了，已经在用兜底路网 —— 但**每次都要如实播报**，
            # 不能因为「反正是兜底」就静默，否则用户以为听到的是真实地图。
            raw = await self.router.plan(req.origin, req.destination_geo)
            return raw or [], self.config_error

        if self.router_name == FALLBACK_ROUTER:
            # 已经在兜底了，别再降级一次绕回来。
            raw = await self.router.plan(req.origin, req.destination_geo)
            return raw or [], None

        raw = await self.router.plan(req.origin, req.destination_geo)
        if raw is not None:
            return raw, None

        # 真实地图没给出路线。区分两种原因 —— 它们对用户是两句不同的话。
        reason = "no_destination_geo" if req.destination_geo is None else "router_unavailable"
        fallback_steps = await self.fallback.plan(req.origin, req.destination_geo)
        return fallback_steps or [], reason


def _positive_int(value, *, default: int) -> int:
    """把客户端传的 `max_steps` 收敛成合法正整数。

    ★ `extra` 是**任意 JSON**，不能信。传 "abc" 或 2.7 会让
      `list[:max_steps]` 抛 TypeError 冒成 500 —— 实测确实如此。
      值不合法就用默认值，一次播报的行为不该因为客户端手滑而崩掉整条链路。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _avoid_list(value) -> list[str]:
    """收敛 `avoid` 成字符串列表。

    ★ 传数字会让 `kind in avoid` 抛 TypeError（实测 500）。
    ★ `avoid: []` 是**合法意图**（「这次不避开任何东西」），要原样保留 ——
      所以只在「根本不是列表」时才回到默认值。
    """
    if not isinstance(value, list):
        return DEFAULT_AVOID
    return [v for v in value if isinstance(v, str)]


def _geo(value) -> tuple[float, float] | None:
    """从 `extra` 里取坐标。

    约定格式是 `{"lat": .., "lng": ..}`，坐标系 **WGS-84**（端侧 GPS 原生），
    换算成厂商坐标系由 router 自己负责 —— 见 `app/core/routers/baidu.py`。
    取不到或格式不对一律返回 None，绝不拿半个坐标去规划。
    """
    if isinstance(value, dict) and "lat" in value and "lng" in value:
        try:
            return (float(value["lat"]), float(value["lng"]))
        except (TypeError, ValueError):
            return None
    return None
