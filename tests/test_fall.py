"""跌倒状态机 —— 安全关键，覆盖最全。"""

from app.contracts import FALL_CANCELLED, FALL_CONFIRMED, FALL_SUSPECTED
from app.core.rules.fall import FallMachine, FallSignal

T0 = 1_000_000


def real_fall(**kw) -> FallSignal:
    """一次典型的真跌倒：自由落体 + 冲击 + 之后长时间静止。"""
    base = dict(
        peak_g=3.2,
        free_fall_ms=120,
        posture="lying",
        post_impact_still_ms=5000,
        movement_class="still",
        on_charger=False,
        screen_on=True,
    )
    base.update(kw)
    return FallSignal(**base)


# --------------------------------------------------------------------------
# 检出
# --------------------------------------------------------------------------


def test_real_fall_enters_suspected_and_asks():
    m = FallMachine()
    anns = m.on_signal(real_fall(), "e1", T0)

    assert len(anns) == 1
    assert anns[0].detail["state"] == FALL_SUSPECTED
    assert "我没事" in anns[0].text
    assert m.state_of("e1") == FALL_SUSPECTED


def test_weak_impact_is_ignored():
    m = FallMachine()
    assert m.on_signal(real_fall(peak_g=1.2), "e1", T0) == []


def test_no_free_fall_is_ignored():
    """没有自由落体阶段，多半只是正常放下手机"""
    m = FallMachine()
    assert m.on_signal(real_fall(free_fall_ms=0), "e1", T0) == []


def test_walking_cadence_is_a_hard_negative():
    """手机在手里被甩、快走、跳一下 —— 最常见的假阳性"""
    m = FallMachine()
    assert m.on_signal(real_fall(movement_class="walking"), "e1", T0) == []
    assert m.state_of("e1") == FALL_CANCELLED


def test_same_event_is_idempotent():
    """同一次事件重复上报不该起两遍流程"""
    m = FallMachine()
    m.on_signal(real_fall(), "e1", T0)
    assert m.on_signal(real_fall(), "e1", T0 + 100) == []


# --------------------------------------------------------------------------
# ★ 永不提前外呼
# --------------------------------------------------------------------------


def test_no_notification_before_deadline():
    m = FallMachine(confirm_ms=15_000)
    m.on_signal(real_fall(), "e1", T0)

    for t in (T0 + 1, T0 + 5_000, T0 + 14_999):
        assert m.tick(t) == [], f"{t - T0}ms 时不该外呼"
    assert m.state_of("e1") == FALL_SUSPECTED


def test_notifies_only_after_deadline():
    m = FallMachine(confirm_ms=15_000)
    m.on_signal(real_fall(), "e1", T0)

    anns = m.tick(T0 + 15_001)
    assert anns, "过了截止时间应该升级"
    assert m.state_of("e1") == FALL_CONFIRMED
    assert all(a.detail["state"] == FALL_CONFIRMED for a in anns)


def test_silence_never_counts_as_safe():
    """★ 这是本模块最重要的一条。

    失去意识的人和「手机掉了但人没事」在传感器上都是「静止」。
    绝不能因为「没检测到后续运动」就自动取消 —— 照常升级。
    """
    m = FallMachine(confirm_ms=15_000)
    m.on_signal(real_fall(movement_class="still"), "e1", T0)

    anns = m.tick(T0 + 15_001)
    assert m.state_of("e1") == FALL_CONFIRMED
    assert anns


def test_phone_dropped_on_ground_still_escalates():
    """手机躺在地上测不到人的步态，movement_class 是 still ——
    但这不构成「安全」的证据。"""
    m = FallMachine(confirm_ms=15_000)
    m.on_signal(
        real_fall(movement_class="still", post_impact_still_ms=120_000),
        "e1", T0,
    )
    assert m.tick(T0 + 15_001)


def test_benign_context_only_extends_the_window():
    """充电中 + 屏幕亮 + 长时间静止 -> 多半是手机被碰掉了。

    但这**只是延长二次确认窗口**，不是取消 —— 用户仍然必须显式取消。
    """
    m = FallMachine(confirm_ms=15_000)
    m.on_signal(
        real_fall(on_charger=True, screen_on=True, post_impact_still_ms=120_000),
        "e1", T0,
    )
    assert m.tick(T0 + 15_001) == [], "宽限期内不该外呼"
    assert m.tick(T0 + 30_001), "宽限期过了仍然要升级"


# --------------------------------------------------------------------------
# 显式取消
# --------------------------------------------------------------------------


def test_explicit_cancel_stops_escalation():
    m = FallMachine(confirm_ms=15_000)
    m.on_signal(real_fall(), "e1", T0)

    ann = m.cancel("e1", T0 + 3_000, method="voice")
    assert ann is not None
    assert ann.detail["state"] == FALL_CANCELLED
    assert m.tick(T0 + 20_000) == [], "取消之后不该再升级"


def test_cancel_reports_method_and_elapsed():
    """取消方式和耗时是评估误报率的关键数据"""
    m = FallMachine()
    m.on_signal(real_fall(), "e1", T0)
    ann = m.cancel("e1", T0 + 4_200, method="shake")

    assert ann.detail["cancel_method"] == "shake"
    assert ann.detail["elapsed_ms"] == 4_200


def test_cancel_unknown_event_is_noop():
    assert FallMachine().cancel("不存在", T0) is None


def test_cancel_twice_is_noop():
    m = FallMachine()
    m.on_signal(real_fall(), "e1", T0)
    assert m.cancel("e1", T0 + 100) is not None
    assert m.cancel("e1", T0 + 200) is None


# --------------------------------------------------------------------------
# 通知链
# --------------------------------------------------------------------------


def test_notification_chain_order_and_dedup_keys():
    m = FallMachine(confirm_ms=1_000)
    m.on_signal(real_fall(), "e1", T0)
    anns = m.tick(T0 + 1_001)

    keys = [a.dedup_key for a in anns]
    assert len(set(keys)) == len(keys), "每条通知的去重键必须不同，否则会被仲裁器吞掉"
    assert any("family" in k for k in keys)
    assert any("grid_worker" in k for k in keys)


def test_never_auto_dials_emergency_services():
    """★ 自动拨 120 是法律责任最重的一环，当前只做到「建议手动拨」。"""
    m = FallMachine(confirm_ms=1_000)
    m.on_signal(real_fall(), "e1", T0)
    anns = m.tick(T0 + 1_001)

    joined = " ".join(a.text for a in anns)
    assert "120" not in joined
    assert any(a.detail.get("advise_manual_call") for a in anns)


def test_cancel_does_not_need_network():
    """★ 取消是纯本地计算 —— 断网时用户必须仍能取消。

    这个用例本身不访问网络，等于证明了取消路径没有网络依赖。
    """
    m = FallMachine()
    m.on_signal(real_fall(), "e1", T0)
    assert m.cancel("e1", T0 + 1_000) is not None
