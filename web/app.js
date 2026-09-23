"use strict";

/* 灵眸伴途 · 页面逻辑
 *
 * 一根筋：**所有播报都从 WebSocket 进来**，HTTP 响应里的 announcements 只用来
 * 数闸门结果（放行几条 / 丢掉几条）。
 *
 * 页面分两层，这个文件同时伺候两层（详见 index.html 头注释）：
 *   · 手机视图   `#app`       —— 默认显示
 *   · 开发者面板 `#devPanel`  —— 默认 hidden，由 setDev() 开关
 * 两层的元素都在 DOM 里，所以按 id 取用一律不判空。
 *
 * 为什么不直接拿 fetch 的返回值渲染？
 *   端侧真实拿播报的通道只有 /v1/stream 一条。调试台要是绕过它渲染，
 *   就等于「用一条生产不存在的路径验证契约」—— WS 断了你也看不出来。
 *   所以哪怕多一次往返，渲染也一律走 WS。
 */

const $ = (id) => document.getElementById(id);

const SOURCE_NAMES = {
  perception: "感知",
  safety: "安全",
  navigation: "导航",
  emergency: "紧急",
  system: "系统",
};

// =====================================================================
// 页面模式 —— 手机视图（默认） / 开发者面板
// =====================================================================

const DEV_KEY = "lingmou.dev";

/* ★ 「把测试件藏起来」＝ `hidden`，不是删掉。
 *   app.js / map.js 一律按 id 取 DOM，元素真被删掉会当场报错；
 *   而契约验证（帧源、其余路由、请求日志、统计）随时要能翻出来。
 *   所以入口留两个：地址栏 ?dev=1，或右上角 ⚙；选择记在 localStorage。 */
function setDev(on) {
  $("devPanel").hidden = !on;
  $("devBtn").classList.toggle("on", on);
  $("devBtn").setAttribute("aria-pressed", on ? "true" : "false");
  document.body.classList.toggle("dev", on);
  try { localStorage.setItem(DEV_KEY, on ? "1" : "0"); } catch { /* 隐私模式下会拒绝 */ }
  if (on) log("开发者模式：开（测试件已显示）", "dim");
}

function devFromUrl() {
  const q = new URLSearchParams(location.search).get("dev");
  if (q === "1") return true;
  if (q === "0") return false;
  try { return localStorage.getItem(DEV_KEY) === "1"; } catch { return false; }
}

$("devBtn").onclick = () => setDev($("devPanel").hidden);
$("devClose").onclick = () => setDev(false);

// =====================================================================
// 轻提示 —— 手机视图里没有请求日志，动作必须有可见反馈
// =====================================================================

let toastTimer = null;

