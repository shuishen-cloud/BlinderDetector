/* 语音输入 —— 目的地那一格（「按住说话」）。
 *
 * ★ 为什么目的地这一格要单独一个模块：它是这一页**唯一**需要用户输入文字
 *   的地方，而用户是看不见屏幕的人 —— 打字意味着调出屏幕键盘、在一个看不见
 *   的输入框里逐字确认，这是整页最难受的一步。按住说话把这一步变成一次
 *   按住 + 一句口头话。
 *
 * ★ 交互是「按住 → 松开」，不是「点一下开始、再点一下结束」：
 *   两个状态要用户用耳朵记住「我现在是开着还是关着」，而按住说话的物理状态
 *   本身就是状态 —— 手指按着就是在听。松开即结束，不存在「忘了关」。
 *
 * ★ 松开之后**不新增任何请求路径**：识别出的文本写进 `#dest`（和打字完全
 *   同一个落点），再调 `nav.js::submitRoute()` —— 打字敲回车走的是**同一个
 *   函数**。换句话说，语音只换掉「怎么把字填进去」，后面的路一步没变。
 *
 *   （2026-09-23 之前这里点的是「开始导航」那个按钮。那个按钮现在已经删掉：
 *   它对用户完全多余，而留着它就等于说「这条路上有一个必须存在的按钮」。
 *   把路抽成 submitRoute() 之后，两条输入方式直连同一个出口。）
 *
 * ★ 三通道反馈（和紧急按钮一个规矩）：眼睛看按钮文字、耳朵听状态、手指感震动。
 *   录的时候听不见「现在在录」是最糟的 —— 用户会对着空气说，然后以为系统坏了。
 */

import { $ } from "./dom.js";
import { log } from "./log.js";
import { toast, speak, haptic } from "./ui.js";
// ★ 导航请求的**唯一**出口（打字那条路也走它）—— 见 nav.js 的头注释。
import { submitRoute } from "./nav.js";

