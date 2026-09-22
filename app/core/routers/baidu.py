"""百度地图步行路线规划（轻量级路线规划 DirectionLite）。

    https://api.map.baidu.com/directionlite/v1/walking

## 三个必须知道的坑

**1. 响应字段是 `instructions`（复数）**，不是 `instruction`。而且 step 明细
需要显式传 `steps_info=1` 才会下发 —— 不传的话可能根本没有 `steps`，
我们会把它误判成「没有路线」，等于**对用户说假话**（真实原因是我们参数没传对）。

**2. 坐标系**：百度默认收 BD-09，而端侧 GPS 出来的是 WGS-84，直接用会偏
几百米 —— 对视障导航是灾难。所以显式传 `coord_type=wgs84` 让百度换算，
本项目不做坐标转换。★ 这个参数对步行是否生效**需拿真 AK 实测一次**。

**3. 百度没有「避开天桥 / 台阶 / 地下通道」的参数。** 它只给最快步行路线，
拿不到任何障碍元数据。所以「无障碍」只能靠 `rules/route.py` 扫文本做
**警告**，绝不能声称「已避开」。看 `rules/route.py` 里怎么说这件事。

## AK 的坑

AK 需要实名认证，且要在控制台为它勾选「**步行路线规划（轻量）**」权限 ——
2018-11-14 之前注册的 AK 默认没有这个权限，会返回错误码。

★ 没配 AK 时**不要在 `__init__` 里抛异常**：`registry.get()` 是在图层构造期
  调用的，抛出去会直接变成 500，降级路径根本来不及触发。把「没 AK」
  表达成 `plan()` 返回 `None` 才对。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx

from app.core.registry import register

_log = logging.getLogger(__name__)

_ENDPOINT = "https://api.map.baidu.com/directionlite/v1/walking"

#: 百度会往 instruction 里塞 HTML 高亮标签，见 `_clean()`。
_TAG_RE = re.compile(r"<[^>]+>")

#: 转向词组。★ 刻意**不用**裸字「左/右」，也不用「左侧/右侧」——
#: 前者会被地名命中（左家庄东街），后者是位置描述不是动作
#: （「目的地在您的右侧」说的是终点在哪，不是让你右转）。
_LEFT_WORDS = ("左转", "左拐", "左前方", "向左")
_RIGHT_WORDS = ("右转", "右拐", "右前方", "向右")


def _clean(text: str) -> str:
    """去掉百度塞在 `instruction` 里的 HTML 标签。

    ★ 实测真实响应长这样（2026-09-21，太原）：

        "向正南方向出发,走30米,<b>过马路左转</b>进入<b>新兰路</b>"

    契约里 `text` 是「端侧唯一消费字段、只播 text」，绝不能带标记 ——
    不剥掉就会把尖括号原样播给视障用户，或让 TTS 念出一串乱码。
    百度文档里没有提到这件事，是打真实接口才发现的。
    """
    return _TAG_RE.sub("", text).strip()

#: 最近一次真实调用的失败原因；`None` = 上次成功（或还没调过）。
#:
#: ★ 用**模块级**变量而不是实例属性：`registry.get()` 无缓存，
#:   `/v1/health` 每次都新建一个实例，实例属性根本传不过去。
#:
#: 它解决的是「配置齐全 ≠ 服务可用」：AK 填了、格式也对，但服务可能被禁用
#: （实测 status=240）。只在 AK 上做检查的话，`/v1/health` 会在系统实际
#: 规划不出路线时依然报 ok —— 而「没出声」和「系统哑了」分不开正是本项目
#: 反复强调要避免的失效模式。
_last_failure: str | None = None


def _maneuver_of(turn_type: str | None, instruction: str = "") -> str:
    """百度的 `turn_type`（中文）-> 契约的 maneuver。

    ★ 认不出来一律当 `straight`，绝不能因为转向词没见过就丢掉整条路线。
      默认值也**必须**是 straight —— `step_announcements()` 靠
      `maneuver != "straight"` 决定要不要震动，若默认成别的值，
      一条 20 步的步行路线会震 20 次，震动信号直接退化成噪音。

    ★ `turn_type` 可能是**空字符串**（实测：一段「过马路右转进入恒山路」
      的 `turn_type` 是空串，`turn_type_id` 却有值）。只看这个字段会把
      转弯判成直行 —— 用户就收不到那次震动提示了。所以空了就退回扫
      instruction，那句话里通常明写着「过马路右转」。
    """
    t = (turn_type or "").strip() or instruction
    # ★ 先判转向、再判「到达」，而且只认**转向词组**、不认裸字「左/右」：
    #   · 「走10米,过马路右转进入**左**家庄东街」—— 裸字「左」会先命中，
    #     把这次右转判成左转；
    #   · 「沿道路直行到达官厅路口右转」—— 裸字「到达」会先命中，
    #     把中途转弯判成终点。
    #   转向优先于到达，是因为一句话里出现「右转」时，那才是这一步要做的事。
    if any(w in t for w in _LEFT_WORDS):
        return "left"
    if any(w in t for w in _RIGHT_WORDS):
        return "right"
    if "到达" in t:
        return "arrive"
    return "straight"


def parse_walking_response(payload: dict) -> list[dict] | None:
    """把百度响应转成原始分段。`None` = 这个响应不可用。

    ★ 每一层都 `.get()` 兜底：百度出错时返回的是形状完全不同的 JSON
      （`{"status": 2, "message": "..."}`），直接下标会抛异常。
    ★ 抽成**纯同步函数**是为了让解析逻辑能被同步测试覆盖 —— 项目里
      没有任何 HTTP 桩先例，也不打算为它引入新依赖。
    """
    if not isinstance(payload, dict):
        return None
    # status 正常是整数 0，但容错字符串 "0"（不同端点行为不一致）
    if str(payload.get("status")) != "0":
        return None

    result = payload.get("result")
    if not isinstance(result, dict):
        return None

    routes = result.get("routes")
    if not isinstance(routes, list):
        return None
    if not routes:            # 服务正常，但确实规划不出路线
        return []

    # ★ 只校验了 `routes` 是 list 还不够 —— 元素本身也得是 dict。
    #   少了这一行，`{"routes": ["oops"]}` 会在下面 `.get()` 抛
    #   AttributeError，一路冒成 500 —— 正是这套防御性解析要避免的失效模式。
    first = routes[0]
    if not isinstance(first, dict):
        return None

    steps = first.get("steps")
    if not isinstance(steps, list):
        return None

    out: list[dict] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        # ★ 实测真实响应用的是 `instruction`（**单数**）—— 反倒是百度文档
        #   和绝大多数博客写的是复数 `instructions`。**文档是错的。**
        #   两种都认，但单数放在前面：有实测样本的那个才是主。
        text = _clean(step.get("instruction") or step.get("instructions") or "")
        # ★ 这里**不再**跳过没有文字的段（原来会 `continue`）：
        #   几何要独立于文字的取舍。漏掉一段的路径点，图上就会出现一条
        #   凭空的连线 —— 那是**编出来的路**，比不画严重。
        #   「没有文字的段不播报」这条规则移到 `rules/route.py::build_detail`
        #   （那里才是决定播什么的地方）。
        out.append({
            "instruction": text,
            "distance_m": _to_float(step.get("distance")),
            "maneuver": _maneuver_of(step.get("turn_type"), text),
            "path": _parse_path(step.get("path")),
        })
    return out


def _parse_path(raw) -> list[list[float]]:
    """把百度的 `path` 字符串转成 `[[经度, 纬度], ...]`。

    实测格式：`"112.55599982546,37.964644419508;112.55594002424,..."`
    —— **经度在前**，11 位小数（后面 `rules/route.py` 会量化到 5 位）。

    ★ 任何异常一律返回 `[]`，绝不让它把整条路线变成 `None` ——
      几何画不出来只是少一张图，路线本身还得照常播报。
    """
    if not isinstance(raw, str) or not raw:
        return []
    out: list[list[float]] = []
    for pair in raw.split(";"):
        parts = pair.split(",")
        if len(parts) != 2:
            continue
        try:
            out.append([float(parts[0]), float(parts[1])])
        except ValueError:
            continue
    return out


def _to_float(value) -> float:
    """把距离转成 float，转不动就当 0.0。

    ★ 不能直接 `float(...)`：拿到非数字（字符串、None、嵌套对象）会抛
      ValueError 冒成 500。距离取不到只是播报少个数字，不该让整条链路崩。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@register("router", "baidu")
