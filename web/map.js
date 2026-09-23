/* 路线可视化面板 —— 把后端算出的路线画出来。
 *
 * ## 三件事决定了它长这样
 *
 * **一、数据从 WebSocket 的 navigation 播报里取。**
 *   与 `js/ui.js` 开头那条架构约定一致（「所有播报都从 WebSocket 进来」）。
 *   但挂点**不能是 `receive()`** —— `receive()` 在「暂停」时直接入队就返回，
 *   恢复时走的是 `paint()`，会绕过 `receive`。挂在 `receive` 上会让
 *   **暂停期间到达的路线永远画不出来**，而「暂停」正是调试台最常点的按钮。
 *   所以挂在 `ws.onmessage` 那一层（见 `js/main.js` 的同名注释）。
 *
 * **二、底图会加载失败，失败必须落在卡片里。**
 *   百度 JSAPI GL 是从 CDN 拉的，断网、没配 AK、AK 没开对应服务都会失败。
 *   失败时**不装死**：退回一条纯 SVG 的画法（不用底图、不用 AK、不用网），
 *   并如实标明这是「示意图（无底图）」。答辩现场断网也讲得下去。
 *
 * **二点五、底图「起来了」和「画出来了」是两件事。**
 *   百度 JSAPI GL 的渲染器是 WebGL，失败时**不抛异常**：`new BMapGL.Map()`
 *   照常返回，`#map` 里却永远不会出现 canvas，页面上只剩一块空白底图 ——
 *   控制台里也只有它自己的日志。原来的降级判据只有「BMapGL 未定义 /
 *   构造抛异常」，于是这种失败会**静默**漏过去：卡片上写着「底图 = 百度」，
 *   用户看着一块空白会以为是网慢，而路线其实一根线都没画出来。
 *   所以建图后要再确认一次画布真的出现了（见 watchRenderer）。
 *   手机 WebView / 省电模式拿不到 WebGL 是常态，不是边缘情形。
 *
 * **二点六、百度在 AK 校验失败时是直接 `alert()` 的。**
 *   手机上那是个**模态框**：冻住整页（连底部「一键求助」都按不动），
 *   而且只有用户手动点确定才消失 —— 对看不见屏幕的人尤其糟。
 *   所以加载期间接管 `window.alert`，把话留下当失败原因，之后还原
 *   （见 loadMapScript / restoreAlert）。
 *
 * **三、起点/终点标记取自路线几何本身，不是输入框。**
 *   输入框是 **WGS-84**，而百度返回的几何是 **BD-09**（实测同一组数字两种
 *   坐标系算出来的起点差约 1 公里）。拿输入框的值在 BD-09 底图上打标记，
 *   标记会和折线错开一公里。用 `geometry` 的首尾点就天然自洽。
 *   输入框与路线起点的偏差用一条**虚线**画出来 —— 不假装它们是同一个坐标系。
 */

import { readGeo } from "./js/dom.js";

