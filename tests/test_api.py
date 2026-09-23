"""接口测试 —— 用 TestClient 直接打路由，不起服务器。"""

import base64
import json
import re
from collections import Counter
from pathlib import PurePosixPath

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


def html_body(client) -> str:
    """页面 HTML，**剥掉注释**。

    ★ 为什么必须剥：测试里常要「数某种标签出现了几次」，而注释里提到
      `<details>` / `<script>` 是常事（这些代码自己就反复提到）。不剥的话
      计数会被说明文字带偏 —— 这个坑在本文件里踩过两次（数 <details>、
      数 <script>），所以统一走这里。
    """
    return re.sub(r"<!--.*?-->", "", client.get("/").text, flags=re.S)


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
# 前端页面与素材托管
# --------------------------------------------------------------------------


def test_homepage_serves_the_console(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "灵眸伴途" in r.text


def test_test_pieces_are_shipped_but_hidden_by_default(client):
    """★「把测试的部分藏起来」＝ `hidden`，**不是删掉**。

    `app.js` / `map.js` 一律按 id 取 DOM —— 元素真被删掉会当场报错，而契约
    验证（其余路由、请求日志、统计、频率旋钮）随时要能翻出来。所以钉两条：
    ① 开发者面板在页面上、且默认 hidden；② 测试件确实都关在面板里面。

    ★ 第 ② 条靠**位置**判断而不是靠 class：`hidden` 是浏览器行为，
    元素一旦漏到 `#devPanel` 外面（比如以后有人挪错一行），手机视图上
    就会冒出调试按钮，而上面那条 `hidden` 照样是绿的。

    ★★ 例外：`video` / `loopBtn` **有意放在手机视图里**（2026-09-22 调整）。
       理由：手机视图默认没有任何帧源，于是第一层（感知）和第二层（安全）
       永远不会触发 —— 打开页面看到的是一个播报流永远空着的「交付形态」。
       视频对盲人用户没用，所以做成一张小卡片（`.video-mini`，84px 高），
       作用是「喂帧」而不是给人看；频率旋钮仍是调试参数，留在面板里。

       这条改动**推翻了本用例原先的设计意图**（原作者用 `loopBtn` 当反例），
       故在此显式记录，免得看起来像谁挪错了行。
    """
    html = client.get("/").text

    assert re.search(r'id="devPanel"[^>]*\shidden', html), "开发者面板必须默认隐藏"

    panel = html.index('id="devPanel"')
    for el in ("perMs", "dropzone", "logTail", "stFrames"):
        assert html.index(f'id="{el}"') > panel, f"{el} 是测试件，应该关在开发者面板里"

    # 手机视图得留着产品功能：播报开关、播报流、导航、一键求助
    phone = html.index('id="app"')
    for el in ("ttsBtn", "feed", "dest", "geoBtn"):
        assert html.index(f'id="{el}"') > phone, f"{el} 是产品功能，不该被藏起来"
    assert "/v1/emergency/sos" in html, "一键求助必须留在手机视图里"


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


def test_tests_never_use_the_configured_real_router(client):
    """★ 测试必须**离线且确定**，不能取决于开发机的 `.env`。

    开发机常配着 `ROUTER=baidu`（要看调试台的真实路线）。少了这条，
    `/v1/navigation/route` 的用例就会真的去打百度接口 —— 结果取决于
    网络和配额，而且**照样会绿**（拿不到路线时图层会如实降级回内置路网），
    所以是个没人会发现的假绿。钉死见 `tests/conftest.py`。
    """
    layer = client.app.state.layers["navigation"]
    assert layer.router_name == "builtin", \
        "用例里的导航层必须是内置路网 —— 别让测试去打真实地图接口"


def test_map_panel_is_served(client):
    """`web/` 下新增的文件由 `/static` 自动托管（不用改路由表）。

    仓库里没有别的测试覆盖它 —— 文件被误删、路径写错都不会有人发现，
    所以在这里钉一条。
    """
    r = client.get("/static/map.js")
    assert r.status_code == 200
    assert "LingmouMap" in r.text, "map.js 必须导出 window.LingmouMap 给 app.js 调"


def test_homepage_includes_the_map_panel(client):
    """地图面板必须在页面上，且 map.js 必须真的被加载。

    ★ 2026-09-23 前端模块化之后，map.js 不再是独立的 <script> ——
      它由 main.js import 进来（加载顺序交给模块图，不再依赖"谁写在前面"）。
      所以这里断的是「被 main.js 引到」，而不是「页面里有那个标签」。
    """
    html = client.get("/").text
    assert 'id="map"' in html, "百度 JSAPI 需要这个容器"
    assert 'id="map-msg"' in html

    main = client.get("/static/js/main.js").text
    assert 'from "../map.js"' in main, "map.js 必须被入口 import"
    assert client.get("/static/map.js").status_code == 200, "map.js 要能被取到"



def test_phone_view_has_a_frame_source(client):
    """★ 手机视图必须自己能喂帧，否则第一、二层永远不会触发。

    没有帧源 = 播报流永远空着 —— 交付形态看起来就是个坏掉的应用。
    视频做小（84px 一条）是因为盲人用户看不到画面，但它必须在。
    """
    html = client.get("/").text
    phone = html.index('id="app"')
    panel = html.index('id="devPanel"')

    for el in ("video", "loopBtn"):
        pos = html.index(f'id="{el}"')
        assert phone < pos < panel, f"{el} 应该在手机视图里（帧源），不该只在开发者面板"
    assert "/data/demo.mp4" in html, "帧源要指向 demo.mp4"


def test_frame_source_has_visible_status(client):
    """★ 帧源必须有可见状态，不能静默失效。

    `grabAndSend()` 在视频没就绪时会直接 return —— 按钮已经变成「停止发帧」，
    页面看起来正常，实际一帧都没发出去，和后端坏了长得一模一样。
    实测（worktree 里缺 demo.mp4）：`POST /v1/frame` 收到 0 次，
    而请求日志在开发者面板里、默认看不见，所以手机视图必须自己说出来。

    这条用例钉住「反馈元素存在且和帧源在同一张卡片里」——
    以后谁把它删了，这里会红。
    """
    html = client.get("/").text
    phone, panel = html.index('id="app"'), html.index('id="devPanel"')

    assert 'id="frameMsg"' in html, "帧源卡片必须有状态反馈元素"
    pos = html.index('id="frameMsg"')
    assert phone < pos < panel, "状态反馈要在手机视图的帧源卡片里，不能只在开发者面板"
    # 和帧源在同一张卡片内（在 video 之后、卡片结束之前）
    assert html.index('id="video"') < pos, "状态行应跟在视频之后"


def test_emergency_is_dismissible_by_tapping_anywhere(client):
    """★ 紧急全屏必须能点**任意位置**关掉，而且要把这件事**说出来**。

    看不见屏幕的用户很难命中一个小按钮。`#emg` 铺满全屏（position:fixed;
    inset:0）且绑了 onclick，所以点黑暗区域任何位置都能关 —— 但空黑区域
    看起来不可点，没人会去试。所以本用例同时钉两件事：
    ① JS 里那个绑定还在（少了它就只剩一个孤零零的小按钮）；
    ② 页面上写明了这件事。
    """
    html = client.get("/").text
    assert 'id="emg"' in html and 'id="emgOk"' in html

    pos = html.index('id="emg"')
    assert "任意位置" in html[pos:pos + 900], "紧急层必须写明「点任意位置也能关闭」"

    # ★ 2026-09-23 模块化后这段逻辑在 js/ui.js（产品界面），不在 app.js
    js = client.get("/static/js/ui.js").text
    assert 'emg").onclick' in js, "遮罩本身必须绑关闭，否则只能点那个小按钮"


def test_phone_feed_has_fixed_height_and_scrolls(client):
    """★ 手机视图的播报流是**定长 + 内滚**。

    定长：播报每 600ms 一条、只增不减，框子跟着内容长会让人不断失去
    位置感。（中间试过 34–56vh 的自适应区间，最后定回固定值 —— 可预期
    比"刚好塞满"重要。）
    内滚：超出就在框内滚；overscroll-behavior 防止滚到底把整页一起带走。
    """
    css = client.get("/static/app.css").text
    i = css.index(".phone .feed")
    block = css[i:i + css[i:].index("}")]

    assert re.search(r"\bheight:\s*\d", block), \
        "必须是固定高度（height），不能随内容长"
    assert "overflow-y: auto" in block, "播报流必须能上下滑动查看"
    assert "overscroll-behavior: contain" in block, "内滚到底不该把整页也带着滚"


def test_video_strip_has_fixed_width(client):
    """帧源条：宽度**固定**，且必须在手机视图里。

    ★ 「放最上方」这条要求已被取代（2026-09-23）：导航与播报改为占据
      顶部，帧源条随之下移。所以位置断言改成「在手机视图内」而不是
      「在最上方」—— 它仍不该被收进开发者面板（那样手机视图就没有帧源，
      第一二层永远不会触发，见 test_phone_view_has_a_frame_source）。

    宽度仍然必须固定：盲人用户看不到画面，它不该随屏幕自适应挤占播报流。
    """
    html = client.get("/").text
    strip = html.index('class="card video-strip"')
    assert html.index('id="app"') < strip < html.index('id="devPanel"'), \
        "帧源条要在手机视图里"

    css = client.get("/static/app.css").text
    blk = css[css.index(".strip-video {"):]
    blk = blk[:blk.index("}")]
    assert "132px" in blk, "视频要固定宽度，不能自适应"


def test_map_fold_does_not_use_details_tag(client):
    """★ 地图默认收起，但**不能用 `<details>` 折叠**。

    地图容器必须有显式的布局高度，否则百度 GL **静默不渲染** —— 既不抛
    异常也不报错（app.css 的 .map-stage 注释记着这个坑）。而 `<details>`
    关闭时内容是 `display:none`：容器高度为 0，正好命中它。

    所以折叠必须走 `max-height`。这条用例钉的是「以后谁把它改回
    `<details>`，会红」—— 否则地图会不声不响地白掉，很难查。
    """
    html = client.get("/").text
    assert 'data-fold' in html, "折叠开关要有 data-fold（app.js 靠它绑事件）"

    # 地图不能在未闭合的 <details> 里（剥注释，见 html_body 的说明）
    body = html_body(client)
    before = body[:body.index('id="map"')]
    assert before.count("<details") == before.count("</details>"), \
        "地图不能在 <details> 里 —— display:none 会让 GL 静默不渲染"

    css = client.get("/static/app.css").text
    blk = css[css.index(".fold-body {"):]
    blk = blk[:blk.index("}")]
    assert "max-height" in blk and "overflow: hidden" in blk, \
        "折叠要用 max-height + overflow，不能 display:none"
    # 展开后要能容下地图（220px）+ 说明文字
    assert "max-height: 0" in blk, "默认应该是收起的"


def test_sos_buttons_live_in_the_top_dock(client):
    """★ 一键求助 / 取消求助 在**顶部常驻区**（2026-09-23 要求「一直都在最顶端」）。

    从底部栏挪上来的。同时钉住 `.topdock` 统一 sticky：品牌栏和操作条
    各自 `top: 0` 会同时贴住视口顶部、滚动时互相盖住。
    """
    html = client.get("/").text
    phone, dock = html.index('id="app"'), html.index('class="topdock"')
    feed = html.index('class="card feed-card"')
    sos, cancel = (html.index('data-route="/v1/emergency/sos"'),
                   html.index('data-route="/v1/emergency/cancel"'))

    assert phone < dock < feed, "顶部常驻区要在播报流之前"
    for name, pos in (("一键求助", sos), ("取消求助", cancel)):
        assert dock < pos < feed, f"{name} 要在顶部常驻区里"

    css = client.get("/static/app.css").text
    blk = css[css.index(".topdock {"):]
    blk = blk[:blk.index("}")]
    assert "sticky" in blk, ".topdock 必须 sticky（常驻）"
    assert ".topdock .topbar { position: static; }" in css, \
        "里面的 .topbar 要改 static，否则两个 sticky 互相盖住"


# --------------------------------------------------------------------------
# 前端模块化（2026-09-23）—— 下面两条是这次重构真正的安全网
# --------------------------------------------------------------------------

JS_FILES = ["/static/js/dom.js", "/static/js/state.js", "/static/js/log.js",
            "/static/js/net.js", "/static/js/ui.js", "/static/js/dev.js",
            "/static/js/main.js", "/static/map.js"]


def test_every_id_the_js_reaches_for_exists_in_the_html(client):
    """★ JS 引用的每个 id 都必须在页面上存在。

    这是前端拆模块之后最要紧的一条：所有模块一律按 id 取 DOM，重构时把
    一个 `$("xxx")` 搬到了别的文件、而那个元素根本不在页面上 ——
    **只有真去点那个按钮才会报错**，而单元测试看不见。
    反向同理：元素被删了而 JS 还在取，也是这里先报。

    静态检查替代不了浏览器，但这一类断裂它能全部拦住。
    """
    ids = set(re.findall(r'id="([^"]+)"', client.get("/").text))

    missing = {}
    for path in JS_FILES:
        src = client.get(path).text
        refs = set(re.findall(r'\$\("([^"]+)"\)', src))
        refs |= set(re.findall(r'getElementById\("([^"]+)"\)', src))
        gap = sorted(r for r in refs if r not in ids)
        if gap:
            missing[path] = gap

    assert not missing, f"JS 引用了页面上不存在的 id：{missing}"


def _resolve_module(base_dir: str, spec: str) -> str:
    """把模块里的相对 specifier 解析成**浏览器会请求的那个绝对路径**。

    ★ 必须自己折叠 `..`：`pathlib` **不**折叠（文档明说），而浏览器会。
      不折叠的话测试请求的是 `/static/js/../map.js` —— 服务端把它规范化
      成 `/static/map.js`，于是照样 200；而浏览器请求的是 `/static/map.js`。
      两者恰好一致时测试绿，一旦服务端不再规范化就变成**假绿**，而真实
      浏览器已经 404 了。
    """
    parts = [p for p in base_dir.split("/") if p]
    for seg in spec.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg not in ("", "."):
            parts.append(seg)
    return "/" + "/".join(parts)


def test_every_module_import_resolves(client):
    """模块 import 的路径都要真的取得到 —— 拼错在浏览器里是**静默失败**：
    整页 JS 不执行，而控制台之外看不出任何异常。"""
    for page in ("/static/js/main.js", "/static/js/dev.js", "/static/map.js"):
        base_dir = str(PurePosixPath(page).parent)
        for m in re.findall(r'from "(\.{1,2}/[^"]+)"', client.get(page).text):
            url = _resolve_module(base_dir, m)
            assert client.get(url).status_code == 200, f"{page} → {m}（{url}）取不到"


def test_module_resolver_collapses_dotdot():
    """给上面那个解析器本身一条用例 —— 它是测试的测试，错了会集体假绿。"""
    assert _resolve_module("/static/js", "../map.js") == "/static/map.js"
    assert _resolve_module("/static/js", "./net.js") == "/static/js/net.js"
    assert _resolve_module("/static", "./js/dom.js") == "/static/js/dom.js"
    assert _resolve_module("/static/js", "../../x.js") == "/x.js"


def test_only_one_script_tag_and_it_is_a_module(client):
    """★ 页面只该有一个 <script>，且必须是 ES module。

    拆模块之前是两个普通脚本，靠「谁写在前面」定顺序；现在顺序交给模块图，
    多一个普通 <script> 或漏掉 type="module" 都会让 import 语法直接报错。
    """
    html = html_body(client)          # 剥注释，否则注释里提到的 <script> 会被算进来
    tags = re.findall(r"<script[^>]*>", html)
    assert len(tags) == 1, f"只该有一个 script 标签，实际 {tags}"
    assert 'type="module"' in tags[0], "必须是 module"
    assert "/static/js/main.js" in tags[0], "入口应是 js/main.js"


def test_only_emergency_announcements_take_over_the_screen(client):
    """★ 视觉全屏只留给需要用户**动作**的播报（跌倒二次确认 / 求助）。

    安全层的危险障碍物不抢屏。按盲人使用逻辑：
      · 全屏遮罩对看不到屏幕的人毫无作用 —— 信息全在耳朵和震动里；
      · 它唯一的作用是给陪同者看，而实测 36 秒弹 8 次（全是自行车/来车），
        屏幕几乎一直被红色盖着，反而把陪同者要看的信息挡掉。
    危险障碍物靠 TTS + haptic=double 就够了。

    钉的是 ui.js 的 receive()：`emergency(...)` 必须由 source 判断守着，
    不能退回「priority>=3 一律全屏」。
    """
    js = client.get("/static/js/ui.js").text
    i = js.index("export function receive(")
    body = js[i:js.index("\n}\n", i)]

    assert "SOURCE_EMERGENCY" in body, "全屏必须按 source 收窄，不能只看 priority"
    assert body.count("emergency(") == 1, "全屏只该有一个入口"
    # 优先级统计与「抢屏」是两件事，别被合并回去
    assert "bump(\"critical\")" in body, "统计仍按 priority>=3，不该跟着收窄"


def test_action_results_are_audible_not_only_visible(client):
    """★ 动作结果不能只给 toast —— 那是视觉的，盲人用户看不到。

    按「一键求助」只弹一个视觉提示，用户不知道自己按上没有。
    所以走三条通道：toast（陪同者）+ TTS（用户）+ haptic（关播报时兜底）。
    """
    js = client.get("/static/js/dev.js").text
    assert "function confirmAction(" in js, "要有统一的动作确认入口"
    i = js.index("function confirmAction(")
    body = js[i:js.index("\n}\n", i)]
    for ch in ("toast(", "speak(", "haptic("):
        assert ch in body, f"动作确认缺 {ch} 这条通道"
    assert "toast(routeToast(" not in js, "路由按钮不该退回只弹 toast"


def test_html_ids_are_unique(client):
    """★ id 必须唯一 —— 重复 id 会让 `getElementById` 只返回**第一个**，
    另一个元素从此静默地永远不更新（不报错，只是不动）。

    加这条的起因：给地图加折叠区时把 `id="map-src"` 复制了一份，于是
    导航卡标题和折叠区各有一个 —— map.js 只更新得到前一个。**已有的
    「JS 引用的 id 是否存在」那条查不出来**，因为两个都存在。
    """
    ids = re.findall(r'id="([^"]+)"', html_body(client))
    dup = {k: v for k, v in Counter(ids).items() if v > 1}
    assert not dup, f"id 重复：{dup}"


def test_incident_stats_are_wired(client):
    """意外统计必须真的接进了播报流，且判据取自 detail。

    统计口径跟后端契约同源（emergency 的 state / safety 的 risk），
    所以后端改措辞不会让数字失真。
    """
    js = client.get("/static/js/incidents.js").text
    # 判据来自契约字段，不是措辞
    for field in ("d.state", "d.escalation_step", "d.risk"):
        assert field in js, f"统计判据应取自 detail 的 {field}"
    for state in ("suspected", "confirmed", "cancelled", "notifying"):
        assert state in js, f"漏了 {state} 这一态"

    html = client.get("/").text
    for el in ("incAccidents", "incWarnings", "incReset"):
        assert f'id="{el}"' in html, f"统计卡片缺 {el}"

    main = client.get("/static/js/main.js").text
    assert "countIncident(a)" in main, "统计没接进播报流"
    # ★ 必须先于 receive()：receive 在暂停时直接返回，出事照样要记
    assert main.index("countIncident(a)") < main.index("receive(a)"), \
        "统计要在 receive() 之前 —— 暂停时 receive 会提前返回"


def test_nav_and_feed_are_the_first_two_cards(client):
    """★ 导航与播报必须在最上方，且播报只占**约两条**的高度。

    两者的可见性是「一直显示」的前提：
      · 播报 —— 陪同者要一直看到「系统正在说什么」；
      · 导航 —— 要一直看到「正往哪走」。
    其余卡片（帧源、意外统计）是次要的，排在后面可以滚。
    播报压到两条高度，是为了让这两张卡不靠滚动就能同屏看到。
    """
    body = html_body(client)
    i = body.index('class="screen"')
    seg = body[i:body.index('class="tabbar"', i)]

    nav = seg.index("导航")
    feed = seg.index("播报")
    video = seg.index("video-strip")
    inc = seg.index("意外统计")

    assert nav < feed < video and nav < feed < inc, \
        "导航与播报必须是前两张卡片"

    css = client.get("/static/app.css").text
    blk = css[css.index(".phone .feed"):]
    blk = blk[:blk.index("}")]
    m = re.search(r"height:\s*(\d+)px", blk)
    assert m, "播报流应是固定像素高度（两条）"
    # 一条 .ann 约 100px，两条 + 间距 ≈ 212 —— 容一点余量
    assert 190 <= int(m.group(1)) <= 240, \
        f"播报流高度 {m.group(1)}px 不是「约两条」（应在 190–240）"


def test_map_startup_is_explicit_not_hidden_in_the_iife(client):
    """★ 地图的启动必须**显式**，不能藏在 IIFE 里自启。

    起因（2026-09-23）：map.js 转成 ES module 时，把
    `window.LingmouMap = {...}` 改成了 `return {...}`，于是 return 落到了
    自启代码**之前** —— `init()` 成了死代码，地图永远不出现。

    ★ 为什么别的用例查不出来：这不是链接错误（id 都在、import 都能解析），
      是**控制流**。静态结构检查全绿，页面却什么都没有，而且不报错。

    所以钉三条：init 被导出、IIFE 内没有 return 之后的启动代码、入口显式调用。
    """
    js = client.get("/static/map.js").text

    assert re.search(r"return \{[^}]*\binit\b", js), "map.js 必须导出 init，否则没人能启动它"

    iife = js[js.index("(() => {"):]
    after_return = iife[iife.index("return {"):iife.index("})();")]
    assert "init()" not in after_return, \
        "return 之后还有 init() —— 那是死代码，地图会静默地不出现"
    assert "DOMContentLoaded" not in after_return, \
        "启动不该藏在 IIFE 里：一个 return 就能把它变成死代码"

    assert "LingmouMap.init()" in client.get("/static/js/main.js").text, \
        "入口必须显式启动地图"
