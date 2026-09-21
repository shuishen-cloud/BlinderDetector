"""第三层无障碍路线规划。

★ 本文件测的是**策略层**（`rules/route.py`）—— 给它一份原始分段，看它
  怎么过滤、怎么警告、怎么说出来。数据源（`routers/`）不在这里测，
  它的解析逻辑在 `test_baidu_router.py`。

★ 原始分段直接取 `routers/builtin.py` 的 `RAW_STEPS`，**不在测试里手抄**
  —— 抄一份就等于允许两边漂移。
"""

from app.core.routers.builtin import RAW_STEPS
from app.core.rules import route


def req(avoid=None, dest="人民医院"):
    return route.RouteRequest(
        origin=(39.9087, 116.3975),
        destination=dest,
        avoid=avoid if avoid is not None else route.DEFAULT_AVOID,
    )


def detail(avoid=None, dest="人民医院", raw=None, router_name="builtin"):
    return route.build_detail(
        req(avoid, dest), RAW_STEPS if raw is None else raw, router_name=router_name
    )


# --------------------------------------------------------------------------
# 无障碍过滤
# --------------------------------------------------------------------------


def test_plan_returns_well_formed_detail():
    d = detail()
    assert d["kind"] == "route"
    assert d["route_id"]
    assert d["steps"]
    assert d["total_distance_m"] > 0
    assert d["total_duration_s"] > 0


def test_overpass_step_is_filtered_out():
    """任务书明确要求过滤天桥、地下通道、无电梯路段"""
    d = detail(avoid=["overpass"])
    assert all("天桥" not in s["instruction"] or "地面" in s["instruction"]
               for s in d["steps"])
    assert any("天桥" in w for w in d["warnings"])


def test_filtering_produces_a_warning():
    d = detail(avoid=["overpass", "underpass"])
    assert d["warnings"], "避开了障碍必须告知用户，不能静默改路线"


def test_no_avoid_keeps_all_steps():
    d = detail(avoid=[])
    assert not d["warnings"]


def test_total_distance_excludes_filtered_steps():
    full = detail(avoid=[])["total_distance_m"]
    filtered = detail(avoid=route.DEFAULT_AVOID)["total_distance_m"]
    assert filtered <= full


def test_route_id_is_stable_for_same_destination():
    assert detail()["route_id"] == detail()["route_id"]


def test_route_id_differs_for_different_destinations():
    assert detail(dest="A")["route_id"] != detail(dest="B")["route_id"]


def test_route_id_distinguishes_the_router():
    """降级后的内置路线与原本的百度路线必须是两个 id。

    否则去重键撞在一起，用户听到的还是那条被降级掉的路线。
    """
    builtin = detail(router_name="builtin")["route_id"]
    baidu = detail(router_name="baidu")["route_id"]
    assert builtin != baidu


# --------------------------------------------------------------------------
# 播报
# --------------------------------------------------------------------------


def test_step_announcement_may_contain_numbers():
    """★ 和第一、二层的关键区别：地图距离是可信的，可以播数字。

    「障碍物不播数字」那条规则只约束单目深度估计。
    """
    anns = route.step_announcements(detail())
    assert anns
    assert any(ch.isdigit() for ch in anns[0].text), "导航指令应该带具体米数"
    assert "米" in anns[0].text


def test_step_announcement_uses_navigation_source():
    anns = route.step_announcements(detail())
    assert all(a.source == "navigation" for a in anns)


def test_only_one_step_is_spoken_by_default():
    """把五步一口气念完，用户一步都记不住"""
    anns = route.step_announcements(detail(avoid=[]))
    step_anns = [a for a in anns if "step_index" in a.detail]
    assert len(step_anns) == 1


def test_max_steps_is_respected():
    anns = route.step_announcements(detail(avoid=[]), max_steps=3)
    step_anns = [a for a in anns if "step_index" in a.detail]
    assert len(step_anns) == 3


def test_step_dedup_keys_are_distinct():
    anns = route.step_announcements(detail(avoid=[]), max_steps=4)
    keys = [a.dedup_key for a in anns if "step_index" in a.detail]
    assert len(set(keys)) == len(keys)


def test_ttl_is_long_enough_for_the_text():
    from app.core.rules import phrasing
    for a in route.step_announcements(detail()):
        assert a.ttl_ms >= phrasing.speech_est_ms(a.text)


def test_warnings_are_announced():
    anns = route.step_announcements(detail(avoid=["overpass"]))
    assert any("warning" in a.detail for a in anns)


# --------------------------------------------------------------------------
# 诚实性：真实地图只能警告，不能声称「已避开」 ★
# --------------------------------------------------------------------------

#: 模拟真实地图返回的分段 —— **没有 barriers 元数据**，只有 instruction 文本。
#: 百度不提供「避开天桥」这种能力，我们能拿到的就这些。
REAL_MAP_STEPS = [
    {"instruction": "沿建国路向东步行 300 米", "maneuver": "straight", "distance_m": 300.0},
    {"instruction": "过天桥后继续直行 80 米", "maneuver": "straight", "distance_m": 80.0},
]


def test_builtin_may_claim_avoidance_because_it_is_true():
    """内置路网的 barriers 是手写元数据，那一步确实被丢掉了 —— 说「已避开」是真的。"""
    d = detail()
    assert any("已避开天桥" in w for w in d["warnings"])


