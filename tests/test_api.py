"""接口测试 —— 用 TestClient 直接打路由，不起服务器。"""

import base64
import json

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
    assert len(arb.sent) == 1
    assert arb.sent[0].source == "safety"


def test_clear_path_produces_no_announcement(client, arb):
    """没障碍就不出声 —— 但要和「系统哑了」区分开，靠 /v1/health 通告"""
    r = client.post("/v1/safety/analyze",
                    json={"frame_id": "f0", "ts": 1_758_326_400_000, "extra": {"index": 0}})
    assert r.json()["announcements"] == []
    assert arb.sent == []


def test_repeated_perception_frame_is_deduped(client, arb):
    """同一帧重复打不该反复播"""
    body = {"frame_id": "f1", "ts": 1_758_326_400_000}
    client.post("/v1/perception/describe", json=body)
    client.post("/v1/perception/describe", json=body)
    assert len(arb.sent) == 1, "第二条应被去重丢掉"


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


# --------------------------------------------------------------------------
# POST /v1/frame —— 统一帧入口（multipart 上传）
# --------------------------------------------------------------------------

#: 1×1 的真 PNG。mock 并不看内容，但用合法图像能让「将来换成真检测模型
#: 会校验格式」这件事现在就不炸。不读 data/ 下的文件 —— 那些被 .gitignore
#: 了，fresh clone 根本没有。
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _upload(client, source=None, extra='{"index": 1}', **form):
    """按调试台的方式发一帧：multipart，图像放 image 字段。"""
    data = dict(form)
    if source is not None:
        data["source"] = source
    if extra is not None:
        data["extra"] = extra
    return client.post("/v1/frame", data=data,
                       files={"image": ("frame.png", PNG_1PX, "image/png")})


@pytest.mark.parametrize("source", ["perception", "safety"])
def test_upload_returns_announcement(client, source):
    r = _upload(client, source=source)
    assert r.status_code == 200

    body = r.json()
    assert body["frame"]["source"] == source
    assert len(body["announcements"]) >= 1


@pytest.mark.parametrize("source", ["perception", "safety"])
def test_upload_outputs_same_envelope(client, source):
    """★ 上传路由的信封必须和 JSON 路由逐字一致 —— 端侧只认这一种形状"""
    a = _upload(client, source=source).json()["announcements"][0]
    assert set(a.keys()) == {
        "text", "ttl_ms", "dedup_key", "source", "priority",
        "id", "ts", "interrupt", "haptic", "frame_id", "detail",
    }
    assert "kind" in a["detail"]


def test_upload_defaults_to_perception(client):
    assert _upload(client, source=None).json()["frame"]["source"] == "perception"


def test_upload_extra_reaches_the_layer(client):
    """extra 是 JSON 字符串传的，得真的解出来喂给规则层。

    index=0 是「前方无障碍」那一帧 —— 能返回空就证明 index 传到了。
    """
    r = _upload(client, source="safety", extra='{"index": 0}')
    assert r.json()["announcements"] == []


def test_upload_frame_id_and_ts_are_echoed(client):
    r = _upload(client, frame_id="up1", ts="1758326400000")
    frame = r.json()["frame"]
    assert frame["frame_id"] == "up1"
    assert frame["ts"] == 1_758_326_400_000, "ts 从表单字符串转成 int"


def test_upload_broadcasts_to_ws(client):
    """★ 上传的帧产生的播报也要走 WS —— 端侧只有一个播报入口"""
    with client.websocket_connect("/v1/stream") as ws:
        assert ws.receive_json()["type"] == "hello"

        _upload(client, source="safety")

        msg = ws.receive_json()
        assert msg["type"] == "announcement"
        assert msg["data"]["source"] == "safety"


def test_upload_reaches_the_arbiter(client, arb):
    _upload(client, source="safety")
    assert len(arb.sent) == 1
    assert arb.sent[0].source == "safety"


def test_upload_without_image_is_rejected(client):
    r = client.post("/v1/frame", data={"source": "perception"})
    assert r.status_code == 400
    assert "image" in r.json()["error"]


def test_far_future_tick_does_not_wedge_the_gate(client, arb):
    """★ 回归：播报流「播了一会儿就永久静默」。

    `smoke.sh` 和调试台的「推进时钟」按钮都发 `now_ms=9999999999999`
    （公元 2286 年）来强行触发升级链。旧实现会把这个未来时刻记成
    「当前播报的开始时间」，导致之后所有真实时间戳都成了「过去」、
    永远退不了场 —— 队列涨满，播报流永久静默。

    现在服务端不再跟踪播放状态，这个失效模式从结构上就不存在了；
    用例留着防止有人再把「当前在播什么」搬回服务端。
    """
    T0 = 1_758_326_400_000

    # 按 smoke.sh 的顺序来：先有播报 → 一键求助 → 推进时钟
    _upload(client, source="safety", ts=str(T0), frame_id="f1")
    client.post("/v1/emergency/sos",
                json={"frame_id": "so1", "ts": T0, "extra": {"index": 1}})
    client.post("/v1/emergency/tick", json={"now_ms": 9_999_999_999_999})

    before = len(arb.sent)
    # 之后用真实量级的时间戳继续喂帧 —— 必须照常发出
    for i in range(15):
        _upload(client, source="safety", ts=str(T0 + 10_000 + i * 1200),
                frame_id=f"n{i}", extra=json.dumps({"index": i}))

    assert len(arb.sent) > before, "未来时钟之后播报流不再恢复"


def test_upload_with_image_as_plain_field_is_rejected(client):
    """image 必须是文件字段，不能是普通文本字段"""
    r = client.post("/v1/frame", data={"image": "not-a-file"})
    assert r.status_code == 400


def test_upload_rejects_unknown_source(client):
    r = _upload(client, source="navigation")
    assert r.status_code == 400
    assert "source" in r.json()["error"]


def test_upload_rejects_malformed_extra(client):
    r = _upload(client, extra="not-json")
    assert r.status_code == 400
    assert "extra" in r.json()["error"]


def test_upload_rejects_non_object_extra(client):
    r = _upload(client, extra="[1,2,3]")
    assert r.status_code == 400


def test_upload_rejects_empty_image(client):
    r = client.post("/v1/frame", data={"source": "perception"},
                    files={"image": ("empty.png", b"", "image/png")})
    assert r.status_code == 400


def test_upload_leaves_no_temp_file(client):
    """★ 临时帧必须在响应返回前删掉，否则跑一天就把磁盘塞满了"""
    from app.main import UPLOAD_DIR

    _upload(client, source="perception")
    _upload(client, source="safety")
    assert list(UPLOAD_DIR.iterdir()) == []


# --------------------------------------------------------------------------
# 前端调试台与素材托管
# --------------------------------------------------------------------------


def test_homepage_serves_the_console(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "调试台" in r.text


def test_data_mount_serves_test_assets(client):
    """<video src="/data/demo.mp4"> 靠它。素材是生成的（gitignore），
    没有就跳过 —— 别让 fresh clone 因为这个用例挂掉。"""
    from app.main import DATA_DIR

    if not (DATA_DIR / "demo.mp4").is_file():
        pytest.skip("还没生成测试素材：bash scripts/make_test_video.sh")
    assert client.get("/data/demo.mp4").status_code == 200

