/* 帧源模拟器 —— **设备端的替身**，单独一页（`/static/sender.html`）。
 *
 * 与播报界面（`/`）的分工：
 *   它只管「喂帧」：抓一帧 → `POST /v1/frame`。
 *   播报界面只管「看结果」：播报全部从 `WS /v1/stream` 回来。
 * 两边共用同一个出口 `net.js::sendFrame()`，但**互不依赖** ——
 * 这一页关掉，播报界面照常工作（只是没有内容可播）。
 *
 * ★ 为什么帧源要独立成页，而不是留在播报界面里：
 *   ① 真机上这一端是**摄像头**，不是网页里的一段视频。它属于设备端，
 *      不该让「交付形态」背着一台演示机。
 *   ② 播报界面服务的是**看不见屏幕**的人 —— 视频条哪怕只有 88px，
 *      也是从播报流和求助按钮那里拿走的。
 *   ③ 分开之后两边各走各的：模拟器在笔记本上喂帧、播报界面在手机上看
 *      结果，这正是真机的拓扑。
 *
 * ★ 为什么是**同一端口下的子页**，而不是另开一个端口：
 *   跨端口会把 `demo.mp4` 变成跨域资源，`drawImage()` 之后
 *   `canvas.toBlob()` 会因画布被**污染**直接抛 SecurityError ——
 *   表现是「按钮变成停止发帧，一帧都没发出去」。同一端口没有这个问题，
 *   将来接 `getUserMedia` 也少一条 https 的来源限制（127.0.0.1 算安全上下文）。
 *
 * ★ 这一页**不做** TTS / 震动：看这一页的是演示者，不是用户的耳朵。
 *   三通道反馈（眼/耳/手）是播报界面的事 —— 那边不能假设用户在看屏幕。
 */

import { $ } from "./dom.js";
import { state, stats } from "./state.js";
import { log } from "./log.js";
import { sendFrame } from "./net.js";

// =====================================================================
// 帧源 ① 视频抽帧 —— 播报的内容来源
// =====================================================================

const video = $("video");
const canvas = document.createElement("canvas");

const SOURCE_CN = { perception: "感知", safety: "安全" };

/* 帧源的状态反馈。
 * ★ 为什么必须有：`grabAndSend()` 在视频没就绪时会**静默 return** ——
 *   按钮已经变成「停止发帧」，页面看起来一切正常，实际一帧都没发出去。
 *   这跟「后端坏了」长得一模一样。实测（worktree 里缺 demo.mp4）：
 *   `POST /v1/frame` 收到 **0 次**，而日志里只有几条 /data 404。
 *   最常见的原因就是素材没生成 —— 它是 gitignore 的产物。
 */
export function setFrameMsg(text, warn = false) {
  const el = $("frameMsg");
  if (!el || el.textContent === text) return;  // 去重：别每拍刷一次 DOM
  el.textContent = text;
  el.classList.toggle("warn", warn);
}

function onVideoBroken() {
  state.frameWarn = "视频加载失败 —— 先跑 python scripts/make_test_video.py 生成 data/demo.mp4";
  setFrameMsg(state.frameWarn, true);
}

//: 浏览器解不出这段编码时的原话。换素材是唯一出路，所以直接说怎么换。
const CODEC_WARN = "这段视频浏览器解不出来（编码不支持，H.265 常见）—— 换成 H.264 的 mp4";

/** 元数据都到了、可还是量不出画面 —— 这不是「还在加载」，是**解不出来**。
 *
 * ★ 实测（2026-09-23，真实 HEVC 素材）：`readyState=4`、`duration=11.6` 秒
 *   （时间轴照走、`paused=false`）、`videoWidth=0`、画布上全黑 —— 而且
 *   **既不抛异常也不触发 error 事件**。只说「视频未就绪」的话，用户会一直
 *   等一个永远不会来的帧，正是本项目最忌讳的那种沉默失效。
 *   `readyState>=1` 说明元数据已经解析出来了，此时还没有尺寸就只剩编码这一种解释。
 */
function decodingBroken() {
  return video.readyState >= 1 && !video.videoWidth;
}

