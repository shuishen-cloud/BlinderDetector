"""第四层求助状态机。"""

from app.contracts import TRIGGER_SCREEN_LONG
from app.core.rules.sos import SosMachine

T0 = 1_000_000


def arm(**kw):
    m = SosMachine(ack_timeout_ms=30_000)
    anns = m.trigger(
        "s1",
        trigger=kw.pop("trigger", TRIGGER_SCREEN_LONG),
        now_ms=kw.pop("now_ms", T0),
        **kw,
    )
    return m, anns


# --------------------------------------------------------------------------
# 触发
# --------------------------------------------------------------------------


def test_trigger_notifies_family_first():
    m, anns = arm()
    assert len(anns) == 1
    assert anns[0].detail["contact_scope"] == "family"
    assert "家人" in anns[0].text
    assert m.is_active("s1")


def test_idempotency_key_prevents_double_call():
    """★ 网络重试、用户连按三次，都不能变成三个电话打给家属。"""
    m = SosMachine()
    m.trigger("s1", trigger=TRIGGER_SCREEN_LONG, now_ms=T0, idempotency_key="k1")
    again = m.trigger("s2", trigger=TRIGGER_SCREEN_LONG, now_ms=T0 + 100,
                      idempotency_key="k1")

    assert len(again) == 1
    assert again[0].detail["duplicate"] is True
    assert "家人" not in "".join(a.text for a in again if "通知" in a.text)


def test_different_keys_are_independent():
    m = SosMachine()
    m.trigger("s1", trigger=TRIGGER_SCREEN_LONG, now_ms=T0, idempotency_key="k1")
    anns = m.trigger("s2", trigger=TRIGGER_SCREEN_LONG, now_ms=T0, idempotency_key="k2")
    assert anns[0].detail.get("duplicate") is not True


# --------------------------------------------------------------------------
# 升级
# --------------------------------------------------------------------------


def test_no_escalation_before_timeout():
    m, _ = arm()
    assert m.tick(T0 + 1_000) == []
    assert m.tick(T0 + 29_999) == []


def test_escalates_to_grid_worker_after_timeout():
    m, _ = arm()
    anns = m.tick(T0 + 30_001)
    assert anns
    assert anns[0].detail["contact_scope"] == "grid_worker"


def test_never_auto_dials_emergency_services():
    """★ 自动拨 120 是法律责任最重的一环 —— 只建议手动拨，不自动拨。"""
    m, _ = arm()
    anns = m.tick(T0 + 30_001)
    assert not any("120" in a.text for a in anns)
    assert any(a.detail.get("advise_manual_call") for a in anns)


def test_chain_stops_at_the_end():
    m, _ = arm()
    m.tick(T0 + 30_001)
    assert m.tick(T0 + 90_000) == [], "链条走完就停，等人工介入"


# --------------------------------------------------------------------------
# 应答与取消
# --------------------------------------------------------------------------


def test_acknowledge_stops_escalation():
    m, _ = arm()
    ann = m.acknowledge("s1", T0 + 5_000)
    assert ann is not None
    assert "已收到" in ann.text
    assert m.tick(T0 + 60_000) == []


def test_cancel_resolves_the_event():
    m, _ = arm()
    assert m.cancel("s1") is True
    assert m.is_active("s1") is False
    assert m.cancel("s1") is False


def test_acknowledge_unknown_event_is_noop():
    assert SosMachine().acknowledge("不存在", T0) is None


# --------------------------------------------------------------------------
# 去重键
# --------------------------------------------------------------------------


def test_notification_keys_are_distinct():
    """★ 「通知家属」和「通知网格员」的去重键必须不同，
    否则第二条会被仲裁器当成重复吞掉，网格员永远收不到。"""
    m, first = arm()
    second = m.tick(T0 + 30_001)
    keys = {a.dedup_key for a in first + second}
    assert len(keys) == len(first) + len(second)


# --------------------------------------------------------------------------
# 诚实性
# --------------------------------------------------------------------------


def test_wording_never_claims_delivery_happened():
    """★ 项目里还没有任何真实发送通道（无 SMTP、无 webhook）。

    同 test_fall：在用户最危险的时刻断言「已通知」，他会停止自救、
    原地等一个不会来的人。文案说到「正在通知」为止。
    见 问题-待处理的一些遗留问题.md §1.5。
    """
    m, first = arm()
    anns = first + m.tick(T0 + 30_001)

    joined = " ".join(a.text for a in anns)
    assert "已通知" not in joined, "还没有通道，不能说「已通知」"
    assert "正在通知" in joined
