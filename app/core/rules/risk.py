"""
第二层：障碍物风险分级。

两条原则：

1. **用悲观距离分级。** 单目深度估计误差 30–50%，而且误差往「低估距离」
   的方向是有害的 —— 系统说 2 米、实际 0.8 米，用户按 2 米的心理预期
   迈步就会撞上。所以分级一律用 `distance - sigma`（悲观下界）。

2. **置信度不够就不播。** 播报的价值取决于用户是否相信它，而信任是消耗品：
   每次误报（报了 danger 但没事）都在消耗信任，直到用户开始忽略预警 ——
   包括真的那一次。所以宁可少报，不可误报。
"""

from __future__ import annotations

from app.contracts import (
    DISTANCE_CLOSE,
    DISTANCE_FAR,
    DISTANCE_MEDIUM,
    DISTANCE_NEAR,
    DISTANCE_TOUCH,
    RISK_DANGER,
    RISK_INFO,
    RISK_WARNING,
    distance_bucket,
)

# --------------------------------------------------------------------------
# 门限
# --------------------------------------------------------------------------

#: 低于这个置信度的障碍物直接不生成 —— 不播比误播好
MIN_OBSTACLE_CONFIDENCE = 0.50

#: 各类型的固有危险度。数字越大越危险，用来决定「多远处就该开始提示」。
#: 只放团队能实际验证的类别，验证不了的不要进这张表。
TYPE_SEVERITY: dict[str, int] = {
    "step_down": 2,  # 踏空跌落，后果最重
    "pothole": 2,
    "curb": 2,  # 路缘，容易踩空
    "glass_door": 2,  # ★ 单目深度对透明/反射表面灾难性失效，而撞玻璃门是经典伤害场景
    "vehicle": 2,
    "bicycle": 1,
    "step_up": 1,
    "pole": 1,
    "person": 0,  # 人会自己避让，不需要强预警
    "other": 1,
}

#: 各危险度等级从多少米外开始给出提示。越危险，越早提示。
ALERT_HORIZON_M: dict[int, float] = {
    2: 4.0,
    1: 2.5,
    0: 1.2,
}

#: 碰撞时间低于这个值就是 danger（秒）
TTC_DANGER_S = 2.0
TTC_WARNING_S = 4.0

#: 认定的「正在接近」最小速度（米/秒）。低于这个值当作静止。
MIN_CLOSING_SPEED_MPS = 0.05

#: 用户默认步行速度（米/秒）。用于没有实测值时的兜底。
DEFAULT_WALK_SPEED_MPS = 1.1


# --------------------------------------------------------------------------
# 计算
# --------------------------------------------------------------------------


def pessimistic_distance(distance_m: float, sigma_m: float | None) -> float:
    """悲观下界 —— 分级一律用它，不用中位数估计。

    单目深度估计的误差不对称地有害：高估距离会让人撞上去，
    低估距离只会让人多绕一步。所以取 `distance - sigma`。
    """
    sigma = sigma_m if sigma_m and sigma_m > 0 else 0.0
    return max(0.0, distance_m - sigma)


def time_to_collision(distance_m: float, closing_speed_mps: float | None) -> float | None:
    """碰撞剩余时间（秒）。物体静止或远去时返回 None。"""
    if closing_speed_mps is None or closing_speed_mps < MIN_CLOSING_SPEED_MPS:
        return None
    if distance_m <= 0:
        return 0.0
    return distance_m / closing_speed_mps


def is_publishable(confidence: float) -> bool:
    """这条障碍物够不够格播出去。"""
    return confidence >= MIN_OBSTACLE_CONFIDENCE


def classify(
    obstacle_type: str,
    distance_m: float,
    sigma_m: float | None = None,
    confidence: float = 1.0,
    closing_speed_mps: float | None = None,
) -> str | None:
    """定风险等级。返回 None 表示这条不该播。

    分级顺序（先到先判）：
      1. 置信度不够 -> None
      2. 碰撞时间 < 2s 或已到脚下 -> danger
      3. 在悲观距离的提示视野内 -> 至少 warning
      4. 碰撞时间 < 4s -> warning
      5. 其余 -> info
    """
    if not is_publishable(confidence):
        return None

    severity = TYPE_SEVERITY.get(obstacle_type, 1)
    d = pessimistic_distance(distance_m, sigma_m)
    bucket = distance_bucket(d)
    ttc = time_to_collision(d, closing_speed_mps)

    # 已经在脚前，无论是什么都是危险
    if bucket == DISTANCE_TOUCH:
        return RISK_DANGER
    if ttc is not None and ttc < TTC_DANGER_S:
        return RISK_DANGER

    # 进入该类型的提示视野
    if d <= ALERT_HORIZON_M[severity]:
        if severity >= 2:
            return RISK_WARNING if bucket in (DISTANCE_NEAR, DISTANCE_CLOSE) else RISK_INFO
        if severity == 1 and bucket == DISTANCE_NEAR:
            return RISK_WARNING

    if ttc is not None and ttc < TTC_WARNING_S:
        return RISK_WARNING

    if bucket in (DISTANCE_NEAR, DISTANCE_CLOSE):
        return RISK_INFO
    if bucket in (DISTANCE_MEDIUM, DISTANCE_FAR):
        return RISK_INFO
    return RISK_INFO


def ttl_for(
    distance_m: float,
    walk_speed_mps: float | None = None,
    *,
    min_ms: int = 1500,
    max_ms: int = 8000,
) -> int:
    """这条预警在多久之内说出来才有意义。

    走得越慢，同一个障碍物的有效窗口越长 —— 用固定 TTL 会让慢速用户
    在走到之前预警就过期了。留 1.5 倍安全裕度。
    """
    speed = walk_speed_mps or DEFAULT_WALK_SPEED_MPS
    speed = max(speed, 0.3)  # 防除零，也防不合理的极慢值
    seconds = distance_m / speed * 1.5
    return int(min(max(seconds * 1000, min_ms), max_ms))