/** 编码问题要在**用户点「开始发帧」之前**就说出来，别等他发现自己空等了。 */
function warnIfUndecodable() {
  if (!state.frameWarn && decodingBroken()) {
    state.frameWarn = CODEC_WARN;
    setFrameMsg(state.frameWarn, true);
  }
}

async function grabAndSend(source) {
  if (!video.videoWidth || video.paused) {
    if (!state.frameWarn) {        // 只报一次，别每拍刷屏
      // ★ 「解不出来」和「还没就绪」要分开报：前者等多久都不会好。
      state.frameWarn = decodingBroken() ? CODEC_WARN : "视频未就绪，这一拍跳过";
      setFrameMsg(state.frameWarn, true);
    }
    return;
  }
  state.frameWarn = "";            // 恢复了，警告撤掉
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);
  // ★ 缩略图必须**现在**取：video 一直在往前播，等上传回来再取就变成
  //   「后面某一拍」的画面了，对照测试会直接错位。
  const shot = thumbURL();
  const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", 0.8));
  if (!blob) {
    state.frameWarn = "截帧失败：canvas 没能导出图片";
    setFrameMsg(state.frameWarn, true);
    return;
  }
  let res;
  try {
    res = await sendFrame(blob, source);
  } catch (e) {
    // sendFrame 已经把细节写进右侧日志了；状态行也顶一句 ——
    // 只留在日志里的失败等于没说出来。
    state.frameWarn = `上传失败：${e.message}`;
    setFrameMsg(state.frameWarn, true);
    renderShot({ index: state.frameSeq - 1, source, ok: false, error: e.message }, shot);
    return;
  }
  renderShot(res, shot);
  if (state.loopTimer) setFrameMsg(`已发 ${stats.frames} 帧`);
}

// =====================================================================
// 帧 ↔ 播报 对照区 —— 这一页最要紧的一块
// =====================================================================
//
// 帧源发出去的到底是什么、系统把它读成了什么，必须能**同屏对上**。
// 否则「主页面那条播报是哪儿来的」没人答得上来：是这一帧被读错了？
// 是闸门把它丢了？还是压根没发出去？三种故障在这一栏里长得完全不一样。
//
// ★ 这里显示的是 `POST /v1/frame` 的**回包**（调试用），不是播报界面的
//   渲染路径。播报界面上的每一条仍然只从 WS 来 —— 见 net.js 的约定。

//: 保留多少条。再多就把这一页变成流水账了。
const SHOTS_MAX = 12;

const thumb = document.createElement("canvas");

/** 把**刚上传的那份像素**缩一张小图。
 *  ★ 取的是 canvas 上刚画好的那一帧，而不是「视频现在播到哪儿」——
 *    两者差着好几拍。自己画的画布不会被跨域污染，所以 toDataURL 可用。 */
function thumbURL() {
  const w = 168;
  const h = Math.max(1, Math.round((w * canvas.height) / canvas.width));
  thumb.width = w;
  thumb.height = h;
  thumb.getContext("2d").drawImage(canvas, 0, 0, w, h);
  return thumb.toDataURL("image/jpeg", 0.6);
}

function saidRow(a) {
  const p = document.createElement("p");
  p.className = "shot-said" + (a.passed ? "" : " dropped");
  p.textContent = (a.passed ? "" : "（闸门丢弃）") + a.text;
  return p;
}

function renderShot(res, shot) {
  const list = $("frameCards");
  const big = $("lastShot");
  if (!list || !big) return;

  const label = `${SOURCE_CN[res.source] || res.source} · idx=${res.index}`;

  // 大图：刚发出去的那一帧（和上传给后端的是同一份像素）
  big.src = shot;
  big.hidden = false;
  big.alt = `刚发出的一帧：${label}`;
  $("lastShotCap").textContent = res.ok
    ? `${label} · 产出 ${res.produced} / 放行 ${res.passed}`
    : `${label} · 上传失败：${res.error}`;

  const row = document.createElement("div");
  row.className = "shot" + (res.ok ? "" : " bad");

  const img = document.createElement("img");
  img.src = shot;
  img.alt = `idx=${res.index} 发出的一帧`;
  row.appendChild(img);

  const box = document.createElement("div");
  box.className = "shot-txt";
  const head = document.createElement("p");
  head.className = "shot-head";
  head.textContent = res.ok
    ? `${label} · 产出 ${res.produced} 放行 ${res.passed}` +
      (res.dropped ? `（丢 ${res.dropped}）` : "")
    : `${label} · 上传失败`;
  box.appendChild(head);

  if (res.ok && res.said.length) {
    res.said.forEach((a) => box.appendChild(saidRow(a)));
  } else if (res.ok) {
    const p = document.createElement("p");
    p.className = "shot-said dim";
    p.textContent = "这一帧没有产出播报";
    box.appendChild(p);
  } else {
    const p = document.createElement("p");
    p.className = "shot-said dropped";
    p.textContent = res.error;
    box.appendChild(p);
  }
  row.appendChild(box);

  list.prepend(row);
  while (list.children.length > SHOTS_MAX) list.lastElementChild.remove();
}