function toast(text, kind = "") {
  const el = $("toast");
  if (!el) return;
  el.textContent = text;
  el.className = `toast ${kind}`.trim();
  el.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

// =====================================================================
// 紧急全屏 —— priority 3 顶到最前面
// =====================================================================

let emgTimer = null;

function emergency(a) {
  const box = $("emg");
  if (!box) return;
  $("emgText").textContent = a.text;
  box.hidden = false;
  if (emgTimer) clearTimeout(emgTimer);
  // 跟播报自己的 ttl 对齐：这条播报过期了，横幅就不该再挂着
  // ——「陈旧信息被当成现状」是这套系统最忌讳的失效模式。
  emgTimer = setTimeout(hideEmergency, Math.max(4000, a.ttl_ms || 10_000));
}

function hideEmergency() {
  if (emgTimer) { clearTimeout(emgTimer); emgTimer = null; }
  const box = $("emg");
  if (box) box.hidden = true;
}

$("emgOk").onclick = hideEmergency;
$("emg").onclick = hideEmergency;

// =====================================================================
// 状态
// =====================================================================

let frameSeq = 0;      // mock 靠 index % 3 轮换场景，必须全局递增（见 index.html 头注释）
let ttsOn = false;     // 真实初值由下面的 setTts(true) 定 —— 这是个用耳朵的产品
let loopTimer = null;
let paused = false;
let filterMin = 0;

const history = [];    // 已渲染的 .ann 元素，最新在前；只用于切换筛选
const queue = [];      // 暂停期间攒下的播报，恢复时补上

const stats = { total: 0, critical: 0, expired: 0, dropped: 0, frames: 0 };

function bump(key, by = 1) {
  stats[key] += by;
  const el = {
    total: $("stTotal"), critical: $("stCritical"), expired: $("stExpired"),
    dropped: $("stDropped"), frames: $("stFrames"),
  }[key];
  if (el) el.textContent = stats[key];
}

// =====================================================================
// 上传 —— 两个帧源共用的唯一出口
// =====================================================================

async function sendFrame(blob, source) {
  const index = frameSeq++;
  const fd = new FormData();
  fd.append("image", blob, `frame_${index}.jpg`);
  fd.append("source", source);
  fd.append("frame_id", `web_${index}`);
  fd.append("ts", String(Date.now()));
  fd.append("extra", JSON.stringify({ index, origin: "web-console" }));

  try {
    const r = await fetch("/v1/frame", { method: "POST", body: fd });
    const b = await r.json();
    if (!r.ok) { log(`${source} 上传失败 ${r.status}：${b.error || ""}`, "err"); return; }

    bump("frames");
    const g = gateSummary(b);
    bump("dropped", g.dropped);
    log(`${source} idx=${index} 产出 ${g.produced} 条，放行 ${g.passed} 条` +
        (g.dropped ? `，丢弃 ${g.dropped} 条（重复/过期）` : ""), g.dropped ? "dim" : "ok");
  } catch (e) {
    log(`${source} 上传异常：${e.message}`, "err");
  }
}

/* 闸门结果。响应里的 `arbiter.sent` 是**累计**放行过的 id 集合，
 * 所以只能拿「本次产出的 id 在不在里面」来数，不能拿集合长度当本次数量。
 * /v1/emergency/tick 的回包是平铺的 {sent:[...]}，一并兼容。 */
function gateSummary(b) {
  const sentIds = new Set(b.arbiter?.sent ?? b.sent ?? []);
  const produced = b.announcements ?? [];
  const passed = produced.filter((a) => sentIds.has(a.id)).length;
  return { produced: produced.length, passed, dropped: produced.length - passed };
}

// =====================================================================
// 帧源 ① 视频抽帧
// =====================================================================

const video = $("video");
const canvas = document.createElement("canvas");

async function grabAndSend(source) {
  if (!video.videoWidth || video.paused) return;
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);
  const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", 0.8));
  if (blob) await sendFrame(blob, source);
}

function toggleLoop() {
  const btn = $("loopBtn");
  if (loopTimer) {
    clearInterval(loopTimer.per);
    clearInterval(loopTimer.saf);
    loopTimer = null;
    btn.textContent = "开始发帧";
    btn.classList.remove("running");
    log("已停止发帧", "dim");
    return;
  }

  video.play().catch(() => {});
  const per = Math.max(200, +$("perMs").value || 2000);
  const saf = Math.max(100, +$("safMs").value || 600);

  // 两条流水线各自独立定时 —— 频率不同是设计要求，不是一个循环发两次
  loopTimer = {
    per: setInterval(() => grabAndSend("perception"), per),
    saf: setInterval(() => grabAndSend("safety"), saf),
  };
  btn.textContent = "停止发帧";
  btn.classList.add("running");
  log(`开始发帧：感知 ${per}ms / 安全 ${saf}ms`, "ok");
}

$("loopBtn").onclick = toggleLoop;

// =====================================================================
// 帧源 ③ 单张 / 多张图片
// =====================================================================

function sendFiles(files) {
  const imgs = [...files].filter((f) => f.type.startsWith("image/"));
  if (!imgs.length) return;
  imgs.forEach((f) => sendFrame(f, "perception"));  // 图片当场景描述喂第一层
  log(`拖入 ${imgs.length} 张图片 → 感知层`, "ok");
}

$("file").onchange = (e) => sendFiles(e.target.files);

const zone = $("dropzone");
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("dragenter", () => zone.classList.add("over"));
document.addEventListener("dragleave", () => zone.classList.remove("over"));
document.addEventListener("drop", (e) => {
  e.preventDefault();
  zone.classList.remove("over");
  if (e.dataTransfer?.files?.length) sendFiles(e.dataTransfer.files);
});

// =====================================================================
// 其余路由
// =====================================================================

// 从一组输入框里读坐标，格式约定 {"lat", "lng"}，坐标系 WGS-84。
// 填不全就返回 null —— 绝不拿半个坐标去规划。
function readGeo(prefix) {
  const lat = parseFloat($(`${prefix}-lat`).value);
  const lng = parseFloat($(`${prefix}-lng`).value);
  return Number.isFinite(lat) && Number.isFinite(lng) ? { lat, lng } : null;
}

/* 手机视图里没有请求日志（它在开发者面板），所以每个动作都得自己
 * 说一句人话 —— 按钮按下去必须立刻有反应。 */
