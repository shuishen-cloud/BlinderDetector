/* 产品界面逻辑 —— 交付形态（手机视图）的那一半。
 *
 * 另一半（帧源、请求日志、健康明细、其余路由按钮）是**契约验证用的调试件**，
 * 在 dev.js。这条界线不是随便划的：index.html 里 `#app` 与 `#devPanel`
 * 就是按它分的，JS 跟着分，改一处不必翻遍整个文件。
 *
 * 这一半包括：播报流渲染、紧急全屏、轻提示、语音与震动、筛选/暂停/清空。
 */

import { $, SOURCE_NAMES, EMPTY_HTML, SOURCE_EMERGENCY, SOURCE_SYSTEM } from "./dom.js";
import { state, history, queue, bump } from "./state.js";
import { log } from "./log.js";
import {
  speak, speakAnnouncement, initSpeech, availability, speechUnavailable,
  onAvailabilityChange, cycleRate, rateLabel,
} from "./speech.js";

// 动作确认（dev.js）要走同一条出口 —— 转发一次，别让调用方改 import。
export { speak };

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
  if (a.priority >= 3) bump("critical");

  // ★★ 视觉全屏**只留给需要用户动作的播报**（跌倒二次确认 / 求助）。
  //
  //   安全层的危险障碍物不抢屏。理由是按盲人使用逻辑来的：
  //     · 全屏遮罩对一个看不到屏幕的人**毫无作用** —— 信息全在耳朵和震动里；
  //     · 它唯一的作用是给陪同者看，而实测 36 秒会弹 8 次（全是自行车/来车），
  //       屏幕几乎一直被红色盖着，反而把陪同者要看的信息挡掉了。
  //
  //   危险障碍物的强提示走另外两条通道，本来就够：
  //     耳朵 —— TTS 念出来（speak）
  //     手  —— haptic="double"（见 contracts.announcement 的优先级映射）
  //   而跌倒/求助是**交互**：用户得听到问题、并做动作应答，那时才值得抢屏。
  if (a.source === SOURCE_EMERGENCY && a.priority >= 3) emergency(a);

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
  el.className = `ann p${a.priority}${a.source === SOURCE_SYSTEM ? " sys" : ""}`;
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

  // ★ 顶插之后要把滚动位置交代清楚，否则正在看的人会被顶走：
  //   prepend 让内容整体下移，而 scrollTop 的数值不变。
  //   · 贴顶的人（绝大多数）：钉回 0 —— 那正好是最新那条。
  //   · 翻了页的人：**交给浏览器自己的 scroll anchoring**（默认开着，
  //     专门就是干这个的）。★ 别自己按 offsetHeight 手算补偿 ——
  //     实测那是手动补偿和浏览器补偿打架，反而留下 27px 残余漂移。
  const atTop = feed.scrollTop < 24;
  feed.prepend(el);
  if (atTop) feed.scrollTop = 0;

  history.unshift(el);
  while (history.length > 100) history.pop().remove();

  speakAnnouncement(a);
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