function toggleLoop() {
  const btn = $("loopBtn");
  if (state.loopTimer) {
    clearInterval(state.loopTimer.per);
    clearInterval(state.loopTimer.saf);
    state.loopTimer = null;
    btn.textContent = "开始发帧";
    btn.classList.remove("running");
    // ★ 停发后不能把状态行打回「未开始」—— 明明发过 34 帧（实测看到过）。
    //   一句话把「发生过的事」说没，和「静默失效」是同一类毛病。
    setFrameMsg(
      state.frameWarn || (stats.frames ? `已停 · 共发 ${stats.frames} 帧` : "未开始"),
      !!state.frameWarn,
    );
    log("已停止发帧", "dim");
    return;
  }

  state.frameWarn = "";
  video.play().catch((e) => {
    state.frameWarn = `视频无法播放（${e.name}）`;
    setFrameMsg(state.frameWarn, true);
  });
  setFrameMsg(`已发 ${stats.frames} 帧`);
  const per = Math.max(200, +$("perMs").value || 2000);
  const saf = Math.max(100, +$("safMs").value || 1500);

  // 两条流水线各自独立定时 —— 频率不同是设计要求，不是一个循环发两次
  state.loopTimer = {
    per: setInterval(() => grabAndSend("perception"), per),
    saf: setInterval(() => grabAndSend("safety"), saf),
  };
  btn.textContent = "停止发帧";
  btn.classList.add("running");
  log(`开始发帧：感知 ${per}ms / 安全 ${saf}ms`, "ok");
}

// =====================================================================
// 帧源 ② 单张 / 多张图片
// =====================================================================

function sendFiles(files) {
  const imgs = [...files].filter((f) => f.type.startsWith("image/"));
  if (!imgs.length) return;
  imgs.forEach((f) => sendFrame(f, "perception"));  // 图片当场景描述喂第一层
  log(`拖入 ${imgs.length} 张图片 → 感知层`, "ok");
}

// =====================================================================
// 绑定
// =====================================================================

export function initSender() {
  $("loopBtn").onclick = toggleLoop;
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

  // 404 时 <video> 的 error 事件在有些浏览器不冒出来，补一次显式探测
  video.addEventListener("error", onVideoBroken);
  // ★ 解不出来的编码**没有** error 事件（实测 HEVC：readyState=4 却不给画面），
  //   所以元数据到手后再自己瞄一眼 —— 不必等用户点了「开始发帧」才发现。
  video.addEventListener("loadedmetadata", warnIfUndecodable);
  // ★ 还要**立刻**查一次：视频是 <video> 自己开始加载的，模块执行很可能晚于
  //   loadedmetadata —— 那就没有事件可等了。实测 HEVC 素材就是这样：
  //   页面打开时 readyState 已经是 4，光靠监听器的话这条告警要等用户点了
  //   「开始发帧」才出来。
  warnIfUndecodable();
  fetch(video.getAttribute("src"), { method: "HEAD" })
    .then((r) => { if (!r.ok) onVideoBroken(); })
    .catch(() => {});
}

initSender();

// ★ 报到：走到这一行说明模块链执行了、装配也没抛。
//   启动自检（boot-check.js）判「页面起来没有」靠的就是这个事件 —— 见它的头注释。
document.dispatchEvent(new Event("lm:booted"));
