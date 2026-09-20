"""第三层无障碍路线规划。"""

from app.core.rules import route


def req(avoid=None, dest="人民医院"):
    return route.RouteRequest(
        origin=(39.9087, 116.3975),
        destination=dest,
        avoid=avoid if avoid is not None else route.DEFAULT_AVOID,
    )


# --------------------------------------------------------------------------
# 无障碍过滤
# --------------------------------------------------------------------------


def test_plan_returns_well_formed_detail():
    d = route.plan(req())
    assert d["kind"] == "route"
    assert d["route_id"]
    assert d["steps"]
    assert d["total_distance_m"] > 0
    assert d["total_duration_s"] > 0


def test_overpass_step_is_filtered_out():
    """任务书明确要求过滤天桥、地下通道、无电梯路段"""
    d = route.plan(req(avoid=["overpass"]))
    assert all("天桥" not in s["instruction"] or "地面" in s["instruction"]
               for s in d["steps"])
    assert any("天桥" in w for w in d["warnings"])


def test_filtering_produces_a_warning():
    d = route.plan(req(avoid=["overpass", "underpass"]))
    assert d["warnings"], "避开了障碍必须告知用户，不能静默改路线"


def test_no_avoid_keeps_all_steps():
    d = route.plan(req(avoid=[]))
    assert not d["warnings"]


def test_total_distance_excludes_filtered_steps():
    full = route.plan(req(avoid=[]))["total_distance_m"]
    filtered = route.plan(req(avoid=route.DEFAULT_AVOID))["total_distance_m"]
    assert filtered <= full


def test_route_id_is_stable_for_same_destination():
    assert route.plan(req())["route_id"] == route.plan(req())["route_id"]


def test_route_id_differs_for_different_destinations():
    assert route.plan(req(dest="A"))["route_id"] != route.plan(req(dest="B"))["route_id"]


# --------------------------------------------------------------------------
# 播报
# --------------------------------------------------------------------------


def test_step_announcement_may_contain_numbers():
    """★ 和第一、二层的关键区别：地图距离是可信的，可以播数字。

    「障碍物不播数字」那条规则只约束单目深度估计。
    """
    anns = route.step_announcements(route.plan(req()))
    assert anns
    assert any(ch.isdigit() for ch in anns[0].text), "导航指令应该带具体米数"
    assert "米" in anns[0].text


def test_step_announcement_uses_navigation_source():
    anns = route.step_announcements(route.plan(req()))
    assert all(a.source == "navigation" for a in anns)


def test_only_one_step_is_spoken_by_default():
    """把五步一口气念完，用户一步都记不住"""
    anns = route.step_announcements(route.plan(req(avoid=[])))
    step_anns = [a for a in anns if "step_index" in a.detail]
    assert len(step_anns) == 1


def test_max_steps_is_respected():
    anns = route.step_announcements(route.plan(req(avoid=[])), max_steps=3)
    step_anns = [a for a in anns if "step_index" in a.detail]
    assert len(step_anns) == 3


def test_step_dedup_keys_are_distinct():
    anns = route.step_announcements(route.plan(req(avoid=[])), max_steps=4)
    keys = [a.dedup_key for a in anns if "step_index" in a.detail]
    assert len(set(keys)) == len(keys)


def test_ttl_is_long_enough_for_the_text():
    from app.core.rules import phrasing
    for a in route.step_announcements(route.plan(req())):
        assert a.ttl_ms >= phrasing.speech_est_ms(a.text)


def test_warnings_are_announced():
    anns = route.step_announcements(route.plan(req(avoid=["overpass"])))
    assert any("warning" in a.detail for a in anns)
