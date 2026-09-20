"""接口测试 —— 用 TestClient 直接打路由，不起服务器。"""

import pytest
from starlette.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client():
    """每个用例一个全新的 app。

    ★ 不能用模块级的 `app` 单例：arbiter / hub / 各层实例都在 create_app()
      时创建并复用，里面持有状态（去重表、跌倒事件、求助幂等键）。
      共享实例会让用例互相污染 —— 第一个用例建立的 fall 事件会让
      第二个用例幂等返回空。
    """
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def arb(client):
    """这个 app 实例自己的仲裁器。"""
    return client.app.state.arbiter


# --------------------------------------------------------------------------
# 形状
# --------------------------------------------------------------------------

ROUTES = [
    ("/v1/perception/describe", "perception"),
    ("/v1/safety/analyze", "safety"),
    ("/v1/safety/fall", "emergency"),
    ("/v1/navigation/route", "navigation"),
    ("/v1/emergency/sos", "emergency"),
]

# index=1 是「前方有台阶」那一帧，保证每个路由都有播报产出。
# index=0 是无障碍场景，安全层会返回空列表 —— 那条路径单独测。
BODY = {"frame_id": "f0001", "ts": 1_758_326_400_000, "extra": {"index": 1}}


@pytest.mark.parametrize("path,source", ROUTES)
def test_route_accepts_frame_returns_announcement(client, path, source):
    r = client.post(path, json=BODY)
    assert r.status_code == 200

    body = r.json()
    assert body["frame"]["frame_id"] == "f0001"
    assert body["frame"]["source"] == source
    assert len(body["announcements"]) >= 1

    ann = body["announcements"][0]
    for f in ("id", "ts", "source", "priority", "text", "ttl_ms", "dedup_key", "detail"):
        assert f in ann, f"Announcement 缺字段 {f}"


@pytest.mark.parametrize("path,source", ROUTES)
def test_every_route_outputs_same_envelope(client, path, source):
    """★ 全部路由的出参形状一致 —— 这是「只有 2 个信封」的直接体现"""
    a = client.post(path, json=BODY).json()["announcements"][0]
    assert set(a.keys()) == {
        "text", "ttl_ms", "dedup_key", "source", "priority",
        "id", "ts", "interrupt", "haptic", "frame_id", "detail",
    }
    assert "kind" in a["detail"]


def test_frame_id_is_echoed_back(client):
    r = client.post("/v1/perception/describe", json={"frame_id": "my-frame-42", "ts": 1})
    assert r.json()["announcements"][0]["frame_id"] == "my-frame-42"


def test_empty_body_still_works(client):
    """组员用 curl 随手打一下不该 500"""
    assert client.post("/v1/safety/analyze", json={}).status_code == 200


def test_bad_json_does_not_500(client):
    r = client.post("/v1/perception/describe", content=b"not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200


# --------------------------------------------------------------------------
# 健康检查
# --------------------------------------------------------------------------


def test_health_reports_impls(client):
    body = client.get("/v1/health").json()
    assert body["ok"] is True
    assert "mock" in body["impls"]["vlm"]
    assert "dashscope" in body["impls"]["vlm"], "换了 .env 就能切厂商"
    assert set(body["impls"]["layer"]) == {"perception", "safety", "navigation", "emergency"}


def test_health_lists_framesource_impls(client):
    assert set(client.get("/v1/health").json()["impls"]["framesource"]) == {"images", "video"}


# --------------------------------------------------------------------------
# 仲裁器接进了路由
# --------------------------------------------------------------------------


def test_safety_alert_reaches_arbiter(client, arb):
    """index=1 是「前方台阶」那一帧，会产生一条预警"""
    client.post("/v1/safety/analyze",
                json={"frame_id": "f1", "ts": 1_758_326_400_000, "extra": {"index": 1}})
    assert len(arb.spoken) == 1
    assert arb.spoken[0].source == "safety"


def test_clear_path_produces_no_announcement(client, arb):
    """没障碍就不出声 —— 但要和「系统哑了」区分开，靠 /v1/health 通告"""
    r = client.post("/v1/safety/analyze",
                    json={"frame_id": "f0", "ts": 1_758_326_400_000, "extra": {"index": 0}})
    assert r.json()["announcements"] == []
    assert arb.spoken == []


def test_repeated_perception_frame_is_deduped(client, arb):
    """同一帧重复打不该反复播"""
    body = {"frame_id": "f1", "ts": 1_758_326_400_000}
    client.post("/v1/perception/describe", json=body)
    client.post("/v1/perception/describe", json=body)
    assert len(arb.spoken) == 1, "第二条应被去重丢掉"


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------


def test_ws_receives_broadcast(client):
    """★ 这里必须用一定产出播报的帧（index=1），否则 receive_json 会永久阻塞"""
    with client.websocket_connect("/v1/stream") as ws:
        assert ws.receive_json()["type"] == "hello"

        client.post("/v1/safety/analyze",
                    json={"frame_id": "f9", "ts": 1_758_326_400_000, "extra": {"index": 1}})

        msg = ws.receive_json()
        assert msg["type"] == "announcement"
        assert msg["data"]["source"] == "safety"


def test_ws_ping_pong(client):
    with client.websocket_connect("/v1/stream") as ws:
        ws.receive_json()
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