function routeToast(path, g) {
  if (path.endsWith("/emergency/sos")) {
    return g.produced ? "已发出求助" : "求助已在处理中，没有重复播报";
  }
  if (path.endsWith("/emergency/cancel")) return "已取消求助";
  if (path.endsWith("/navigation/route")) {
    return g.produced ? "路线已下发，播报马上到" : "没有拿到可播报的路线";
  }
  if (path.endsWith("/safety/fall")) {
    return g.produced ? "跌倒信号已发出" : "跌倒信号已发出（重复，已忽略）";
  }
  return g.produced ? `已发送，放行 ${g.passed} 条` : "已发送（被闸门丢弃）";
}

document.querySelectorAll("button[data-route]").forEach((btn) => {
  btn.onclick = async () => {
    const path = btn.dataset.route;
    const body = Object.assign(
      { frame_id: `ui_${Date.now()}`, ts: Date.now() },
      JSON.parse(btn.dataset.body || "{}"),
    );
    if (btn.dataset.dest) {
      body.extra = { ...(body.extra || {}), destination: $("dest").value };
    }
    if (btn.dataset.geo) {
      // 第三层的坐标。真实地图只认坐标、不认地名，所以目的地坐标做成可选的
      // `destination_geo`：留空时不发，后端会如实降级回内置路网并播报说明。
      const origin = readGeo("geo");
      const destGeo = readGeo("dest-geo");
      body.extra = { ...(body.extra || {}) };
      if (origin) body.extra.geo = origin;
      if (destGeo) body.extra.destination_geo = destGeo;
    }
    try {
      const r = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const b = await r.json();
      const g = gateSummary(b);
      bump("dropped", g.dropped);
      log(`POST ${path} → 产出 ${g.produced} 条，放行 ${g.passed} 条`,
          g.produced ? "ok" : "dim");
      toast(routeToast(path, g), g.produced ? "ok" : "");
    } catch (e) {
      log(`POST ${path} 失败：${e.message}`, "err");
      toast(`发送失败：${e.message}`, "err");
    }
  };
});

// ---------------------------------------------------------------------
// 「用当前位置」—— 手机上唯一能自己拿到起点的方式
// ---------------------------------------------------------------------

$("geoBtn").onclick = () => {
  const btn = $("geoBtn");
  const reset = () => { btn.disabled = false; btn.textContent = "用当前位置"; };

  if (!navigator.geolocation) {
    toast("这个浏览器不提供定位", "err");
    return;
  }
  btn.disabled = true;
  btn.textContent = "定位中…";

  navigator.geolocation.getCurrentPosition(
    (pos) => {
      // ★ 浏览器给的就是 WGS-84 —— 与服务端 AK 的 coord_type=wgs84 对齐。
      //   本项目**不做** WGS-84 → BD-09 的转换（见 app/core/routers/base.py）。
      $("geo-lat").value = pos.coords.latitude.toFixed(6);
      $("geo-lng").value = pos.coords.longitude.toFixed(6);
      const adv = $("geo-lat").closest("details");
      if (adv) adv.open = true;          // 展开，让改动看得见
      toast(`起点已换成当前位置（精度约 ${Math.round(pos.coords.accuracy)} 米）`, "ok");
      reset();
    },
    (err) => {
      // 定位只在 https 或 localhost 下可用 —— 局域网 IP 直连会被浏览器拒绝。
      toast(`定位失败：${err.message || err.code}（需要 HTTPS 或 localhost）`, "err");
      reset();
    },
    { enableHighAccuracy: true, timeout: 8000, maximumAge: 0 },
  );

  // 兜底：某些实现两个回调都不来，按钮不能永远卡在「定位中…」。
  setTimeout(reset, 9000);
};