/** 震一下。`el` 可选 —— 不传就只走真马达（动作确认时不需要视觉闪烁）。 */
export function haptic(kind, el) {
  if (el && kind && kind !== "none") {
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

/** 声音开关上的字 —— 注意是**三个**状态，不是一个开关的两态。
 *
 *  ★ 多出来的那个「不可用」是 2026-09-23 加的：浏览器没有语音 API、或系统里
 *    一个中文嗓音都没有时，开关亮着「声音：开」而一点声都不出 —— 这个项目
 *    最不能接受的就是这种假绿。探不到就如实写探不到。
 *
 *  ★ 顺带把 aria-live 一起定了：这一页有**两条**声音通道（自带 TTS 与读屏
 *    TalkBack / VoiceOver），两边同时念同一句话就是双读。所以让 live 跟着
 *    开关走：TTS 在响 → off（读屏别插嘴）；TTS 不响（用户关了，或压根没有）
 *    → polite（读屏接手，此刻它是用户唯一还能听到播报的通道）。 */
function paintTts() {
  const dead = speechUnavailable();
  const live = state.ttsOn && !dead;
  $("ttsTxt").textContent = dead ? "声音：不可用"
    : (state.ttsOn ? "声音：开" : "声音：关");
  $("ttsBtn").classList.toggle("on", live);
  $("ttsBtn").classList.toggle("dead", dead);
  $("ttsBtn").setAttribute("aria-pressed", live ? "true" : "false");
  $("feed").setAttribute("aria-live", live ? "off" : "polite");
  return live;
}

export function setTts(on) {
  state.ttsOn = on;
  paintTts();
  if (!on) window.speechSynthesis?.cancel();
}

/** 语速按钮上的字。★ 光写「语速」不行 —— 用户得知道**现在是几档**，
 *  否则他没法判断按一下之后是否变快了。 */
function paintRate() {
  const label = rateLabel();
  $("rateBtn").textContent = `语速 ${label}`;
  $("rateBtn").setAttribute("aria-label", `播报语速 ${label}，点击换下一档`);
}

/** 本地产生的一条播报（不是服务端来的）—— 断网、降级这类系统状态。
 *
 *  ★ 为什么不能只改那枚小药丸：对看不见屏幕的人，「断开」和「一切都好、
 *    只是暂时没人说话」长得一模一样。所以状态变化必须走**播报流这条同一条
 *    路**：TTS 会念出来，TTS 关掉时 feed 的 aria-live 会交给读屏念，
 *    而它同时留在流里能被翻回来。三条通道，一个来源。
 */
export function localNote(text, { priority = 2, hapticKind = "none", ttl = 15_000 } = {}) {
  const a = {
    id: `local_${Date.now()}`,
    source: SOURCE_SYSTEM,
    priority,
    text,
    ttl_ms: ttl,
    haptic: hapticKind,
    interrupt: priority >= 3,
  };
  // 暂停是「别往墙上贴」，不是「别记下来」—— 和 receive() 一个规矩。
  if (state.paused) { queue.push(a); return a; }
  // ★ 本地播报**不受筛选影响**：把墙筛成「只看紧急」是选择不看次要播报，
  //   不是选择不知道系统哑了。
  paint(a, true);
  return a;
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
    // ★ 文案与 aria-pressed 一起走。原先的文字是「暂停 / 恢复」两个光秃秃的动
    //   词，读屏念出来分不清说的是「现在暂停着」还是「按了会暂停」——
    //   写成「暂停播报 / 继续播报」后的那个动词就是**按下会发生什么**，
    //   和 aria-pressed 里的**当前状态**配合着听才不会搞反。
    $("pauseBtn").textContent = state.paused ? "继续播报" : "暂停播报";
    $("pauseBtn").setAttribute("aria-pressed", state.paused ? "true" : "false");
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

  $("ttsBtn").onclick = () => {
    // 念不出来的时候别让开关假装能开 —— 点一下就**说**一次（看得见 + 听得见），
    // 不能点了没反应。
    if (speechUnavailable()) {
      const why = availability() === "no-api" ? "这个浏览器不支持语音播报" : "系统里没有中文语音，念不出来";
      toast(why, "err");
      log(`声音不可用：${why}`, "err");
      return;
    }
    setTts(!state.ttsOn);
  };

  $("rateBtn").onclick = () => {
    const label = cycleRate();
    paintRate();
    // ★ 换档必须**当场念一句**：语速只能用耳朵判断，光把数字改掉用户没得评估。
    //   这句也顺带证明新档位是通的。
    speak("语速已切换。前方有台阶，请注意。", { priority: 2, interrupt: true });
    log(`播报语速 → ${label}`, "dim");
  };

  // 开机自检语音能力 —— 探不到就得改开关上的字（见 paintTts）。
  // ★ 先挂回调再探：探到结果的那一刻（同步或异步）都要**立刻**反映到开关上。
  onAvailabilityChange(paintTts);
  initSpeech();
  paintRate();

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
