/* 产品界面逻辑 —— 交付形态（手机视图）的那一半。
 *
 * 另一半（帧源、请求日志、健康明细、其余路由按钮）是**契约验证用的调试件**，
 * 在 dev.js。这条界线不是随便划的：index.html 里 `#app` 与 `#devPanel`
 * 就是按它分的，JS 跟着分，改一处不必翻遍整个文件。
 *
 * 这一半包括：播报流渲染、紧急全屏、轻提示、语音与震动、筛选/暂停/清空。
 */

import { $, SOURCE_NAMES, EMPTY_HTML } from "./dom.js";
import { state, history, queue, bump } from "./state.js";
import { log } from "./log.js";

// =====================================================================
// 轻提示 —— 手机视图里没有请求日志，动作必须有可见反馈
// =====================================================================

let toastTimer = null;

export function toast(text, kind = "") {
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

export function hideEmergency() {
  if (emgTimer) { clearTimeout(emgTimer); emgTimer = null; }
  const box = $("emg");
  if (box) box.hidden = true;
}

// =====================================================================
// 播报渲染
// =====================================================================

export function receive(a) {
  bump("total");

  // ★ 紧急播报**不排队**：暂停是「别往墙上贴」，不是「别告诉我出事了」。
  if (a.priority >= 3) {
    bump("critical");
    emergency(a);
  }

  // 暂停只是「不往墙上贴」，不拦数据 —— 统计照涨，恢复时补上，
  // 否则暂停期间发生的事在调试台里就凭空消失了。
  if (state.paused) { queue.push(a); return; }
  // 被筛掉的也留在 history 里，切回「全部」时能重新看见；
  // 但已经不播报了，就不该再出声、再震动。
  paint(a, a.priority >= state.filterMin);
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
// 语音播报
// =====================================================================

function speak(text) {
  if (!state.ttsOn || state.paused || !window.speechSynthesis) return;
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "zh-CN";
  speechSynthesis.speak(u);
}

export function setTts(on) {
  state.ttsOn = on;
  $("ttsTxt").textContent = on ? "播报：开" : "播报：关";
  $("ttsBtn").classList.toggle("on", on);
  $("ttsBtn").setAttribute("aria-pressed", on ? "true" : "false");
  if (!on) window.speechSynthesis?.cancel();
}

// =====================================================================
// 事件绑定
// =====================================================================

export function initUI() {
  $("filters").onclick = (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    [...e.currentTarget.children].forEach((c) => c.classList.toggle("on", c === chip));
    state.filterMin = +chip.dataset.min;
    for (const el of history) el.hidden = +el.dataset.priority < state.filterMin;
    log(`筛选：${chip.textContent}`, "dim");
  };

  $("pauseBtn").onclick = () => {
    state.paused = !state.paused;
    $("pauseBtn").textContent = state.paused ? "恢复" : "暂停";
    if (!state.paused && queue.length) {
      log(`恢复，补上积压的 ${queue.length} 条`, "dim");
      queue.splice(0).forEach((a) => paint(a, a.priority >= state.filterMin));
    }
  };

  $("clearBtn").onclick = () => {
    history.splice(0).forEach((el) => el.remove());
    $("feed").innerHTML = EMPTY_HTML;
    log("已清空播报流", "dim");
    toast("已清空");
  };

  $("ttsBtn").onclick = () => setTts(!state.ttsOn);
  // 默认**开**：这一屏的全部价值就是出声。浏览器在首次交互前可能拒绝
  // 朗读，用户碰一下屏幕就正常了 —— 不必为此把默认值改成「关」。
  setTts(true);

  $("emgOk").onclick = hideEmergency;
  // ★ 整块遮罩都可点关闭（见 index.html 的 .emg-hint）：黑暗区域看起来
  //   不可点，所以页面上必须写明这件事。
  $("emg").onclick = hideEmergency;

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
}
