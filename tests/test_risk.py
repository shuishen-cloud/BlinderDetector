"""第二层风险分级规则。"""

import pytest

from app.contracts import (
    DISTANCE_CLOSE,
    DISTANCE_FAR,
    DISTANCE_NEAR,
    DISTANCE_TOUCH,
    RISK_DANGER,
    RISK_INFO,
    RISK_WARNING,
    distance_bucket,
)
from app.core.rules import risk


# --------------------------------------------------------------------------
# 悲观距离
# --------------------------------------------------------------------------


def test_pessimistic_distance_subtracts_sigma():
    """★ 分级必须用悲观下界。

    误差不对称地有害：高估距离会让人撞上去，低估只会让人多绕一步。
    """
    assert risk.pessimistic_distance(2.0, 0.7) == pytest.approx(1.3)


def test_pessimistic_distance_never_negative():
    assert risk.pessimistic_distance(0.5, 3.0) == 0.0


def test_pessimistic_distance_handles_missing_sigma():
    assert risk.pessimistic_distance(2.0, None) == 2.0


def test_sigma_changes_the_bucket():
    """误差大到一定程度会把障碍物从「几米外」拉到「很近」"""
    assert distance_bucket(2.0) == DISTANCE_CLOSE
    assert distance_bucket(risk.pessimistic_distance(2.0, 0.7)) == DISTANCE_NEAR


# --------------------------------------------------------------------------
# 置信度门限
# --------------------------------------------------------------------------


def test_low_confidence_is_not_published():
    """宁可少报，不可误报 —— 信任是消耗品。"""
    assert risk.classify("vehicle", 3.0, 0.5, confidence=0.41) is None


def test_confidence_at_threshold_passes():
    assert risk.classify("vehicle", 3.0, 0.5, confidence=0.50) is not None


# --------------------------------------------------------------------------
# 分级
# --------------------------------------------------------------------------


def test_obstacle_at_feet_is_danger():
    assert risk.classify("step_down", 0.4, 0.1, 0.9) == RISK_DANGER


def test_fast_approaching_object_is_danger():
    """5 米外但 3 m/s 冲过来 -> 碰撞时间 1.7 秒 -> danger"""
    assert risk.classify("bicycle", 5.0, 0.5, 0.9, closing_speed_mps=3.0) == RISK_DANGER


def test_slow_approaching_object_is_not_danger():
    """同样是 5 米，慢慢靠近就不是 danger"""
    assert risk.classify("bicycle", 5.0, 0.5, 0.9, closing_speed_mps=0.5) != RISK_DANGER


def test_static_object_has_no_ttc():
    assert risk.time_to_collision(5.0, None) is None
    assert risk.time_to_collision(5.0, 0.0) is None


def test_nearby_step_is_at_least_warning():
    """2 米外的台阶，用户两三步就到"""
    assert risk.classify("step_down", 2.0, 0.7, 0.86) == RISK_WARNING


def test_person_is_low_severity():
    """人会自己避让，不需要强预警 —— 报多了会淹没真正的危险"""
    result = risk.classify("person", 3.0, 0.5, 0.9)
    assert result == RISK_INFO


def test_severe_types_have_a_wider_alert_horizon():
    """同样 2.5 米，台阶要警告，行人不用 —— 危险类型提示得更早。

    否则「左边有人」这类低价值播报会淹没真正要命的台阶预警。
    """
    assert risk.classify("step_down", 2.5, 0.5, 0.9) == RISK_WARNING
    assert risk.classify("person", 2.5, 0.5, 0.9) == RISK_INFO


def test_step_degrades_to_info_before_entering_the_warning_band():
    """3~4 米之间台阶只轻声提一句，进 3 米才升级成警告。

    距离档位是左闭右开：悲观距离 3.0 米算 MEDIUM 而不是 CLOSE。
    """
    assert risk.classify("step_down", 3.5, 0.0, 0.9) == RISK_INFO
    assert risk.classify("step_down", 2.9, 0.0, 0.9) == RISK_WARNING


def test_unknown_type_falls_back_to_default_severity():
    """没登记的类型按中等危险度处理，不能直接崩或者静默放行"""
    assert risk.classify("不认识的类型", 1.0, 0.2, 0.9) == RISK_WARNING


def test_glass_door_is_treated_as_severe():
    """单目深度对透明表面灾难性失效，但撞玻璃门是经典伤害场景 ——
    不能因为「测不准」就不报。"""
    assert risk.TYPE_SEVERITY["glass_door"] == risk.TYPE_SEVERITY["step_down"]


# --------------------------------------------------------------------------
# TTL
# --------------------------------------------------------------------------


def test_ttl_scales_with_distance():
    near = risk.ttl_for(1.0, 1.1)
    far = risk.ttl_for(6.0, 1.1)
    assert far > near


def test_ttl_is_longer_for_slower_walkers():
    """走得慢的用户，同一个障碍物的有效窗口更长 ——
    固定 TTL 会让他在走到之前预警就过期了。"""
    fast = risk.ttl_for(3.0, 1.5)
    slow = risk.ttl_for(3.0, 0.6)
    assert slow > fast


def test_ttl_is_clamped():
    assert risk.ttl_for(0.1, 5.0) >= 1500
    assert risk.ttl_for(500.0, 0.3) <= 8000


def test_ttl_handles_zero_speed():
    """不能除零"""
    assert risk.ttl_for(2.0, 0.0) > 0
