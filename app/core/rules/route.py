"""第三层：无障碍路线规划 —— **策略层**。

★ 这里**不再拿数据**。路线数据由 `app/core/routers/` 提供
  （内置假路网 / 百度地图，靠 `.env` 的 `ROUTER` 切换），本文件只回答
  「怎么过滤、怎么警告、怎么说出来」。换地图厂商时这里一行都不用改 ——
  与 `detectors/` 之于 `rules/risk.py` 是同一个分工。

★ 和第一、二层的关键区别：**这一层的距离是可信的**，来自地图而非
  单目深度估计，所以**允许播报具体数字**（「前方 300 米右转」）。
  「障碍物不播数字」那条规则只约束第二层。

★★★★★ 本层最重要的一条：**「已避开」和「请注意」是两句不同的话。** ★★★★★

  · 内置路网的 `barriers` 是**手写元数据**，说「这步会经过天桥」时我们
    确实把那一步丢掉了 —— 所以对它说「已避开天桥」是**真的**。
  · 真实地图 API **不提供任何障碍元数据**（百度没有「避开天桥」这个参数），
    我们唯一能做的是扫 `instruction` 文本**发现**它 —— 那只能警告，
    **绝不能声称「已避开」**。

  对视障用户，「已避开天桥」会让他放心往前走，而前面正是一座天桥。
  这与团队刚修掉的「**已通知**您的家人」是同一类缺陷：系统在没做到的时候
  说做到了。详见 `问题-待处理的一些遗留问题.md` §1.5。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

from app.contracts import (
    PRIORITY_IMPORTANT,
    SOURCE_NAVIGATION,
    SOURCE_SYSTEM,
    Announcement,
    route_dedup_key,
    route_detail,
    system_detail,
)
from app.core.rules import phrasing


@dataclass
class RouteRequest:
    origin: tuple[float, float]
    destination: str
    avoid: list[str] = field(default_factory=list)
    walk_speed_mps: float | None = None
    #: 目的地的坐标。★ 真实地图 API 只认坐标，不认地名 ——
    #: 「最近的地铁站」这种自然语言需要先地理编码（本轮不做）。
    #: 缺它时真实地图路线规划不出来，会如实降级。
    destination_geo: tuple[float, float] | None = None


#: 播报文本里「自带距离」的判据：**数字 + 单位**。
#: 只查裸字「米」会被地名（米市大街）命中，把距离整段吞掉。
_DISTANCE_RE = re.compile(r"\d\s*(?:米|公里|km|m\b)", re.IGNORECASE)

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

#: 文本扫描的**危险句式**。
#:
#: ★ 刻意只匹配句式、不匹配裸词，两个原因都有实测教训：
#:   · 「天桥」是北京地名（天桥南大街、天桥路口），裸词匹配必然误报 ——
#:     让用户去绕一个根本不存在的东西，比不报更伤信任（alarm fatigue）。
#:   · 「电梯」语义是**反的**：一条写着「乘坐无障碍电梯」的指令恰恰是好路，
#:     按 `no_elevator` 去扫会把好路说成坏路，在用户最需要电梯时把他吓走。
#:     所以 `no_elevator` **不参与文本扫描** —— 宁可漏报，不可误报。
_BARRIER_PHRASES: dict[str, tuple[str, ...]] = {
    "overpass": ("过天桥", "上天桥", "走天桥", "人行天桥", "天桥过街"),
    "underpass": ("地下通道", "走地下", "过地下"),
    # ★ 台阶/阶梯/楼梯 直接匹配裸词，因为它们**没有歧义** ——
    #   不像「天桥」（北京地名，裸词必误报）、「电梯」（语义会反转）。
    #   这条是实测教训：真实太原路线第 15 段写的是「过阶梯」，
    #   而最初的词表只有「上台阶/下台阶/走台阶/爬楼梯/走楼梯」，
    #   于是**一段真台阶被漏报了** —— 用户走过去而收不到任何提示。
    "stairs": ("台阶", "阶梯", "楼梯"),
    "steep": ("陡坡", "上坡路", "下坡路"),
}


def build_detail(
    req: RouteRequest,
    raw_steps: list[dict],
    *,
    router_name: str = "builtin",
    coord_system: str | None = None,
) -> dict:
    """把 router 给的原始分段，按无障碍策略加工成 `route_detail` 的形状。

    `raw_steps` 每段：`{"instruction", "distance_m", "maneuver", "path", "barriers"?}`。
    `path` 只用于拼路线级几何（可视化），不参与过滤/警告/措辞。
    """
    steps: list[dict] = []
    warnings: list[str] = []
    total = 0.0
    warned_kinds: set[str] = set()
    geometry: list[list[float]] = []

    for raw in raw_steps:
        instruction = raw.get("instruction") or ""

        # ★ 几何**最先收集**，且刻意放在下面所有 `continue` 之前：
        #   一段路可能因为「没有可播的文字」或「被无障碍过滤掉」而不进 steps，
        #   但它的路径点必须留下 —— 漏掉一段，地图上就会出现一条**凭空的连线**，
        #   那是在编一条没走过的路，比不画严重得多。
        geometry.extend(_clean_points(raw.get("path")))

        if not instruction:
            # 没有可播文字的段：不播报、不计入总距离。它的路径点上面已经留下了。
            # （这条规则原先在 `routers/baidu.py` 的解析里，移到这里是因为
            #   「播什么」是策略层的决定，解析层只管把数据原样搬过来。）
            continue

        barriers = raw.get("barriers")

        # ---- 路径 1：作者手写了障碍元数据（只有内置路网会走这里）----
        # 这一步确实被丢掉了，所以「已避开」是真的。
        blocked = [b for b in (barriers or []) if _barrier_kind(b) in req.avoid]
        if blocked:
            labels = "、".join(
                AVOID_LABELS.get(_barrier_kind(b), _barrier_kind(b)) for b in blocked
            )
            warnings.append(f"已避开{labels}")
            continue

        # ---- 路径 2：没有元数据，只能扫文本（真实地图走这里）----
        # ★ 只扫 `barriers is None` 的步骤：内置路网每一步都带 barriers
        #   （哪怕空列表），所以它的行为 100% 不变；真实地图的步骤没有这个
        #   字段，才是扫描目标。按 kind 去重 —— 同一种障碍在一串步骤里被提到
        #   多次，用户只需要听到一次。
        if barriers is None:
            for kind in _detect_barriers(instruction):
                if kind in req.avoid and kind not in warned_kinds:
                    warned_kinds.add(kind)
                    warnings.append(f"前方有{AVOID_LABELS.get(kind, kind)}，请注意")

        distance = float(raw.get("distance_m") or 0.0)
        steps.append({
            "instruction": instruction,
            "maneuver": raw.get("maneuver") or "straight",
            "distance_m": distance,
        })
        total += distance

    # 抽稀 + 量化只影响画图，播报文本与米数一律不受影响。
    geometry = _simplify(_drop_repeated(geometry)) if geometry else []

    return route_detail(
        route_id=_route_id(req, router_name),
        steps=steps,
        total_distance_m=total,
        total_duration_s=total / (req.walk_speed_mps or 1.1),
        warnings=warnings,
        geometry=geometry,
        # ★ 只在真有几何时才声称坐标系。没有几何却报 "bd09ll" 是在撒谎 ——
        #   前端会照着这个值去解析一堆不存在的东西。
        coord_system=coord_system if geometry else None,
    )


def _clean_points(raw) -> list[list[float]]:
    """只留下能当坐标用的点，形状不对的一律丢掉。

    ★ 为什么必须清理而不是直接 `extend`：`path` 只用于可视化，一个坏点
      不该让**整条播报**挂掉。而第三方按 README 加一个 `router` 实现时，
      很自然会把厂商原始的 path 字符串（`"112.5,37.9;..."`）原样塞进来 ——
      那会在下面 `round()` 上抛 TypeError，冒成 500。
      「画不出图」是能接受的降级，「播报不出来」不是。
    """
    if not isinstance(raw, list):
        return []
    out: list[list[float]] = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        try:
            out.append([float(p[0]), float(p[1])])
        except (TypeError, ValueError):
            continue
    return out


def _drop_repeated(points: list[list[float]]) -> list[list[float]]:
    """去掉相邻段接缝处的重复点。

    实测百度相邻段的末点与下一段的首点是**逐字符相同**的（同一串数字解析出的
    浮点数也必然相等）。不去掉就会出现零长度线段，有些渲染器会画出尖刺。
    """
    out: list[list[float]] = []
    for p in points:
        if out and out[-1] == p:
            continue
        out.append(p)
    return out


#: 抽稀的垂距阈值（米）。见 `_simplify` 的说明。
_SIMPLIFY_TOLERANCE_M = 8.0
#: 坐标量化到几位小数。5 位 ≈ 1.1 米 —— 画图绰绰有余，还省掉四成体积
#: （百度原始返回是 11 位小数，每点约 34 字符）。
_COORD_PRECISION = 5


def _simplify(
    points: list[list[float]], tolerance_m: float = _SIMPLIFY_TOLERANCE_M
) -> list[list[float]]:
    """Douglas-Peucker 抽稀 + 量化。

    ★ **为什么可以抽稀**：调试台的地图卡片宽约 400–500 px，装 12.5 公里路线
      约合 25–30 米/像素；而实测 537 个点的平均间距才 23 米 ——
      **已经是一个点压在一个像素以内**。抽到 8 米阈值，屏幕上几乎逐像素相同。

    ★ **为什么不用均匀抽稀**：按固定间隔丢点会切掉转弯的拐角，路线看起来
      就"抄近道"了 —— 而那正是要看清的地方。垂距阈值法保住的恰恰是拐点。

    ★ 这只影响**画图**。播报文本、`total_distance_m`、所有米数都来自
      `steps`，与这里无关。契约里也写明了 `geometry` 不得用于任何决策。
    """
    if len(points) < 3:
        return _quantize(points)

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        dmax, idx = 0.0, -1
        for k in range(i + 1, j):
            d = _perp_distance_m(points[k], points[i], points[j])
            if d > dmax:
                dmax, idx = d, k
        if idx >= 0 and dmax > tolerance_m:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))

    return _quantize([p for p, k in zip(points, keep) if k])


def _quantize(points: list[list[float]]) -> list[list[float]]:
    return [[round(p[0], _COORD_PRECISION), round(p[1], _COORD_PRECISION)]
            for p in points]


def _perp_distance_m(p, a, b) -> float:
    """点 p 到线段 ab 的垂距（米）。

    用等距圆柱近似（按纬度缩放经度）—— 在这个尺度上够准，而且不需要引
    任何投影库。不缩经度的话南北向的路线会被算斜，抽稀就会抽错地方。
    """
    lat0 = math.radians((a[1] + b[1]) / 2)
    kx = 111320.0 * math.cos(lat0)
    ky = 111320.0
    px, py = (p[0] - a[0]) * kx, (p[1] - a[1]) * ky
    bx, by = (b[0] - a[0]) * kx, (b[1] - a[1]) * ky
    seg2 = bx * bx + by * by
    if seg2 == 0:
        return math.hypot(px, py)
    t = max(0.0, min(1.0, (px * bx + py * by) / seg2))
    return math.hypot(px - t * bx, py - t * by)


def _barrier_kind(barrier) -> str:
    """兼容 dataclass 与 dict 两种写法的障碍元数据。"""
    if isinstance(barrier, dict):
        return barrier.get("kind", "")
    return getattr(barrier, "kind", "")


def _detect_barriers(instruction: str) -> list[str]:
    """扫文本找出危险句式。命中才返回，认不出就是不报。"""
    return [
        kind
        for kind, phrases in _BARRIER_PHRASES.items()
        if any(p in instruction for p in phrases)
    ]


def _route_id(req: RouteRequest, router_name: str) -> str:
    """确定性的路线标识。

    ★ 不能用内置的 `hash()`：Python 对字符串的 hash **每个进程随机化**
      （PYTHONHASHSEED），重启后同一条路线会拿到不同的 id，日志和复现全对不上。

    ★ 把 `router_name` 与 `origin` 一起编进去：
      · 带上 router 名字，降级后的内置路线与原本的百度路线是**两个不同的 id**，
        去重键自然分开 —— 用户能听到降级后的新路线，而不是被当成重复吞掉；
      · 带上 origin，同一句「人民医院」从天安门出发和从回龙观出发不再撞成同一条。
    """
    origin = f"{req.origin[0]:.5f},{req.origin[1]:.5f}"
    seed = f"{router_name}|{origin}|{req.destination}".encode("utf-8")
    return "r_" + hashlib.blake2s(seed, digest_size=4).hexdigest()


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
    route_id = detail["route_id"]

    for i, step in enumerate(detail.get("steps", [])[:max_steps]):
        dist = step["distance_m"]
        # ★ 这里可以播数字：来自地图，误差量级和单目深度完全不同
        #
        # ★ 但别重复播。真实地图的 instruction 通常**自带距离**
        #   （实测百度：「向正南方向出发,走30米,过马路左转进入新兰路」），
        #   再补一句「约 30 米」只会让用户多听一遍同样的信息。
        #   内置路网的指令不含「米」，所以还是走追加那条路 —— 行为不变。
        # ★ 距离单位**两种**都要认：实测百度短段写「走770米」，长段写
        #   「走2.1公里」—— 只查「米」的话，长段会变成
        #   「走2.1公里,直行进入解放北路辅路，约 2095 米」，同一件事说两遍
        #   还换了单位。
        #
        # ★ 但必须匹配**「数字 + 单位」**，不能只查裸字「米」：
        #   「沿米市大街向北步行」这种地名会让裸字命中，于是距离整段丢掉，
        #   用户在最需要距离的导航步骤上反而一个数字都听不到。
        instruction = step["instruction"]
        has_distance = bool(_DISTANCE_RE.search(instruction))
        text = instruction if has_distance else f"{instruction}，约 {int(dist)} 米"
        ttl, _ = phrasing.ensure_ttl(15_000, text)

        # ★ 几何只挂在**第一条**播报上。
        #   一次导航会产生 N 条分步播报 + 若干警告，每条 detail 都带整条折线的话，
        #   `Envelope.publish` 要把它序列化 N 次，`Hub.broadcast` 还要对每个连接
        #   再发一次 —— 12 KB × N × 客户端数。地图拿到一次就够了。
        #   代价是形状不齐，所以在这里和契约里都写明。
        payload = detail if i == 0 else {
            k: v for k, v in detail.items() if k != "geometry"
        }

        out.append(
            Announcement(
                text=text,
                ttl_ms=ttl,
                dedup_key=route_dedup_key(route_id, "step", i),
                source=SOURCE_NAVIGATION,
                priority=PRIORITY_IMPORTANT,
                haptic="short" if step["maneuver"] != "straight" else "none",
                detail=payload | {"step_index": i},
            )
        )

    # ★ 警告同样剥掉几何 —— 见上面「几何只挂在第一条」的说明。
    #   地图已经从第一条播报拿到了折线，警告再各带一份只是重复传输。
    bare = {k: v for k, v in detail.items() if k != "geometry"}

    for w in detail.get("warnings", []):
        out.append(
            Announcement(
                text=w,
                ttl_ms=8000,
                dedup_key=route_dedup_key(route_id, "warn", w),
                source=SOURCE_NAVIGATION,
                priority=PRIORITY_IMPORTANT,
                detail=bare | {"warning": w},
            )
        )
    return out


# --------------------------------------------------------------------------
# 系统通告 —— 「沉默不能有歧义」
# --------------------------------------------------------------------------


def _notice(route_id: str, kind: str, text: str, ttl_ms: int = 10_000) -> Announcement:
    ttl, _ = phrasing.ensure_ttl(ttl_ms, text)
    return Announcement(
        text=text,
        ttl_ms=ttl,
        dedup_key=route_dedup_key(route_id, "notice", kind),
        source=SOURCE_SYSTEM,
        priority=PRIORITY_IMPORTANT,
        detail=system_detail(kind),
    )


#: 降级原因 -> 如实措辞。措辞必须说清「这是演示路网」—— 内置路网是**编的**。
#: 让用户按一条编的路线走，比听到「已避开天桥」严重得多。
_DEGRADE_REASONS = {
    "router_unavailable": "地图服务暂时不可用，以下路线来自内置演示路网",
    "no_destination_geo": "没有收到目的地坐标，以下路线来自内置演示路网",
    # ★ 配置写错与「服务挂了」是两回事，措辞必须分开 ——
    #   同一句话会把排查的人引向网络，而问题其实在 .env。
    "router_misconfigured": "路线规划配置有误，以下路线来自内置演示路网",
}


def route_degraded_announcement(
    route_id: str, reason: str = "router_unavailable"
) -> Announcement:
    """已降级到内置路网。`reason` 决定措辞 —— 别把不同的失败原因说成同一句。"""
    text = _DEGRADE_REASONS.get(reason, _DEGRADE_REASONS["router_unavailable"])
    return _notice(route_id, f"degraded_{reason}", text)


def route_no_route_announcement(route_id: str) -> Announcement:
    """服务正常，但确实规划不出路线。与「服务不可用」是两件事，别混说。"""
    return _notice(route_id, "no_route", "未能规划到步行路线")


def route_no_origin_announcement(route_id: str) -> Announcement:
    """没收到定位，起点用了默认位置。

    ★ 本地图之前，起点兜底是无害的（内置路网根本不看坐标）。接上真实地图后，
      它会静默地规划出一条**错的**路线 —— 这是比「已避开天桥」更重的诚实性问题：
      不是声称避开了什么，而是自信地播报一条从错误起点算出来的路线。
    """
    return _notice(route_id, "no_origin", "没有收到定位，本次路线从默认位置开始")
