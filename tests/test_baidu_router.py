"""百度地图步行路线规划的**解析层**。

★ 只测纯同步函数 `parse_walking_response()`，不发任何网络请求 ——
  项目里没有任何 HTTP 桩先例，也不打算为它引入新依赖
  （`pytest-asyncio` 没装，`anyio` 的插件虽有但不值得为这点逻辑引进来）。
  HTTP 调用本身很薄，它的失败路径由端到端用例覆盖。

★ 测试用的 payload 是照着百度文档手工构造的。**拿到真 AK 后必须真调一次
  核对字段假设** —— 尤其是 `instructions`（复数）和 `steps_info=1`。
"""

import pytest

from app.core.routers import baidu
from app.core.routers.baidu import _maneuver_of, parse_walking_response

#: 一个形状正常的步行规划响应。★ 三个细节都照**实测**的真实响应来，
#: 不是照百度文档 —— 文档说字段是 instructions（复数），实测是单数 instruction。
OK_PAYLOAD = {
    "status": 0,
    "message": "ok",
    "result": {
        "origin": {"lng": 112.543633, "lat": 37.957919},
        "destination": {"lng": 112.581822, "lat": 37.859717},
        "routes": [
            {
                "distance": 12553,
                "duration": 11265,
                "steps": [
                    {"instruction": "向正南方向出发,走30米,<b>过马路左转</b>进入<b>新兰路</b>",
                     "distance": 29, "turn_type": "过马路左转"},
                    {"instruction": "走770米,<b>直行</b>进入<b>新城南大街</b>",
                     "distance": 773, "turn_type": "直行"},
                    # ★ 实测：turn_type 可能是**空字符串**，而 turn_type_id 有值
                    {"instruction": "走10米,<b>过马路右转</b>进入<b>恒山路</b>",
                     "distance": 13, "turn_type": "", "turn_type_id": 24},
                ],
            }
        ],
    },
}


def test_parses_normal_response():
    steps = parse_walking_response(OK_PAYLOAD)
    assert steps is not None
    assert len(steps) == 3
    assert steps[0] == {
        "instruction": "向正南方向出发,走30米,过马路左转进入新兰路",  # HTML 已剥掉
        "distance_m": 29.0,
        "maneuver": "left",
    }
    assert steps[1]["maneuver"] == "straight"


def test_html_tags_are_stripped():
    """★ 实测真实响应带 `<b>` 高亮标签。

    契约里 `text` 是「端侧唯一消费字段、只播 text」，绝不能带标记 ——
    不剥掉就会把尖括号原样播给视障用户。
    """
    for step in parse_walking_response(OK_PAYLOAD):
        assert "<" not in step["instruction"]
        assert ">" not in step["instruction"]


def test_empty_turn_type_falls_back_to_instruction():
    """★ 实测：`turn_type` 可能是空串（`turn_type_id` 却有值）。

    只看 `turn_type` 会把这次转弯判成直行 → 用户收不到那次震动提示。
    空了就退回扫 instruction，那句话里明写着「过马路右转」。
    """
    steps = parse_walking_response(OK_PAYLOAD)
    assert steps[2]["maneuver"] == "right"


def test_parsed_steps_carry_no_barriers_key():
    """★ 真实地图的步骤**必须没有** `barriers` 字段。

    这不是巧合，是 `rules/route.py` 用来区分「能声称已避开」和「只能警告」
    的依据 —— 一旦这里多出一个 barriers，真实路线就会被误认为掌握障碍元数据。
    """
    for step in parse_walking_response(OK_PAYLOAD):
        assert "barriers" not in step


# --------------------------------------------------------------------------
# 不可用的响应 —— 一律 None（触发降级），绝不能假装「没有路线」
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    None,
    [],
    "not a dict",
    {},                                            # 什么都没有
    {"status": 2, "message": "AK 参数错误"},         # 百度报错
    {"status": "0"},                               # 有状态没结果
    {"status": 0, "result": {}},                   # 结果里没有 routes
    {"status": 0, "result": {"routes": None}},
    {"status": 0, "result": {"routes": [{}]}},     # route 里没有 steps
    # ★ 只校验 routes 是 list 不够，元素本身也得是 dict ——
    #   少了这层校验会在 .get() 抛 AttributeError 冒成 500
    {"status": 0, "result": {"routes": ["oops"]}},
    {"status": 0, "result": {"routes": [None]}},
    {"status": 0, "result": {"routes": [42]}},
])
def test_unusable_response_returns_none(payload):
    assert parse_walking_response(payload) is None


def test_status_as_string_zero_is_accepted():
    """不同端点的 status 类型不一致，字符串 "0" 也要认。"""
    payload = {**OK_PAYLOAD, "status": "0"}
    assert parse_walking_response(payload) is not None


def test_empty_routes_means_no_route_not_failure():
    """★ 服务正常但确实没有路线 -> `[]`，与「服务不可用」的 None 是两件事。

    混为一谈就会把「我参数没传对」说成「没有路线」，那是对用户说假话。
    """
    assert parse_walking_response({"status": 0, "result": {"routes": []}}) == []


# --------------------------------------------------------------------------
# 字段容错
# --------------------------------------------------------------------------


def test_accepts_plural_instructions_spelling():
    """★ 实测真实响应用的是**单数** `instruction`；复数 `instructions` 是
    百度文档（和大量博客）写的 —— **文档是错的**。

    这里保留复数的兼容，是因为不确定所有端点/版本是否一致：宁可多认一种，
    也不要因为字段名不对就把整条路线丢掉。
    """
    payload = {
        "status": 0,
        "result": {"routes": [{"steps": [
            {"instructions": "沿人行道直行", "distance": 100},
        ]}]},
    }
    steps = parse_walking_response(payload)
    assert steps[0]["instruction"] == "沿人行道直行"


