"""路线数据源抽象。

★ 把「路线数据从哪来」和「无障碍策略怎么定」分开：

    routers/        只回答「地图说怎么走」（拿原始分段）
    rules/route.py  回答「怎么过滤、怎么警告、怎么说出来」

这与 `detectors/` 之于 `rules/risk.py` 是同一个分工 —— 换地图厂商
（百度 / 高德 / 内置假路网）时，无障碍策略一行都不用动。

★ 三个返回值的语义，降级行为全靠它区分：

    None   -> 服务不可用（超时 / HTTP 错误 / 厂商返回错误码）→ 触发降级
    []     -> 服务正常，但确实规划不出路线
    [...]  -> 正常路线

把「我参数没传对」和「真的没有路线」混为一谈，就会对着用户说假话，
所以这两个必须分开表达。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Router(Protocol):
    async def plan(
        self, origin: tuple[float, float], destination: tuple[float, float] | None
    ) -> list[dict] | None:
        """规划一条步行路线，返回**未过滤**的原始分段。

        `origin` / `destination` 都是 `(纬度, 经度)`，WGS-84（端侧 GPS 原生）。

        ★ 坐标系换算由 router **委托给厂商**完成（百度收 `coord_type=wgs84`），
          本项目**不自己实现** WGS-84 → BD-09 的转换 —— 那是一套非官方的近似
          算法，与「不写没有依据的精确」这条一以贯之的做法冲突。

        ★ `destination` 为 `None` 表示端侧没给目的地坐标。真实地图 API
          **只认坐标、不认地名**（「最近的地铁站」得先地理编码），所以这种
          情况必须返回 `None` 触发降级，绝不能瞎猜一个目的地 —— 那等于
          对着视障用户自信地播报一条根本不对的路线。

        每段的形状：`{"instruction": str, "distance_m": float,
        "maneuver": str, "path": list, "barriers": list}`，
        其中 `barriers` 可选。

        ★ `barriers` 只有**客户端自己知道**障碍元数据时才给（内置假路网就是
          手写的）。真实地图 API 不提供这个字段，所以那种路线只能靠
          `rules/route.py` 扫文本做**警告** —— 做不到声称「已避开」。
        ★ `path` 是这一段的折线 `[[经度, 纬度], ...]`；没有坐标的数据源给
          空列表。它**只用于可视化**，不参与过滤、警告、措辞任何一步。

        ★★ 实现类**应当**提供类属性 `coord_system`（`"bd09ll"` 或 `None`），
          说明 `path` 用的是哪个坐标系。它**刻意不进这个 Protocol**：
          给 `@runtime_checkable` 的 Protocol 加非方法成员会让
          `issubclass()` 直接抛 TypeError。读取方一律用
          `getattr(router, "coord_system", None)` 兜底 ——
          第三方按 README 加的实现忘了这个属性，不该在请求路径上炸。
        """
        ...

    async def health(self) -> bool:
        """能否正常调用。★ 只做**配置检查**，绝不在里面发网络请求 ——
        `/v1/health` 被前端每 5 秒轮询一次，真打接口会烧配额。"""
        ...