class BaiduRouter:
    """百度步行路线规划。失败一律返回 None，由图层负责降级。"""

    name = "baidu"
    #: 返回的几何用的坐标系。★ 与上面 `ret_coordtype` 是同一件事的两处表述，
    #:  改动必须成对 —— 见 `base.py` 的说明（它**不进 Protocol**，用 getattr 读）。
    coord_system = "bd09ll"

    def __init__(
        self,
        ak: str | None = None,
        timeout_ms: int | None = None,
        fixture: str | None = None,
    ) -> None:
        from app import config

        self.ak = ak if ak is not None else config.BAIDU_AK
        self.timeout_ms = timeout_ms or config.BAIDU_TIMEOUT_MS
        # ★ 开发开关：配了就从本地 JSON 读响应代替 HTTP。
        #   没有 AK 也能把「真实响应 → 解析 → 警告 → 降级」整条链跑通，
        #   答辩现场不至于只能讲代码。
        self.fixture = fixture if fixture is not None else config.BAIDU_FIXTURE
        #: fixture 解析结果缓存（内容固定，别每次请求都读盘 + 阻塞事件循环）
        self._fixture_cache: dict | None = None

    async def plan(self, origin, destination) -> list[dict] | None:
        if destination is None:
            # 百度只认坐标、不认地名（「最近的地铁站」得先地理编码）。
            # 没有坐标就返回 None 让图层如实降级 —— 绝不瞎猜一个目的地，
            # 那等于对着视障用户播报一条根本不对的路线。
            return None
        payload = await self._fetch(origin, destination)
        if payload is None:
            return None
        # ★ 先挡住「根本不是 JSON 对象」的响应，再 `payload.get`。
        #   `parse_walking_response` 里有一模一样的检查，但它排在这一行**之后** ——
        #   少了这一行，一个 JSON 数组（比如 BAIDU_FIXTURE 指向的文件存成了
        #   `[1,2,3]`）会在 `.get` 上抛 AttributeError 冒成 500，
        #   而本模块的承诺是「任何形状的失败都不许变成 500」。
        if not isinstance(payload, dict):
            _log.warning("百度返回的不是 JSON 对象（%s），按服务不可用处理",
                         type(payload).__name__)
            return None

        global _last_failure

        status = str(payload.get("status"))
        if status != "0":
            # ★ 必须留这一行：百度把「AK 没开这项服务」「AK 类型不对」
            #   「配额用尽」统统表达成非 0 的 status，而我们对用户只能
            #   说一句「地图服务暂时不可用」。不把真实原因记到服务端日志，
            #   排查的人会完全摸不着头脑。
            #   实测最常踩的是 status=240「APP 服务被禁用」——
            #   没在控制台为 AK 勾选「步行路线规划（轻量）」。
            _last_failure = f"{status} {payload.get('message') or ''}".strip()
            _log.warning("百度路线规划返回错误: status=%s message=%s",
                         status, payload.get("message"))
        else:
            _last_failure = None
        return parse_walking_response(payload)

    async def _fetch(self, origin, destination) -> dict | None:
        if self.fixture:
            return self._read_fixture()
        # 没配 AK 直接当服务不可用 —— 不发无谓的请求，也绝不抛异常
        if not self.ak:
            return None
        params = {
            "origin": _coord(origin),
            "destination": _coord(destination),
            "ak": self.ak,
            "coord_type": "wgs84",   # 端侧 GPS 原生就是 WGS-84（输入坐标系）
            "steps_info": "1",       # ★ 不传可能没有 steps，见模块 docstring
            # ★ 显式写死返回坐标系，不靠百度默认值：
            #   前端地图（百度 JSAPI GL）要的就是 BD-09，而默认值哪天变了
            #   我们不会知道 —— 那份 `BAIDU_FIXTURE` 样本也没记录它是在哪个
            #   ret_coordtype 下抓的。写死之后 `coord_system="bd09ll"` 才是
            #   我们自己控制的事实，不是一个碰巧成立的假设。
            "ret_coordtype": "bd09ll",
        }
        global _last_failure
        try:
            async with httpx.AsyncClient(timeout=self.timeout_ms / 1000) as c:
                r = await c.get(_ENDPOINT, params=params)
                r.raise_for_status()
                return r.json()
        except (httpx.HTTPError, ValueError) as e:
            # 超时 / 连不上 / 非 2xx / 响应不是 JSON —— 统统当服务不可用。
            # 真实服务一定会失败，这里不能让它冒成 500。
            _last_failure = f"{type(e).__name__}: {e}"
            _log.warning("百度路线规划请求失败: %s: %s", type(e).__name__, e)
            return None

    def _read_fixture(self) -> dict | None:
        """读一次并缓存。

        ★ 这是 `async` 路径里的**同步文件 I/O** —— 每次请求都读盘会让整个
          事件循环在读盘期间停摆，WS 播报和 `/v1/health` 轮询一起被拖住。
          而 fixture 内容是固定的，读一次就够。
        """
        if self._fixture_cache is None:
            try:
                with open(self.fixture, encoding="utf-8") as f:  # type: ignore[arg-type]
                    self._fixture_cache = json.load(f)
            except (OSError, ValueError) as e:
                # 读不到就每次都重试（文件是开发时手工放的，可能刚补上），
                # 但把原因记下来 —— 不然表现只是「一直在降级」，查不出所以然。
                _log.warning("BAIDU_FIXTURE 读取失败: %s: %s", type(e).__name__, e)
                return None
        return self._fixture_cache

    async def health(self) -> bool:
        """能否正常调用。

        ★ 绝不在这里发网络请求 —— `/v1/health` 被前端每 5 秒轮询一次，
          真打接口会烧配额也会拖慢健康检查。

        ★ 但也不能只看 AK 有没有填：AK 填了、格式也对，服务仍可能被禁用
          （实测 status=240「APP 服务被禁用」）。所以叠加「上次真实调用的
          结果」—— 调用过且失败了就如实报降级，成功过就恢复。
        """
        if self.fixture:
            # 开关打开但文件不在，和「AK 填了但服务被禁用」是同一类问题 ——
            # 每次 plan() 都会降级，健康检查却报 ok。
            #
            # ★ 文件在也还不够：fixture 里存的可能就是一份**失败响应**
            #   （样本目录里就有 status=240「APP 服务被禁用」那种）。
            #   那时 plan() 每次都返回 None、图层每次都播「地图服务暂时不可用」，
            #   健康检查却一直绿着 —— 正是上面那条「配置齐全 ≠ 服务可用」
            #   在 fixture 这条路上被漏掉。所以照样叠加「上次真实调用的结果」。
            return Path(self.fixture).is_file() and _last_failure is None
        if not self.ak:
            return False
        return _last_failure is None


def _coord(lat_lng: tuple[float, float]) -> str:
    """百度要「纬度,经度」，顺序与契约里的 `{lat, lng}` 一致。"""
    lat, lng = lat_lng
    return f"{lat:.6f},{lng:.6f}"
