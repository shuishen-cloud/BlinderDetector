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
        pytest.skip("还没生成测试素材：python scripts/make_test_video.py")
    assert client.get("/data/demo.mp4").status_code == 200


# --------------------------------------------------------------------------
# 第三层：extra 是不可信输入，坏值不许把链路打崩
# --------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [
    {"destination": "X", "max_steps": "abc"},
    {"destination": "X", "max_steps": 2.7},
    {"destination": "X", "max_steps": None},
    {"destination": "X", "avoid": 5},
    {"destination": "X", "avoid": {"overpass": True}},
    {"destination": "X", "avoid": [1, 2, None]},
    {"destination": "X", "geo": "not-a-dict"},
    {"destination": "X", "destination_geo": [1, 2]},
    {"destination": "X", "geo": {"lat": "abc", "lng": 1}},
])
def test_bad_extra_values_never_500(client, extra):
    """★ `extra` 是**任意 JSON**，不能信。

    实测过：`max_steps` 传字符串、`avoid` 传数字都会让 `list[:n]` /
    `in` 抛 TypeError 冒成 500 —— 客户端手滑一次，整条播报链路就没了。
    这里所有值都该被收敛成合法默认值，而不是崩掉。
    """
    r = client.post("/v1/navigation/route",
                    json={"frame_id": "f", "ts": 1, "extra": extra})
    assert r.status_code == 200


def test_empty_avoid_is_honoured_not_replaced_by_default(client):
    """`avoid: []` 是合法意图（这次不避开任何东西），不能被默认值顶掉。"""
    r = client.post("/v1/navigation/route", json={
        "frame_id": "f", "ts": 1, "extra": {"destination": "X", "avoid": []}})
    detail = [a["detail"] for a in r.json()["announcements"]
              if a["detail"].get("kind") == "route"][0]
    assert detail["warnings"] == []


def test_unknown_router_boots_and_degrades_honestly(monkeypatch):
    """★ `ROUTER` 写错名字不能让服务起不来。

    `registry.get()` 抛的 KeyError 发生在 `create_app()` 的图层构造期 ——
    不兜住的话整个 uvicorn 直接退出，连 `/v1/health` 都打不开。
    这与本项目「配置不对也要降级并如实播报」的立身之本相反。
    """
    from app import config

    monkeypatch.setattr(config, "ROUTER", "no-such-router")
    with TestClient(create_app()) as c:
        r = c.post("/v1/navigation/route",
                   json={"frame_id": "f", "ts": 1, "extra": {"destination": "X"}})
        assert r.status_code == 200
        texts = [a["text"] for a in r.json()["announcements"]]
        assert any("演示路网" in t for t in texts), \
            "配置写错必须如实播报，不能静默地用兜底路网"


def test_health_marks_an_unregistered_router_distinctly(monkeypatch):
    """★ 名字**没注册**（拼错、或照抄 .env.example 把 `ROUTER=` 留空）
    与「服务不可用」是两件事。

    混成同一个原因码，会让人去查网络，而问题其实在配置。
    """
    from app import config

    monkeypatch.setattr(config, "ROUTER", "no-such-router")
    with TestClient(create_app()) as c:
        deg = c.get("/v1/health").json()["degraded"]
    assert any(d["reason"] == "router_not_registered" for d in deg), deg
    # 顺带把「有哪些合法取值」也报出来，省得再去翻代码
    assert any(d.get("known") for d in deg), deg


def test_misconfigured_router_does_not_blame_the_network(monkeypatch):
    """★ 配置拼错时，播报不能复用「地图服务暂时不可用」那句话 ——
    那会把排查的人引向网络，而问题在 .env。"""
    from app import config

    monkeypatch.setattr(config, "ROUTER", "no-such-router")
    with TestClient(create_app()) as c:
        r = c.post("/v1/navigation/route",
                   json={"frame_id": "f", "ts": 1, "extra": {"destination": "X"}})
        texts = [a["text"] for a in r.json()["announcements"]]
    assert any("配置有误" in t for t in texts), texts
    assert not any("暂时不可用" in t for t in texts), texts


def test_blank_router_falls_back_to_builtin(monkeypatch):
    """`ROUTER=` 留空（.env.example 里 AK/FIXTURE 都是留空的，很容易照抄）
    不该抛异常，直接用兜底实现。"""
    from app import config

    monkeypatch.setattr(config, "ROUTER", "")
    with TestClient(create_app()) as c:
        assert c.get("/v1/health").status_code == 200


# --------------------------------------------------------------------------
# 前端配置与地图面板
# --------------------------------------------------------------------------


def test_frontend_config_exposes_the_browser_ak(client, monkeypatch):
    """★ 浏览器端 AK 走这个接口下发，**不能写进 `web/` 里的文件** ——
    那是静态托管目录，写死等于提交进仓库。每个人的 AK 不同。"""
    from app import config

    monkeypatch.setattr(config, "BAIDU_BROWSER_AK", "test-browser-ak")
    r = client.get("/v1/frontend-config")
    assert r.status_code == 200
    assert r.json()["baidu_browser_ak"] == "test-browser-ak"


def test_frontend_config_never_leaks_the_server_side_ak(client):
    """★ 服务端 AK 是能真花钱的凭据，绝不能顺手下发给浏览器。

    本机没配时这条空转；只要配了就一定会查 —— 而开发机通常配着，
    所以它确实挡得住「不小心把两个 AK 混在一起下发」这种错。
    """
    from app import config

    if not config.BAIDU_AK:
        pytest.skip("本机没配服务端 AK")
    assert config.BAIDU_AK not in client.get("/v1/frontend-config").text


def test_map_panel_is_served(client):
    """`web/` 下新增的文件由 `/static` 自动托管（不用改路由表）。

    仓库里没有别的测试覆盖它 —— 文件被误删、路径写错都不会有人发现，
    所以在这里钉一条。
    """
    r = client.get("/static/map.js")
    assert r.status_code == 200
    assert "LingmouMap" in r.text, "map.js 必须导出 window.LingmouMap 给 app.js 调"


def test_homepage_includes_the_map_panel(client):
    html = client.get("/").text
    assert "/static/map.js" in html
    assert 'id="map"' in html, "百度 JSAPI 需要这个容器"
    assert 'id="map-msg"' in html
    # ★ 顺序要紧：map.js 只注册入口，app.js 加载时就 connect()，所以 map.js 在后
    assert html.index("/static/app.js") < html.index("/static/map.js")

