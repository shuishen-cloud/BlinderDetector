"""契约自检 —— 零第三方依赖，直接跑也能过。

    python -m pytest tests/test_contracts.py
"""

import json

from app import contracts as C
from app.mock import fixtures as F


# --------------------------------------------------------------------------
# 两个信封的基本形状
# --------------------------------------------------------------------------


def test_frame_constructs_and_serializes():
    d = F.SAMPLE_FRAME.to_dict()
    assert d["frame_id"] == "f0001"
    assert d["source"] == C.SOURCE_PERCEPTION
    assert json.dumps(d, ensure_ascii=False)  # 可 JSON 化


def test_announcement_required_fields():
    a = F.PERCEPTION
    for f in ("id", "ts", "source", "priority", "text", "ttl_ms", "dedup_key"):
        assert getattr(a, f) not in (None, ""), f"{f} 不能为空"


def test_announcement_to_json_is_utf8_not_escaped():
    assert "红绿灯" in F.PERCEPTION.to_json()


def test_all_sources_have_a_fixture():
    for src in (C.SOURCE_PERCEPTION, C.SOURCE_SAFETY, C.SOURCE_NAVIGATION,
                C.SOURCE_EMERGENCY, C.SOURCE_SYSTEM):
        assert src in F.BY_SOURCE, f"{src} 缺样例"


# --------------------------------------------------------------------------
# detail 形状
# --------------------------------------------------------------------------


def test_every_fixture_detail_has_kind():
    for src, ann in F.BY_SOURCE.items():
        assert "kind" in ann.detail, f"{src} 的 detail 缺 kind"


def test_vision_detail_shape():
    d = F.PERCEPTION.detail
    assert d["kind"] == "vision"
    assert isinstance(d["scene_description"], str)
    assert d["ocr_results"][0]["text"]
    obj = d["objects"][0]
    for f in ("label", "confidence", "position", "bbox"):
        assert f in obj, f"objects 缺 {f}"


def test_positions_are_legal():
    for obj in F.PERCEPTION.detail["objects"]:
        assert obj["position"] in (C.POSITION_LEFT, C.POSITION_CENTER, C.POSITION_RIGHT)


def test_bbox_is_normalized():
    """bbox 必须是归一化 [0,1]，不是像素坐标 —— 组员最容易对不上的地方"""
    for obj in F.PERCEPTION.detail["objects"]:
        x, y, w, h = obj["bbox"]
        assert all(0.0 <= v <= 1.0 for v in (x, y, w, h)), f"未归一化: {obj['bbox']}"


def test_safety_detail_shape():
    d = F.SAFETY.detail
    assert d["kind"] == "safety"
    assert d["risk"] in (C.RISK_INFO, C.RISK_WARNING, C.RISK_DANGER)
    obs = d["obstacles"][0]
    assert obs["type"] in C.OBSTACLE_TYPES
    assert "distance_sigma_m" in obs, "障碍物必须带深度误差范围"


def test_route_detail_shape():
    d = F.NAVIGATION.detail
    assert d["kind"] == "route"
    assert d["steps"][0]["instruction"]
    assert d["total_distance_m"] > 0


def test_emergency_detail_shape():
    d = F.FALL_SUSPECTED_ANN.detail
    assert d["kind"] == "emergency"
    assert d["state"] == C.FALL_SUSPECTED
    assert d["idempotency_key"], "必须有幂等键，否则重试会重复呼叫家属"


def test_system_detail_shape():
    assert F.DEGRADED.detail["kind"] == "system"
    assert F.DEGRADED.detail["reason"]


# --------------------------------------------------------------------------
# 距离档位与去重键（安全关键）
# --------------------------------------------------------------------------


def test_distance_bucket_boundaries():
    assert C.distance_bucket(0.3) == C.DISTANCE_TOUCH
    assert C.distance_bucket(1.0) == C.DISTANCE_NEAR
    assert C.distance_bucket(2.0) == C.DISTANCE_CLOSE
    assert C.distance_bucket(4.0) == C.DISTANCE_MEDIUM
    assert C.distance_bucket(9.0) == C.DISTANCE_FAR


def test_dedup_key_changes_when_risk_escalates():
    """★ 最关键的一条：风险升级必须产生不同的去重键。

    否则 台阶(info,3.2m) -> 台阶(warning,1.8m) -> 台阶(danger,0.7m)
    会被当成「同一个台阶的重复」全部吞掉，被吞的恰恰是最危险那条。
    """
    info = C.make_dedup_key("obstacle", "step_down", "center", C.RISK_INFO, 3.2)
    warn = C.make_dedup_key("obstacle", "step_down", "center", C.RISK_WARNING, 1.8)
    danger = C.make_dedup_key("obstacle", "step_down", "center", C.RISK_DANGER, 0.7)

    assert len({info, warn, danger}) == 3, "风险升级产生了相同的去重键，会被误去重"


def test_dedup_key_changes_when_distance_bucket_changes():
    a = C.make_dedup_key("obstacle", "step_down", "center", C.RISK_DANGER, 2.9)
    b = C.make_dedup_key("obstacle", "step_down", "center", C.RISK_DANGER, 1.1)
    assert a != b, "距离档位变化应产生不同的去重键"


def test_fixture_safety_dedup_key_matches_its_obstacle():
    """样例里的 dedup_key 必须和 obstacles[0] 一致，否则去重测不准"""
    obs = F.SAFETY.detail["obstacles"][0]
    expected = C.make_dedup_key("obstacle", obs["type"], obs["position"],
                                obs["risk"], obs["distance_m"])
    assert F.SAFETY.dedup_key == expected


# --------------------------------------------------------------------------
# 震动与优先级
# --------------------------------------------------------------------------


def test_critical_priority_gets_strong_haptic():
    a = C.announcement(C.SOURCE_SAFETY, C.PRIORITY_CRITICAL, "前方有台阶", 6000, "k")
    assert a.haptic == C.HAPTIC_DOUBLE


def test_background_priority_is_silent():
    a = C.announcement(C.SOURCE_PERCEPTION, C.PRIORITY_BACKGROUND, "前方有咖啡店", 5000, "k")
    assert a.haptic == C.HAPTIC_NONE