def test_skips_steps_without_text_and_non_dict_steps():
    payload = {
        "status": 0,
        "result": {"routes": [{"steps": [
            {"instructions": "", "distance": 10},
            {"distance": 10},          # 没有文本
            "not a dict",
            {"instructions": "左转", "distance": 20},
        ]}]},
    }
    steps = parse_walking_response(payload)
    assert len(steps) == 1
    assert steps[0]["instruction"] == "左转"


@pytest.mark.parametrize("distance", [None, "abc", [], {"a": 1}])
def test_unparseable_distance_becomes_zero(distance):
    """距离取不到只是播报少个数字，不该让整条链路抛异常冒成 500。"""
    payload = {
        "status": 0,
        "result": {"routes": [{"steps": [
            {"instruction": "直行", "distance": distance},
        ]}]},
    }
    assert parse_walking_response(payload)[0]["distance_m"] == 0.0


def test_missing_distance_becomes_zero():
    payload = {
        "status": 0,
        "result": {"routes": [{"steps": [{"instruction": "直行"}]}]},
    }
    assert parse_walking_response(payload)[0]["distance_m"] == 0.0


# --------------------------------------------------------------------------
# turn_type -> maneuver
# --------------------------------------------------------------------------


@pytest.mark.parametrize("turn_type,expected", [
    ("直行", "straight"),
    ("右转", "right"),
    ("右前方转弯", "right"),
    ("过马路右转", "right"),
    ("左转", "left"),
    ("左前方转弯", "left"),
    ("过马路左转", "left"),
    ("到达目的地", "arrive"),
])
def test_turn_type_mapping(turn_type, expected):
    assert _maneuver_of(turn_type) == expected


@pytest.mark.parametrize("turn_type", [None, "", "东南方向", "途经点", "疑似未知词"])
def test_unknown_turn_type_defaults_to_straight(turn_type):
    """★ 默认值必须是 straight。

    认不出来就当直行 —— 既不会因为转向词没见过而丢掉路线，也不会让
    每一个没识别出的步骤都带上震动（`step_announcements` 靠
    `maneuver != "straight"` 决定震不震，默认成别的值会让一条 20 步的
    路线震 20 次，震动信号直接退化成噪音）。
    """
    assert _maneuver_of(turn_type) == "straight"


@pytest.mark.parametrize("instruction,expected", [
    # 地名里的「左」不能骗过它 —— 实测裸字匹配会把这次右转判成左转
    ("走10米,过马路右转进入左家庄东街", "right"),
    # 「到达」出现在句中不是终点，转向优先
    ("沿道路直行到达官厅路口右转", "right"),
    ("走80米,到达终点", "arrive"),
    # 位置描述不是动作：「目的地在您的右侧」说的是终点在哪，不是让你右转
    ("直行，目的地在您的右侧", "straight"),
    ("过马路左转进入新兰路", "left"),
])
def test_maneuver_ignores_place_names_and_position_descriptions(instruction, expected):
    """★ 只认**转向词组**（左转/右前方…），不认裸字「左/右」，
    也不认「左侧/右侧」这种位置描述。"""
    assert _maneuver_of("", instruction) == expected


# --------------------------------------------------------------------------
# health()：配置齐全 ≠ 服务可用 ★
# --------------------------------------------------------------------------


def _health(*, ak: str = "dummy-ak", fixture: str = ""):
    """`health()` 是 async，但这里没必要为此引入 pytest-asyncio ——
    用标准库的 `asyncio.run()` 就够，测试文件保持全同步。

    `fixture` 默认空串：不然开发机 `.env` 里的 BAIDU_FIXTURE 会让用例行为不确定。
    """
    import asyncio

    return asyncio.run(baidu.BaiduRouter(ak=ak, fixture=fixture).health())


def test_health_false_without_ak():
    assert _health(ak="") is False


def test_health_true_with_ak_and_no_recorded_failure(monkeypatch):
    monkeypatch.setattr(baidu, "_last_failure", None)
    assert _health(ak="dummy-ak") is True


def test_health_false_after_a_failed_call(monkeypatch):
    """★ 实测踩到的坑：AK 填了、格式也对，但服务被禁用（status=240）。

    只在 AK 上做检查的话，`/v1/health` 会在系统实际规划不出路线时依然报 ok
    —— 而「没出声」和「系统哑了」分不开，正是本项目反复强调要避免的失效模式。
    """
    monkeypatch.setattr(baidu, "_last_failure", "240 APP 服务被禁用")
    assert _health(ak="dummy-ak") is False


def test_plan_without_ak_returns_none_without_any_request():
    """没有 AK 时直接返回 None 触发降级 —— 不发无谓的请求，也绝不抛异常
    （抛出去会在图层构造期变成 500，降级路径根本来不及触发）。"""
    import asyncio

    router = baidu.BaiduRouter(ak="", fixture="")
    assert asyncio.run(router.plan((39.9, 116.4), (39.91, 116.41))) is None


def test_health_false_when_fixture_file_is_missing():
    """开关打开但文件不在，和「AK 填了但服务被禁用」是同一类问题：
    每次 plan() 都降级，健康检查却报 ok。"""
    assert _health(fixture="data/does-not-exist.json") is False


def test_health_true_when_fixture_file_exists():
    assert _health(fixture="data/baidu_walking_sample.json") is True


def test_plan_without_destination_geo_returns_none():
    """百度只认坐标不认地名 —— 没坐标就别猜，交给图层如实降级。"""
    import asyncio

    router = baidu.BaiduRouter(ak="dummy-ak", fixture="")
    assert asyncio.run(router.plan((39.9, 116.4), None)) is None
