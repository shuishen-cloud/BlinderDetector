/* 播报出口的**端侧那一半** —— 队列、抢占、语速、可用性自检。
 *
 * ★ 为什么这一半必须在前端：`docs/design.md` D11 把「排序、打断、积压保护、
 *   到期不补播」明确划给了端侧 —— 服务端观察不到喇叭，模拟播放状态连出过
 *   两个「永久静默」的 bug，那套状态机因此被拆掉。`Announcement` 里的
 *   `priority` / `interrupt` / `ttl_ms` 就是留给这一半用的。而在此之前，
 *   前端只是无条件 `speechSynthesis.speak()` —— 契约划过来的活没人干。
 *
 * ★ 本文件只解决一件事：**别让耳朵落后于现实**。
 *   中文 TTS 约 250ms/字（D5 自己算过），一条 15 字的播报要念 3.7 秒。
 *   而播报是按场景节奏来的，两秒内来两条是常事 —— 无条件排队的后果是队列
 *   **单调增长**，用户听到的是十几秒前的那个**位置**，人已经走过去了。
 *   听见错的，比什么都没听见更坏。所以：
 *     · p3（跌倒 / 求助）永远抢占，一个字都不许挡在前面；
 *     · 积压超过上限就整批砍掉再念新的 —— 排队那几条的 ttl 也差不多到了；
 *     · 砍掉多少条要记下来（`speechStats`），否则「怎么少听见一句话」查不出来。
 */

import { state } from "./state.js";

/** 语速档位。盲人用户普遍把读屏开到 1.5–2×，这是习惯不是偏好；
 *  而且语速直接影响上面那条积压问题（1× 时积压速度翻倍）。 */
const RATES = [
  { v: 1, label: "1×" },
  { v: 1.25, label: "1.25×" },
  { v: 1.5, label: "1.5×" },
  { v: 2, label: "2×" },
];

/** 最多允许「正在念一条 + 排一条」。再多就说明耳朵已经跟不上了。 */
const MAX_PENDING = 1;

/** 语音列表的等待上限。Chrome 首次 `getVoices()` 一定是空的，要等
 *  `voiceschanged`；等不到才敢下结论 —— 过早宣判「没有中文语音」是冤枉。 */
const VOICES_TIMEOUT_MS = 2500;

let rate = 1;
let pending = 0;            // 排队中的条数（含正在念的那条）
let dropped = 0;            // 为了跟上现实被整批砍掉的条数
let failed = 0;             // 引擎自己念不出来的次数（设备静音 / 被系统打断）
let voice = null;
/** unknown（还没探出来）| ok | no-api | no-voice */
let apiState = "unknown";
let listener = null;

/** 可用性变化时回调一次 —— 按钮上的字要跟着变，不能一直写着「声音：开」。 */
export function onAvailabilityChange(cb) { listener = cb; }

export function availability() { return apiState; }

/** 「这个浏览器根本念不出来」—— 与「用户自己关掉了」是两件事。 */
export function speechUnavailable() {
  return apiState === "no-api" || apiState === "no-voice";
}

export function currentRate() { return rate; }

export function rateLabel() {
  return (RATES.find((r) => r.v === rate) || RATES[0]).label;
}

/** 换一档语速，返回新档位的标签。 */
export function cycleRate() {
  const i = RATES.findIndex((r) => r.v === rate);
  rate = RATES[(i + 1) % RATES.length].v;
  return rateLabel();
}

export function speechStats() {
  return { rate, pending, dropped, failed, apiState, voice: voice?.name || null };
}

/** 开机自检：有没有这个 API、有没有中文嗓音。
 *  ★ 探不到就得**说出去**（按钮改成「声音：不可用」），绝不能亮着「声音：开」
 *    却一点声都没有 —— 那是这个项目最不能接受的假绿。 */
export function initSpeech() {
  if (!("speechSynthesis" in window)) {
    apiState = "no-api";
    return apiState;
  }

  const pick = () => {
    const vs = window.speechSynthesis.getVoices() || [];
    if (!vs.length) return false;               // 还没加载完，先别下结论
    voice = vs.find((v) => /^zh(-|_|$)/i.test(v.lang)) || null;
    apiState = voice ? "ok" : "no-voice";
    listener?.(apiState);
    return true;
  };

  if (!pick()) {
    window.speechSynthesis.addEventListener?.("voiceschanged", pick);
    setTimeout(() => {
      if (apiState !== "unknown") return;
      // 超时了还是不给我们名单：这时候只能按「探不到」处理，
      // 但不能推翻已经确定的结果（pick 成功过就不会走到这里）。
      if (!pick()) { apiState = "no-api"; listener?.(apiState); }
    }, VOICES_TIMEOUT_MS);
  }
  return apiState;
}

/** 念一句。
 *
 *  `priority` / `interrupt` 直接来自 `Announcement`（见 contracts.py 的映射）。
 *  返回 {spoken, reason} —— 调用方**可以**据此知道这句话到底出去了没有，
 *  别让「没念出来」变成一个查不到的状态。
 */
export function speak(text, { priority = 1, interrupt = false } = {}) {
  if (!state.ttsOn || state.paused) return { spoken: false, reason: "off" };
  if (speechUnavailable()) {
    return { spoken: false, reason: apiState === "no-api" ? "no-api" : "no-voice" };
  }

  const synth = window.speechSynthesis;
  const urgent = priority >= 3 || interrupt;

  if (urgent && pending) {
    // 抢占：这一条既比正在念的重要，也一定更新。
    dropped += pending;
    pending = 0;
    synth.cancel();
  } else if (pending > MAX_PENDING) {
    // 积压了。排队那几条的 ttl 基本已经走完 —— 整批砍掉，念最新的。
    dropped += pending;
    pending = 0;
    synth.cancel();
  }

  const u = new SpeechSynthesisUtterance(text);
  u.lang = "zh-CN";
  if (voice) u.voice = voice;
  u.rate = rate;
  pending += 1;
  u.onend = () => { pending = Math.max(0, pending - 1); };
  u.onerror = (e) => {
    pending = Math.max(0, pending - 1);
    // cancel() 会让被取消的那几条也走 onerror，那不是故障，不算数。
    const why = e?.error;
    if (why !== "interrupted" && why !== "canceled") failed += 1;
  };
  synth.speak(u);
  return { spoken: true };
}

/** 把一条 Announcement 念出来 —— 优先级和打断都按它自己带的字段走。 */
export function speakAnnouncement(a) {
  return speak(a.text, { priority: a.priority, interrupt: a.interrupt });
}