const LingmouMap = (() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SRC = $("map-src");
  const MSG = $("map-msg");
  const SVG = $("map-svg");

  let map = null;          // BMapGL.Map 实例（底图就绪后才有）
  let overlays = [];       // 当前画在地图上的覆盖物
  let lastRoute = null;    // 最近一条**带几何**的路线（底图还没就绪时先存着）
  let lastRouteId = null;  // 最近一条路线的 id —— 用来忽略同一条路线的无几何重复消息
  let mapReady = false;

  // 导航层发出的「系统通告」的 reason（见 app/core/rules/route.py 的 _notice）。
  // ★ 只有这些才该清掉地图 —— 别的层也会发 system 通告（比如 VLM 降级），
  //   那些与路线无关，不该把地图擦了。
  // ★ `degraded_*` 这几个键与后端 `rules/route.py::_DEGRADE_REASONS` 成对
  //   （通告的 reason 是 `degraded_` + 那张表里的键）。后端加一个降级原因，
  //   这里必须跟着加 —— 漏一个，地图就会继续画着上一条已作废的路线。
  const NAV_NOTICES = new Set([
    "no_route",
    "degraded_router_unavailable",
    "degraded_no_destination_geo",
    "degraded_router_misconfigured",
  ]);

  // ★ `no_origin` 刻意**不在**上面那组里。
  //   它说的是「没收到定位，这次从默认位置起算」—— 这是**关于起点**的一句
  //   如实说明，不是在说这条路线作废。路线是照常规划出来的，把它一并擦掉
  //   等于对用户说「路线没了」，而播报流里那条 route 还好端端地挂着。
  //   本系统目前**没有定位能力**（`extra.geo` 得手填，见 navigation.py 的
  //   FALLBACK_ORIGIN 注释），所以「只传了 destination_geo」是**常态**而不是
  //   边缘情形：放进作废组，等于每次规划都在画完之后立刻把刚画好的线擦掉。
  //   正确处理是：路线照画，另外在卡片上标明起点不是定位。
  const ORIGIN_NOTICES = new Set(["no_origin"]);

  // ====================================================================
  // 卡片状态
  // ====================================================================

  //: 起点不是定位时的注记。叠在来源标签后面，**不影响地图上画了什么**。
  let originCaveat = "";
  //: 最近一次 `setSrc()` 的文本。注记到达时要重贴一遍标签，得知道贴什么。
  let srcText = "";

  function setSrc(text) {
    srcText = text;
    if (!SRC) return;
    SRC.textContent = originCaveat
      ? (text ? `${text} · ${originCaveat}` : originCaveat)
      : text;
  }

  function showMessage(text, { caption = false } = {}) {
    if (!MSG) return;
    MSG.textContent = text;   // 刻意用 textContent：与其它模块一样不碰 innerHTML
    MSG.classList.toggle("caption", caption);
    MSG.hidden = false;
  }

  function hideMessage() {
    if (MSG) MSG.hidden = true;
  }

  function clearOverlays() {
    if (map) overlays.forEach((o) => map.removeOverlay(o));
    overlays = [];
    if (SVG) {
      SVG.hidden = true;
      SVG.textContent = "";    // 清空子节点
    }
  }

  // ====================================================================
  // 底图（百度 JSAPI GL）
  // ====================================================================

  //: 底图不可用时的原因，用来在示意图的注脚里**如实说明**是哪一种失败。
  let mapFailReason = "";

  //: 加载/建图期间百度 alert() 出来的话（AK 校验失败走的就是这条路）。
  let akAlert = "";

  //: 原始的 window.alert —— 用完要还回去，别把整页的 alert 都改掉。
  const realAlert = typeof window.alert === "function" ? window.alert : null;

  function restoreAlert() {
    if (realAlert) window.alert = realAlert;
  }

  function loadMapScript(ak) {
    // ★ 接管 window.alert（见模块头注释「二点六」）：百度 AK 校验失败时
    //   直接 alert()，在手机上是冻住整页的模态框（连底部「一键求助」都
    //   按不动）。页面自己从不调 alert()，所以这里换成一个收集器。
    //
    // ★★ 还原点**不能**放在 `finish()` 里。★★
    //   实测：`getscript` 加载完、库回调「就绪」之后，百度才会在处理
    //   `sign_check` / 瓦片时把 alert() 弹出来。在 `finish()` 里还回去，
    //   等于恰好把要挡的那一个放过去 —— 页面照样被模态框冻住，而我们的
    //   降级逻辑（也是 JS）在用户点掉之前一行都跑不了。
    //   所以还原点只有两个：**确认底图真的画出来了**，或**确认它失败**。
    //   外加一条硬超时兜底，别让 alert 被永远占着。
    window.alert = (msg) => { akAlert = String(msg == null ? "" : msg); };
    setTimeout(restoreAlert, 20000);

    // JSAPI GL 用 <script> 引入 —— 零 npm、零构建，符合项目约束。
    //
    // ★★ 必须带 `callback` 参数。★★
    //   不带 callback 时，百度返回的引导脚本是这么干的：
    //       document.write('<script src=".../getscript?...">')
    //   而 `document.write` 在**文档解析完成之后会被浏览器直接忽略**
    //   （只在控制台留一条警告）。我们是页面加载完才动态插这个脚本的，
    //   于是真正的库永远不会被加载，`BMapGL` 永远不出现 ——
    //   **地图不出来，而且不报错**。这是接 JSAPI 最经典的一个坑。
    //   带上 callback 后，百度改用 createElement + appendChild，
    //   并在库就绪时回调我们指定的全局函数。
    return new Promise((resolve) => {
      let done = false;
      const finish = (ok) => {
        if (done) return;
        done = true;
        resolve(ok);      // ★ 这里**不还原** alert，见上面那段注释
      };

      window.__lmMapReady = () => { delete window.__lmMapReady; finish(true); };

      const s = document.createElement("script");
      s.src = "https://api.map.baidu.com/api?v=1.0&type=webgl"
            + `&ak=${encodeURIComponent(ak)}&callback=__lmMapReady`;
      s.onerror = () => finish(false);
      document.head.appendChild(s);

      // 兜底超时：CDN 卡住时不能让卡片永远停在「加载中」。
      setTimeout(() => finish(false), 8000);
    });
  }

  async function startBasemap(ak) {
    setSrc("底图加载中…");
    showMessage("正在加载百度地图底图…（失败会自动退回无底图示意图，路线照常接收）");

    const ok = await loadMapScript(ak);
    if (!ok || typeof BMapGL === "undefined") {
      mapFailReason = "百度地图底图没能加载出来（离线，或这个 AK 没开通「JavaScript API GL」服务）";
      mapFailed();
      return;
    }
    try {
      map = new BMapGL.Map("map");
      map.centerAndZoom(new BMapGL.Point(116.404, 39.915), 13);
      map.enableScrollWheelZoom(true);
      mapReady = true;
      setSrc("底图 = 百度");
      hideMessage();
      if (lastRoute) drawRoute(lastRoute);   // 底图晚于路线到达：补画
      // ★ 建图没抛异常 ≠ 画得出来：GL 渲染器起不来时它一声不响地什么都不画。
      watchRenderer();
    } catch (e) {
      // 库加载出来了但建图失败 —— 最常见的原因是 Referer 白名单不含当前地址。
      mapFailReason = `地图初始化失败（${e.message}）—— 若 AK 配了 Referer 白名单，`
                    + "请把当前访问地址加进去（含局域网 IP 时尤其注意）";
      mapFailed();
    }
  }

  /* ★ 建图成功不等于画得出来：GL 渲染器起不来时 `#map` 里永远不会有
   *   canvas，页面上只留下一块空白底图。稍等片刻确认画布存在，没有就
   *   如实降级成示意图。等的是几百毫秒的量级 —— 画布是同步建的，
   *   这么久了还没有，就是真没起来。 */
  function watchRenderer(tries = 6) {
    if (!mapReady) return;                                  // 已经降级过了
    if (document.querySelector("#map canvas")) {            // 正常：GL 就绪
      restoreAlert();
      return;
    }
    if (tries <= 0) {
      mapFailReason = akAlert
        ? `百度地图没能初始化（${akAlert.slice(0, 120)}）`
        : "这个浏览器没能给出 WebGL 画布（多半是不支持 WebGL）";
      mapFailed();
      return;
    }
    setTimeout(() => watchRenderer(tries - 1), 400);
  }

  function mapFailed() {
    mapReady = false;
    setSrc("底图不可用");
    // ★ 把 #map 一起藏掉：它自己会挂一张白灰色的 bg.png，留在示意图底下
    //   会让人以为「图在这儿，只是没加载完」—— 又是一次沉默的失效。
    document.querySelector(".map-stage")?.classList.add("nomap");
    restoreAlert();
    // 已经有路线的话直接改画示意图；没有就只留提示。
    if (lastRoute) drawRoute(lastRoute);
    else showMessage(`路线仍然照常接收 —— 有坐标时会画成无底图示意图。\n${mapFailReason}`);
  }

  // ====================================================================
  // 画路线
  // ====================================================================

  function drawRoute(d) {
    if (!d || d.kind !== "route") return;

    const pts = d.geometry;
    const hasGeo = Array.isArray(pts) && pts.length >= 2;
    const sameRoute = d.route_id && d.route_id === lastRouteId;

    // ★★ 一次导航会产生**多条** `kind === "route"` 的播报：分步指令 + 无障碍警告。
    //    而几何**只挂在第一条**上（见 `rules/route.py`，那是为了别把 1.2 KB
    //    的折线序列化 N 次）。
    //    所以后面那些消息同样是 route、却没有 geometry —— 对着它们清空重画，
    //    会把**已经画好的线擦掉**，还播出一句假话「这条路线没有坐标」。
    //    同一条 route_id 的无几何消息，直接忽略。
    if (!hasGeo && sameRoute) return;

    lastRouteId = d.route_id || lastRouteId;

    // ★ 新路线一到，上一条的起点注记就**过期**了 —— 留着它会在下次拿到
    //   真定位时继续显示「起点非定位」，那是反方向的假话。
    //   这次若仍然没定位，它自己那条 no_origin 通告会紧接着把它设回来。
    originCaveat = "";

    if (!hasGeo) {
      // 真的没有坐标（内置演示路网就是这样）。
      // **如实说**，不画一条凭空连起来的线（那是在编一条没走过的路）。
      lastRoute = null;
      clearOverlays();
      setSrc("无可画数据");
      showMessage("这条路线没有坐标，画不出来。内置演示路网只有文字和距离，" +
                  "接真实地图（ROUTER=baidu）才有几何。播报与米数不受影响。");
      return;
    }

    lastRoute = d;
    clearOverlays();

    // ★ 判据顺序写死：**先看几何有没有，再看底图能不能用**。
    //   这样即使 coord_system 哪天报错了，也不会在地图上渲染出错位的东西。
    if (mapReady && d.coord_system === "bd09ll") {
      drawOnMap(pts);
    } else {
      drawOnSVG(pts, d.coord_system);
    }
  }

  function drawOnMap(pts) {
    setSrc("底图 = 百度");
    hideMessage();
    document.querySelector(".map-stage")?.classList.remove("nomap");

    const path = pts.map(([lng, lat]) => new BMapGL.Point(lng, lat));

    overlays.push(new BMapGL.Polyline(path, {
      strokeColor: "#5b9dff",
      strokeWeight: 6,
      strokeOpacity: 0.9,
    }));

    // 起点 / 终点取自几何的首尾点 —— 与折线同一个坐标系，不可能错开。
    // ★ 配文字标签：JSAPI 默认的 Marker 图标长得一样，光看两个点
    //   分不出哪头是起点、哪头是终点。
    const last = path[path.length - 1];
    overlays.push(new BMapGL.Marker(path[0]));
    overlays.push(new BMapGL.Marker(last));
    overlays.push(labelAt(path[0], "起点"));
    overlays.push(labelAt(last, "终点"));

    // 输入框里的起点（WGS-84）与路线起点（BD-09）对不上的话，用虚线连出来。
    const box = readBoxOrigin();
    if (box) {
      const p = new BMapGL.Point(box.lng, box.lat);
      overlays.push(new BMapGL.Polyline([p, path[0]], {
        strokeColor: "#fbbf24",
        strokeWeight: 2,
        strokeOpacity: 0.7,
        strokeStyle: "dashed",
      }));
    }

    overlays.forEach((o) => map.addOverlay(o));
    // 把整条路线放进视野。setViewport 会自己算合适的缩放级别。
    map.setViewport(path);
  }

  /** 地标上的文字标签。配色沿用 app.css 的深色主题。 */
  function labelAt(point, text) {
    const l = new BMapGL.Label(text, {
      position: point,
      offset: new BMapGL.Size(14, -6),
    });
    l.setStyle({
      color: "#e9edf5",
      backgroundColor: "rgba(7, 9, 14, .8)",
      border: "1px solid rgba(255, 255, 255, .16)",
      borderRadius: "4px",
      padding: "1px 5px",
      fontSize: "11px",
      lineHeight: "16px",
    });
    return l;
  }

  /** 读输入框里的起点（WGS-84）。只用来画那条提示偏差的虚线。
   *
   * ★ 复用 `js/dom.js` 的 `readGeo()`，**不另写一份**。两边各写一份读同一组
   *   输入框的代码，等于把「字段 id 叫什么、怎么算合法」这个约定复制成两份 ——
   *   哪天改了一处，另一处会静默地读错，而那条虚线会指向错的地方。
   */
  function readBoxOrigin() {
    return readGeo("geo");
  }

  // ====================================================================
  // 无底图时的示意画法（纯 SVG，不需要网络/AK）
  // ====================================================================

  function drawOnSVG(pts, coordSystem) {
    if (!SVG) return;
    const W = 100, H = 62, PAD = 6;

    const lngs = pts.map((p) => p[0]);
    const lats = pts.map((p) => p[1]);
    const minLng = Math.min(...lngs), maxLng = Math.max(...lngs);
    const minLat = Math.min(...lats), maxLat = Math.max(...lats);

    // ★ 经度要按纬度缩放：1 度经度的实际长度是 1 度纬度的 cos(纬度) 倍。
    //   不缩的话南北向的路线会被拉斜 —— 那画出来的就不是真实几何了。
    const kx = Math.cos(((minLat + maxLat) / 2) * Math.PI / 180) || 1;
    const spanX = Math.max((maxLng - minLng) * kx, 1e-9);
    const spanY = Math.max(maxLat - minLat, 1e-9);
    const scale = Math.min((W - 2 * PAD) / spanX, (H - 2 * PAD) / spanY);
    const offX = (W - spanX * scale) / 2;
    const offY = (H - spanY * scale) / 2;

    // 纬度往上增大，SVG 的 y 往下增大 —— 所以纬度取负。
    const xy = ([lng, lat]) => [
      offX + (lng - minLng) * kx * scale,
      offY + (maxLat - lat) * scale,
    ];

    const coords = pts.map(xy);
    const line = coords.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
    const [ax, ay] = coords[0];
    const [bx, by] = coords[coords.length - 1];

    SVG.setAttribute("viewBox", `0 0 ${W} ${H}`);
    SVG.innerHTML = `
      <polyline points="${line}" fill="none" stroke="#5b9dff" stroke-width="1.4"
                stroke-linejoin="round" stroke-linecap="round" />
      <circle cx="${ax.toFixed(2)}" cy="${ay.toFixed(2)}" r="1.8" fill="#34d399" />
      <circle cx="${bx.toFixed(2)}" cy="${by.toFixed(2)}" r="1.8" fill="#fb7185" />`;
    SVG.hidden = false;

    // ★ 措辞必须独立：没有底图就**不能叫「地图」**，否则是在暗示这是真实路网。
    //   而且要说清**是哪一种**失败 —— 「离线」和「没配 AK」是两回事，
    //   让人去查错的地方比不说更糟。
    const why = mapFailReason
      || (coordSystem && coordSystem !== "bd09ll"
          ? `坐标是 ${coordSystem}，底图要 bd09ll`
          : "地图底图不可用");
    setSrc("示意图（无底图）");
    showMessage(`路线示意图（无底图）—— ${why}。上图是真实几何，只是没有街道底图。`,
                { caption: true });
  }

  // ====================================================================
  // 对外入口 —— 由 js/main.js 的 ws.onmessage 调用
  // ====================================================================

  function onAnnouncement(a) {
    if (!a || !a.detail) return;

    if (a.detail.kind === "route") {
      drawRoute(a.detail);
      return;
    }

    if (a.detail.kind !== "system") return;

    // ★ 降级 / 无路线的通告必须让地图**跟着作废**。
    //   否则「未能规划到步行路线」都播出来了，地图上还自信地画着上一条路线
    //   —— 那正是这套系统最忌讳的失效模式（沉默 / 陈旧信息被当成现状）。
    if (NAV_NOTICES.has(a.detail.reason)) {
      lastRoute = null;
      originCaveat = "";
      clearOverlays();
      setSrc("路线已作废");
      showMessage(`上一条路线已作废：${a.text}`);
      return;
    }

    // ★ 起点相关的通告**不作废地图**。路线照画 —— 它确实是规划出来的，
    //   只是起点用了默认位置。擦掉它是在说另一句假话（「路线没了」）。
    //   该做的是把这个前提**标在卡片上**，让人知道这条线是从哪儿起算的。
    if (ORIGIN_NOTICES.has(a.detail.reason)) {
      originCaveat = "起点非定位";
      setSrc(srcText);          // 重贴标签，把注记叠上去
    }
  }

  // ====================================================================
  // 启动
  // ====================================================================

  async function init() {
    if (!SRC || !MSG || !SVG) return;   // 卡片不在页面上（比如被裁掉了）
    try {
      const r = await fetch("/v1/frontend-config");
      const cfg = await r.json();
      if (cfg.baidu_browser_ak) {
        startBasemap(cfg.baidu_browser_ak);   // 不 await：别挡住后面的初始化
      } else {
        mapFailReason = "地图未配置：请在 .env 里填 BAIDU_BROWSER_AK"
                      + "（浏览器端 AK，与算路线用的服务端 AK 是两个东西）";
        mapFailed();
      }
    } catch (e) {
      setSrc("配置读取失败");
      showMessage(`读不到 /v1/frontend-config：${e.message}`);
    }
  }

  /* 折叠展开后调用：容器尺寸变了，让 GL 重新量一次。
     ★ 地图是在「折叠状态」下初始化的（容器被 max-height 压住），
       展开后必须让它重新测量，否则可能一直画不出来 —— 和容器高度为 0
       导致静默不渲染是同一类问题，只是更隐蔽。 */
  function refresh() {
    try { if (map && typeof map.resize === "function") map.resize(); } catch (e) { /* 忽略 */ }
    // 兜底：某些版本只对窗口 resize 有反应
    try { window.dispatchEvent(new Event("resize")); } catch (e) { /* 忽略 */ }
  }

  return { init, onAnnouncement, refresh };
})();

export default LingmouMap;