// =====================================================================
// WebSocket —— 端侧唯一的播报入口
// =====================================================================

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/v1/stream`);

  ws.onopen = () => setWs(true);
  ws.onclose = () => { setWs(false); setTimeout(connect, 2000); };  // 断线自愈
  ws.onerror = () => setWs(false);

  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "hello") { log(`WS 已连接 ts=${m.data.ts}`, "ok"); return; }
    if (m.type === "pong") return;
    if (m.type === "announcement") {
      receive(m.data);
      // 路线可视化面板（web/map.js，可选加载；没加载时可选链安全跳过）。
      //
      // ★ 刻意挂在这里而**不是 `receive()` 里**：`receive()` 在「暂停」时
      //   直接入队就返回，恢复时走的是 `paint()`，会绕过 `receive` ——
      //   挂在那儿会让**暂停期间到达的路线永远画不出来**，而「暂停」正是
      //   这个调试台最常点的按钮。
      // ★ 地图也不该受 `paused` / `filterMin` 影响：它不是播报墙，
      //   而是「当前这条路线长什么样」的一个视图。
      window.LingmouMap?.onAnnouncement(m.data);
    }
  };
}

function setWs(ok) {
  $("wsDot").className = "dot " + (ok ? "on" : "off");
  $("wsTxt").textContent = ok ? "已连接" : "断开，重连中…";
}

// =====================================================================
// 播报渲染
// =====================================================================

function receive(a) {
  bump("total");

  // ★ 紧急播报**不排队**：暂停是「别往墙上贴」，不是「别告诉我出事了」。
  if (a.priority >= 3) {
    bump("critical");
    emergency(a);
  }

  // 暂停只是「不往墙上贴」，不拦数据 —— 统计照涨，
  // 恢复时补上，否则暂停期间发生的事在调试台里就凭空消失了。
  if (paused) { queue.push(a); return; }
  // 被筛掉的也留在 history 里，切回「全部」时能重新看见；
  // 但已经不播报了，就不该再出声、再震动。
  paint(a, a.priority >= filterMin);
}

function paint(a, visible = true) {
  const feed = $("feed");
  feed.querySelector(".empty")?.remove();

  const el = document.createElement("div");
  el.className = `ann p${a.priority}${a.source === "system" ? " sys" : ""}`;
  el.dataset.priority = a.priority;
  el.dataset.ts = Date.now();
  if (!visible) el.hidden = true;

  const head = document.createElement("div");
  head.className = "head";

  const src = document.createElement("span");
  src.className = "tag src";
  src.textContent = SOURCE_NAMES[a.source] || a.source;
  head.appendChild(src);

  if (a.priority >= 2) {
    const hot = document.createElement("span");
    hot.className = "tag hot";
    hot.textContent = a.priority === 3 ? "紧急" : "重要";
    head.appendChild(hot);
  }

  const when = document.createElement("span");
  when.className = "expire-note";
  when.textContent = "刚刚";
  head.appendChild(when);

  const txt = document.createElement("div");
  txt.className = "txt";
  txt.textContent = a.text;          // textContent，不用 innerHTML

  const meta = document.createElement("div");
  meta.className = "meta";
  const bits = [
    `priority=${a.priority}`, `ttl=${a.ttl_ms}ms`,
    `haptic=${a.haptic}`, a.interrupt ? "interrupt" : null, a.id,
  ].filter(Boolean);
  for (const b of bits) {
    const s = document.createElement("span");
    s.className = "tag";
    s.textContent = b;
    meta.appendChild(s);
  }

  const ttl = document.createElement("div");
  ttl.className = "ttl";
  const bar = document.createElement("i");
  bar.style.animationDuration = `${a.ttl_ms}ms`;
  ttl.appendChild(bar);

  el.append(head, txt, meta, ttl);
  feed.prepend(el);
  history.unshift(el);
  while (history.length > 100) history.pop().remove();

  speak(a.text);
  haptic(a.haptic, el);
  expire(el, a.ttl_ms);
}

/* ttl 是从「收到」起算的，过期即丢 —— 这里把「这条其实已经不该播了」
 * 变成看得见的状态：整条褪色 + 倒计时条走完。 */
function expire(el, ttl) {
  setTimeout(() => {
    el.classList.add("expired");
    const note = el.querySelector(".expire-note");
    if (note) note.textContent = "已过期";
    bump("expired");
  }, ttl);
}

function haptic(kind, el) {
  if (kind && kind !== "none") {
    el.classList.add("haptic");
    setTimeout(() => el.classList.remove("haptic"), 300);
  }
  // 端侧真马达；浏览器上多数环境没有，拿不到就静默退回视觉提示
  const pat = { short: 80, double: [60, 60, 60], long: 400 }[kind];
  if (pat && navigator.vibrate) navigator.vibrate(pat);
}

// =====================================================================
// 筛选 / 暂停 / 清空
// =====================================================================

$("filters").onclick = (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  [...e.currentTarget.children].forEach((c) => c.classList.toggle("on", c === chip));
  filterMin = +chip.dataset.min;
  for (const el of history) el.hidden = +el.dataset.priority < filterMin;
  log(`筛选：${chip.textContent}`, "dim");
};

$("pauseBtn").onclick = () => {
  paused = !paused;
  $("pauseBtn").textContent = paused ? "恢复" : "暂停";
  if (!paused && queue.length) {
    log(`恢复，补上积压的 ${queue.length} 条`, "dim");
    queue.splice(0).forEach((a) => paint(a, a.priority >= filterMin));
  }
};

//: 空态。与 index.html 里的那份必须一致 —— 清空后重新插的就是它。
const EMPTY_HTML = '<div class="empty">' +
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5 6.5 9H3v6h3.5L11 19zM15.5 8.5a5 5 0 0 1 0 7" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>' +
  '<p>还没有播报</p><p class="dim">系统出声后，这里按时间倒序列出来</p></div>';

$("clearBtn").onclick = () => {
  history.splice(0).forEach((el) => el.remove());
  $("feed").innerHTML = EMPTY_HTML;
  log("已清空播报流", "dim");
  toast("已清空");
};

// =====================================================================
// 语音播报
// =====================================================================

function speak(text) {
  if (!ttsOn || paused || !window.speechSynthesis) return;
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "zh-CN";
  speechSynthesis.speak(u);
}

function setTts(on) {
  ttsOn = on;
  $("ttsTxt").textContent = on ? "播报：开" : "播报：关";
  $("ttsBtn").classList.toggle("on", on);
  $("ttsBtn").setAttribute("aria-pressed", on ? "true" : "false");
  if (!on) window.speechSynthesis?.cancel();
}

$("ttsBtn").onclick = () => setTts(!ttsOn);
// 默认**开**：这一屏的全部价值就是出声。浏览器在首次交互前可能拒绝
// 朗读，用户碰一下屏幕就正常了 —— 不必为此把默认值改成「关」。
setTts(true);

// =====================================================================
// 健康检查 —— 「没出声」必须能和「系统哑了」区分开
// =====================================================================

async function health() {
  const dev = $("devStatus");
  try {
    const b = await (await fetch("/v1/health")).json();
    const bad = !b.ok;
    $("hDot").className = "dot " + (bad ? "on" : "off");
    // ★ 手机上只说「正常 / 降级」两个词，但**必须说**：用户若不知道系统
    //   哑了，会把「没出声」理解成「环境安全」。细节放 title 与开发者面板
    //   —— 见 app/api/routes.py::health。
    $("hTxt").textContent = bad ? "降级" : "正常";
    $("hPill").title = bad
      ? "降级：" + b.degraded.map((d) => d.reason).join(", ")
      : "全部实现可用";

    if (dev) {
      const c = b.config;
      dev.textContent = [
        `运行实现  VLM=${c.VLM_PROVIDER}  DET=${c.DETECTOR}  ROUTER=${c.ROUTER}  FRAME=${c.FRAME_SOURCE}`,
        `降级      ${bad ? b.degraded.map((d) => `${d.reason}(${d.impl})`).join(", ") : "无"}`,
        ...Object.keys(b.impls).map((k) => `可选 ${k.padEnd(10)} ${b.impls[k].join(", ")}`),
      ].join("\n");
    }
  } catch (e) {
    $("hDot").className = "dot off";
    $("hTxt").textContent = "连不上";
    $("hPill").title = `健康检查失败：${e.message}`;
    if (dev) dev.textContent = `健康检查失败：${e.message}`;
  }
}

// =====================================================================
// 日志
// =====================================================================

let logLines = 0;

function log(line, kind = "") {
  const el = $("log");
  const row = document.createElement("div");
  if (kind) row.className = `l-${kind}`;
  row.textContent = `${new Date().toLocaleTimeString()} ${line}`;
  el.prepend(row);

  while (el.children.length > 300) el.lastElementChild.remove();
  $("logTail").textContent = `${++logLines} 条`;
}

// 相对时间每秒刷新一次。过期的条目不再刷新，免得「刚刚」和「已过期」打架。
setInterval(() => {
  for (const el of history) {
    if (el.classList.contains("expired")) continue;
    const note = el.querySelector(".expire-note");
    if (!note) continue;
    const s = Math.round((Date.now() - +el.dataset.ts) / 1000);
    note.textContent = s < 2 ? "刚刚" : `${s} 秒前`;
  }
}, 1000);

// =====================================================================
// 帧源 ② 摄像头 —— 只留接口，本期不实现取流
// =====================================================================
//
// 接入时把下面的注释打开即可，后端与 sendFrame() 都不用改：
//
//   async function startCamera() {
//     const stream = await navigator.mediaDevices.getUserMedia({
//       video: { facingMode: "environment" },      // 后置摄像头
//     });
//     const v = document.createElement("video");
//     v.srcObject = stream; v.play();
//     setInterval(() => grabFrom(v, "safety"), 300);   // 安全层要高频
//   }
//
// 注意：getUserMedia 只在 https 或 localhost 下可用 —— 局域网 IP
// 直连时浏览器会拒绝，端侧真机需要 HTTPS 或原生壳。

setDev(devFromUrl());   // 先定视图再做别的 —— 免得新播报到达时版面在跳
connect();
health();
setInterval(health, 5000);