def test_real_map_warns_instead_of_claiming_avoidance():
    """★ 真实地图拿不到障碍元数据，只能警告 —— 说「已避开」就是假话。

    对视障用户，「已避开天桥」会让他放心往前走，而前面正是一座天桥。
    """
    d = detail(raw=REAL_MAP_STEPS)
    assert any("前方有天桥" in w for w in d["warnings"])
    assert not any("已避开" in w for w in d["warnings"])


def test_text_scan_does_not_match_bare_place_names():
    """「天桥」是北京地名（天桥南大街），裸词匹配必然误报。

    让用户去绕一个根本不存在的东西，比不报更伤信任。
    """
    steps = [{"instruction": "沿天桥南大街向北步行 200 米",
              "maneuver": "straight", "distance_m": 200.0}]
    assert detail(raw=steps)["warnings"] == []


def test_text_scan_does_not_misread_elevators():
    """电梯的语义是反的：一条「乘坐无障碍电梯」恰恰是好路。

    按 no_elevator 去扫等于在用户最需要电梯的时候把他吓走，所以不扫这一类。
    """
    steps = [{"instruction": "乘坐无障碍电梯进入站厅",
              "maneuver": "straight", "distance_m": 30.0}]
    assert detail(raw=steps)["warnings"] == []


def test_detects_stairs_with_baidus_actual_wording():
    """★ 实测教训：百度写的是「过阶梯」，不是「上台阶」。

    最初的词表只有「上台阶/下台阶/走台阶/爬楼梯/走楼梯」，于是真实太原
    路线里那段「走70米,过阶梯,直行进入涧河路」**被漏报了** ——
    用户会走过一段台阶却收不到任何提示。
    """
    steps = [{"instruction": "走70米,过阶梯,直行进入涧河路",
              "maneuver": "straight", "distance_m": 66.0}]
    warnings = detail(raw=steps)["warnings"]
    assert any("台阶" in w for w in warnings), f"台阶没被识别: {warnings}"


def test_detects_underpass_with_baidus_actual_wording():
    """同一条真实路线里还有「下地下通道」「上地下通道」，这种要认。"""
    steps = [{"instruction": "走50米,下地下通道,直行",
              "maneuver": "straight", "distance_m": 50.0}]
    warnings = detail(raw=steps)["warnings"]
    assert any("地下通道" in w for w in warnings)


def test_repeated_barrier_kind_warns_only_once():
    """同一种障碍在一串步骤里被提到多次，用户只需要听到一次。"""
    steps = [
        {"instruction": "过天桥", "maneuver": "straight", "distance_m": 10.0},
        {"instruction": "继续走，再过一个天桥", "maneuver": "straight", "distance_m": 10.0},
    ]
    mentions = [w for w in detail(raw=steps)["warnings"] if "天桥" in w]
    assert len(mentions) == 1


def test_steps_without_barriers_are_never_filtered():
    """真实地图的步骤只可能被警告，绝不会被丢掉 —— 丢掉就是静默改路线。"""
    d = detail(raw=REAL_MAP_STEPS)
    assert len(d["steps"]) == len(REAL_MAP_STEPS)


def test_distance_is_not_spoken_twice():
    """★ 实测百度的 instruction 自带距离（「走770米,直行进入新城南大街」），
    再补一句「约 770 米」只会让用户多听一遍同样的信息。"""
    steps = [{"instruction": "走770米,直行进入新城南大街",
              "maneuver": "straight", "distance_m": 773.0}]
    text = route.step_announcements(detail(raw=steps))[0].text
    assert text == "走770米,直行进入新城南大街"
    assert text.count("770") == 1


def test_distance_is_still_appended_when_a_street_name_contains_metre():
    """★ 「米市大街」是北京真实街道名。

    只查裸字「米」的话这条会被误判成「自带距离」，于是距离**整段丢掉** ——
    用户在最需要距离的导航步骤上反而一个数字都听不到。
    判据必须是「数字 + 单位」。
    """
    steps = [{"instruction": "沿米市大街向北步行",
              "maneuver": "straight", "distance_m": 200.0}]
    text = route.step_announcements(detail(raw=steps))[0].text
    assert "约 200 米" in text


def test_builtin_plan_returns_copies_not_the_shared_constant():
    """★ 内置路网是**降级兜底**，最不该被污染。

    `plan()` 直接返回模块级常量的话，所有调用方共享同一个可变对象 ——
    将来任何一处原地加工都会静默改掉整个进程的兜底路线。
    """
    import asyncio

    from app.core.routers import builtin as B

    r1 = asyncio.run(B.BuiltinRouter().plan((1.0, 2.0), (3.0, 4.0)))
    r2 = asyncio.run(B.BuiltinRouter().plan((1.0, 2.0), (3.0, 4.0)))
    assert r1 is not r2 and r1[0] is not r2[0]

    r1[0]["distance_m"] = 99999.0
    assert B.RAW_STEPS[0]["distance_m"] != 99999.0


def test_distance_in_kilometres_is_also_recognised():
    """★ 实测百度长段写「走2.1公里」而不是「米」。

    只查「米」的话会变成「走2.1公里,直行进入解放北路辅路，约 2095 米」
    —— 同一件事说两遍，还换了单位。
    """
    steps = [{"instruction": "走2.1公里,直行进入解放北路辅路",
              "maneuver": "straight", "distance_m": 2095.0}]
    text = route.step_announcements(detail(raw=steps))[0].text
    assert text == "走2.1公里,直行进入解放北路辅路"
    assert "2095" not in text


def test_distance_is_appended_when_instruction_lacks_it():
    """内置路网的指令不含「米」，所以还是走追加那条路。"""
    text = route.step_announcements(detail(avoid=[]))[0].text
    assert "米" in text and "约" in text