/** 浏览器给的构造器。Chrome / Edge 有前缀版，Safari 也有；Firefox 至今没有。 */
function RecogCtor() {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

export function voiceInputAvailable() { return !!RecogCtor(); }

/** 一句话说清「为什么用不了」——不能只说「不支持」。 */
function whyUnavailable() {
  if (!RecogCtor()) return "这个浏览器不支持语音输入（Firefox 至今没有），请直接打字";
  if (!window.isSecureContext) return "语音输入需要 HTTPS 或 localhost，请换地址打开";
  return "语音输入不可用，请直接打字";
}

const LABELS = {
  idle: "按住说话",
  listening: "正在听…松开结束",
  busy: "识别中…",
  dead: "语音不可用",
};

function paint(btn, state) {
  $("talkTxt").textContent = LABELS[state];
  btn.classList.toggle("listening", state === "listening");
  btn.classList.toggle("dead", state === "dead");
  btn.setAttribute("aria-label", LABELS[state]);
}

/** 松开之后：把文本交给**原来那条路**（`nav.js::submitRoute`，打字那条也走它）。
 *
 *  ★ 不在这里 fetch：路由、坐标、错误处理全在 submitRoute 里。这里多写一份
 *    就等于把那条路复制成两份，改一处另一处会静默不一致 —— 这个项目刚在
 *    `.video-strip` 上栽过同类跟头。
 */
function handOver(text) {
  $("dest").value = text;                 // ← 和打字完全同一个落点
  log(`语音目的地：${text}`, "ok");
  toast(`目的地：${text}`, "ok");
  haptic("short");
  // 先报一遍**听到了什么**再出发：识别错了的话，用户此刻就能开口纠正，
  // 而不是等路线播报出来才发现去的是别的地方。
  speak(`目的地：${text}`, { priority: 2, interrupt: true });

  // 让「目的地：X」这句话先出去，再让请求跑起来（路线播报随后接上）。
  setTimeout(() => submitRoute(), 0);
}

/** 没听清 / 没权限：都要**说出来**，不能安安静静地什么都不做。 */
function complain(msg, kind = "err") {
  toast(msg, kind);
  log(`语音输入：${msg}`, "err");
  speak(msg, { priority: 2, interrupt: true });
  haptic("double");
}

export function initVoiceInput() {
  const btn = $("talkBtn");
  const ctor = RecogCtor();

  if (!ctor) {
    // ★ 不能假装能用（和「声音：不可用」同一个规矩）：按钮上直接写实话，
    //   点一下给出原因 —— 用户会知道该去打字，而不是对着它按半天。
    paint(btn, "dead");
    btn.onclick = () => complain(whyUnavailable());
    return;
  }

  let rec = null;
  let listening = false;    // 手指按着
  let heard = false;        // 这一次收到过结果没有

  const finish = () => {
    listening = false;
    rec = null;
    paint(btn, "idle");
  };

  const start = (e) => {
    e.preventDefault();
    if (listening) return;

    let r;
    try {
      r = new ctor();
    } catch {
      complain(whyUnavailable());
      return;
    }
    rec = r;
    heard = false;
    listening = true;

    r.lang = "zh-CN";
    r.continuous = false;        // 一句话，松开就完
    r.interimResults = false;    // 只要最终结果：中间结果会让人以为已经听到了
    r.maxAlternatives = 1;

    r.onresult = (ev) => {
      const text = (ev.results?.[0]?.[0]?.transcript || "").trim();
      if (!text) return;         // 空结果交给 onend 去报「没听清」
      heard = true;
      paint(btn, "busy");
      handOver(text);
    };

    r.onerror = (ev) => {
      heard = true;              // 已经报过错了，别让 onend 再报一次
      const why = ev?.error;
      // ★ 收尾**不能只靠 onend**：规范说 onerror 之后也会派发 onend，但
      //   实测（2026-09-23，用桩引擎跑的）只要引擎少发一次 onend，按钮就会
      //   永远停在「识别中…」—— 而用户以为自己还在录。所以两条路都收尾，
      //   onend 再来一次也是幂等的（`missed` 那时已经不成立）。
      finish();
      if (why === "no-speech") complain("没听清，请再按住说一次");
      else if (why === "not-allowed" || why === "service-not-allowed") {
        complain("麦克风没被允许。请允许麦克风，或用 HTTPS / localhost 打开");
      } else if (why === "aborted") {
        // 我们自己 stop() 引起的，不算故障 —— 但这时通常也没等到结果
        complain("没听清，请再按住说一次");
      } else complain(`语音识别失败：${why || "未知原因"}`);
    };

    r.onend = () => {
      // ★ onend 一定会来（成功、没说话、被打断都来）。**它是唯一的收尾点** ——
      //   松手之后按钮不能永远停在「正在听…」，否则用户会以为还在录。
      const missed = listening && !heard;
      finish();
      if (missed) complain("没听清，请再按住说一次");
    };

    paint(btn, "listening");
    haptic("short");
    // 按下去立刻出声说「在听了」：看不见屏幕的人只有这一条线索，
    // 而且它同时确认了「麦克风被允许了」。
    speak("请说目的地", { priority: 2, interrupt: true });
    try {
      r.start();
    } catch {
      finish();
      complain("麦克风启动失败，请稍后再试");
    }
  };

  const stop = () => {
    if (!listening || !rec) return;
    // 松手**立刻**给回执，不等识别结果 —— 弱网/慢机器下 onend 可能还要一会儿。
    haptic("short");
    paint(btn, "busy");
    try {
      rec.stop();               // stop 会让引擎交出最终结果
    } catch {
      finish();
    }
  };

  btn.addEventListener("pointerdown", (e) => {
    // ★ 捕获指针：手指按住后滑出按钮、再松开，也要算「松开」。
    //   不捕获的话按钮会一直停在「正在听…」，而用户以为自己已经松手了。
    try { btn.setPointerCapture(e.pointerId); } catch { /* 老浏览器没有，忽略 */ }
    start(e);
  });
  btn.addEventListener("pointerup", stop);
  btn.addEventListener("pointercancel", () => {
    // 系统把指针抢走了（来电、手势返回）——按「没听清」收尾，别卡住。
    if (listening) { listening = false; paint(btn, "idle"); }
  });

  // 键盘：没有「按住」的语义，退化成「按空格开始、松空格结束」——
  // 和外接键盘/开关设备用户的预期一致。
  btn.addEventListener("keydown", (e) => {
    if (e.repeat) return;
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); start(e); }
  });
  btn.addEventListener("keyup", (e) => {
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); stop(); }
  });
}
