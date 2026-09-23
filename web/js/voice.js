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
 * ★★ 识别有**两条通道**，这是本模块最要紧的一件事（2026-09-23 加）★★
 *
 *   甲·服务端（默认）：录音 → `POST /v1/asr` → 文本。自己家的，看得见日志、
 *       换得掉实现、失败能如实降级。**优先用它。**
 *   乙·浏览器：`SpeechRecognition`（走 Chrome→Google / Safari→Apple 的云）。
 *       甲不可用时才用；它的 `error` 我们无能为力（`network` 就是它的原话）。
 *
 *   为什么不让浏览器那条当默认：实测在国内网络 / Chromium 裸构建下它必然报
 *   `network`，而我们既看不到失败率也换不掉它 —— 这一环等于不受控。服务端
 *   识别把这一环拿回自己手里，代价是松手后多一个往返（`识别中…`）。
 *
 * ★ 自己念的提示音会被自己录进去：服务端通道是**真录音**，「请说目的地」
 *   这句提示音和用户的声音一起进了麦克风，所以 `stripPrompt()` 会把识别结果里
 *   混进来的提示语削掉。根上的解法是提示音换成短哔声（或者真机上的回声消除），
 *   这里先按「说得准」这一条把它削干净 —— 让用户念一遍提示语不是他的错。
 *
 * ★ 三通道反馈（和紧急按钮一个规矩）：眼睛看按钮文字、耳朵听状态、手指感震动。
 *   录的时候听不见「现在在录」是最糟的 —— 用户会对着空气说，然后以为系统坏了。
 */

import { $ } from "./dom.js";
import { log } from "./log.js";
import { toast, speak, haptic, localNote } from "./ui.js";
import { postAsr } from "./net.js";
// ★ 导航请求的**唯一**出口（打字那条路也走它）—— 见 nav.js 的头注释。
import { submitRoute } from "./nav.js";

//: 服务端识别要的采样率与声道。语音识别的标准输入，10 秒 ≈ 320 KB。
//: 44.1k 立体声 10 秒是 1.7 MB —— 弱网上多出来的每一秒都是失败率。
const TARGET_RATE = 16000;

//: 单次录音的**上限**。按住不放不能录到天荒地老（文件大小与识别延迟都会失控）。
//: 到点自动停并按这一段落识别，同时**说出来** —— 静默截断等于偷偷丢用户的话。
const MAX_RECORD_MS = 15_000;

/** 浏览器给的构造器。Chrome / Edge 有前缀版，Safari 也有；Firefox 至今没有。 */
function RecogCtor() {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

/** 能不能走服务端那条（录音 + 解码 + 重采样，三样都得有）。 */
function recorderAvailable() {
  return !!(
    window.MediaRecorder
    && navigator.mediaDevices?.getUserMedia
    && (window.AudioContext || window.webkitAudioContext)
  );
}

export function voiceInputAvailable() { return recorderAvailable() || !!RecogCtor(); }

/** 一句话说清「为什么用不了」——不能只说「不支持」。 */
function whyUnavailable() {
  if (!window.isSecureContext) return "语音输入需要 HTTPS 或 localhost，请换地址打开";
  if (RecogCtor()) return "这个浏览器没有可用的语音识别，请直接打字";
  return "这个浏览器不支持语音输入（Firefox 至今没有），请直接打字";
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
  $("talkBtn").classList.remove("warn");   // 认出来了就把降级标记摘掉
  $("dest").value = text;                  // ← 和打字完全同一个落点
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

/** 削掉识别结果里混进来的**我们的提示音**。
 *
 *  ★ 服务端通道是真录音：按下时念的「请说目的地」与用户的声音一起进了麦克风，
 *    所以结果可能是「请说目的地，我要去太原站」。用户没有念错什么，是我们
 *    把自己的声音录了进去 —— 削掉是本分，不是宽容。
 *  ★ 只削**开头**：用户句中真的说了这几个字（比如让朋友复述）不该被误伤。
 */
const PROMPT_WORDS = /^(请说目的地|请说|说吧|说话)[，,。.、！!\s]*/;
export function stripPrompt(text) {
  return String(text || "").replace(PROMPT_WORDS, "").trim();
}

/** Float32 [-1,1] 单声道 → 16-bit PCM WAV。标准 44 字节头。 */
function encodeWav(samples, rate) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const put = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
  put(0, "RIFF");  v.setUint32(4, 36 + samples.length * 2, true);
  put(8, "WAVE");  put(12, "fmt ");
  v.setUint32(16, 16, true);            // fmt 块长度
  v.setUint16(20, 1, true);             // PCM
  v.setUint16(22, 1, true);             // 单声道
  v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true);      // 字节率
  v.setUint16(32, 2, true);             // 块对齐
  v.setUint16(34, 16, true);            // 位深
  put(36, "data"); v.setUint32(40, samples.length * 2, true);
  let off = 44;
  for (let i = 0; i < samples.length; i++, off += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

/** 录下来的音频（webm/opus、m4a…）→ 16k 单声道 WAV。
 *
 *  ★ 为什么要转：服务端那台厂商只认 wav / mp3 这类**文件格式**。浏览器录出来的
 *    是 webm/opus（Chrome）或 mp4/aac（Safari），不能直接发。转码放端侧做还有
 *    两个好处：省一次上传（16k 单声道比 44.1k 立体声小十倍），以及不必在后端
 *    引入 ffmpeg 这种重依赖（Termux 上装不动）。
 *  ★ 重采样交给 `OfflineAudioContext` —— 浏览器的重采样比自己写线性插值靠谱，
 *    而且顺手把立体声下混成单声道。
 */
async function blobToWav(blob) {
  const AC = window.AudioContext || window.webkitAudioContext;
  const ctx = new AC();
  try {
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
    const frames = Math.max(1, Math.ceil(decoded.duration * TARGET_RATE));
    const off = new OfflineAudioContext(1, frames, TARGET_RATE);
    const src = off.createBufferSource();
    src.buffer = decoded;
    src.connect(off.destination);
    src.start();
    const mono = await off.startRendering();
    return encodeWav(mono.getChannelData(0), TARGET_RATE);
  } finally {
    // ★ 必须关：每按一次开一个 AudioContext，不关的话浏览器会在第 6 个左右
    //   开始拒绝（每个页面有上限），而现象是「按第 N 次就没反应了」。
    try { await ctx.close(); } catch { /* 老实现不返回 promise，忽略 */ }
  }
}

/** 服务端给的降级原因 → 说得出口的话。
 *
 *  ★ 一律不带厂商原文：那是给排查的人看的（进 `log`），念给用户听只会变成
 *    一串他不该负责的细节。这里是「哪件事坏了」的短句。
 */
const ASR_REASONS = {
  asr_unavailable: "没有接（ASR=none）",
  asr_misconfigured: "配置没配齐（少 key 或地址）",
  asr_not_registered: "配置的名字没注册过",
  asr_failed: "调用失败",
  asr_error: "出错",
};

/** 永久性不可用（配置问题）—— 换引擎；暂时性失败只重试。
 *
 *  ★ `asr_misconfigured` 也算永久：`.env` 不会自己变好。它和 `asr_unavailable`
 *    的措辞**必须分开** —— 一个该去查 `.env`，一个本来就该退回浏览器那条，
 *    合成一句会让人去查一个根本没坏的东西。
 */
function isPermanent(reason) {
  return reason === "asr_unavailable"
    || reason === "asr_misconfigured"
    || reason === "asr_not_registered";
}

/** 已经连不上过一次了 —— 之后只说短句，别再念一遍整段解释。 */
let netWarned = false;

/** 浏览器那条通道连不上**浏览器厂商的云服务**（`error === "network"`）。
 *
 *  ★ 起因（2026-09-23，实测）：Chromium 裸构建里不带语音服务，或到 Google
 *    的网络不通时，长按松开后 `error` 就是字符串 `network`。原兜底分支把它
 *    原样念出来 ——「语音识别失败：network」，用户听到一个英文单词，而且
 *    **极容易理解成「本项目的后端/网络断了」**，转头去查一个根本没坏的东西。
 *    浏览器厂商的云服务 ≠ 本项目的后端，这两件事必须分开说
 *    （和 `/v1/health` 给不同降级原因分不同措辞是同一条规矩）。
 *
 *  ★ 走 localNote 而不是 speak：它同时喂 TTS、读屏（TTS 关时）和播报流 ——
 *    这条解释要能**翻回来重看**，因为它说了「该改用打字」这件以后还有用的事。
 *
 *  ★ 不把按钮切成「语音不可用」（那是给「一条通道都没有」留的）：
 *    网络是会回来的，标成不可用等于**假红**，用户从此不再试一个其实能用的
 *    功能。只标琥珀 + 说清怎么绕过去（打字），并且允许继续按住重试。
 */
function complainNetwork() {
  const btn = $("talkBtn");
  btn.classList.add("warn");
  if (netWarned) {
    localNote("语音还是连不上，请直接打字。", { priority: 2, hapticKind: "double" });
    return;
  }
  netWarned = true;
  log("语音输入：浏览器厂商的云语音服务连不上（error=network）", "err");
  localNote(
    "语音识别靠浏览器厂商的云服务，现在连不上 —— 不是本项目的后端。请直接打字。",
    { priority: 2, hapticKind: "double" },
  );
}

export function initVoiceInput() {
  const btn = $("talkBtn");

  // ★ 引擎优先级：服务端 → 浏览器 → 没有。见文件头「两条通道」。
  let engine = recorderAvailable() ? "server" : (RecogCtor() ? "browser" : null);

  if (!engine) {
    // ★ 不能假装能用（和「声音：不可用」同一个规矩）：按钮上直接写实话，
    //   点一下给出原因 —— 用户会知道该去打字，而不是对着它按半天。
    paint(btn, "dead");
    btn.onclick = () => complain(whyUnavailable());
    return;
  }

  let listening = false;   // 手指按着
  let heard = false;       // 这一次收到过结果没有
  let serverFails = 0;     // 服务端连续失败次数（第二次起改口径）

  // ---- 引擎甲：服务端（录音 → /v1/asr）----
  let stream = null;       // 麦克风流。**录完立刻关** —— 不留常驻录音
  let rec = null;          // MediaRecorder
  let chunks = [];
  let timedOut = false;    // 这一次是不是录满上限被截断的

  const closeStream = () => {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
    rec = null;
  };

  const finish = () => {
    listening = false;
    rec = null;
    paint(btn, "idle");
  };

  /** 松开之后：转码 → 上传 → 拿文本。整条异步链路上的每一步都要收尾。 */
  async function uploadRecording() {
    const mime = chunks[0]?.type || "audio/webm";
    const blob = new Blob(chunks, { type: mime });
    chunks = [];
    paint(btn, "busy");

    if (timedOut) {
      timedOut = false;
      // ★ 录音已经停了才说这句 —— 早一步就会被自己录进去。
      localNote(`录满 ${MAX_RECORD_MS / 1000} 秒，先按到这里识别。`, { priority: 2 });
    }

    let wav;
    try {
      wav = await blobToWav(blob);
    } catch (e) {
      log(`语音输入：录音转码失败（${e?.name || e}）`, "err");
      complain("这段录音没能处理，请再按住说一次");
      paint(btn, "idle");
      return;
    }

    const fd = new FormData();
    // ★ 用 `append(name, blob, filename)` 而不是 new File(...)：少一处
    //   `File` 构造器的兼容性赌注（老 Safari 没有），浏览器会按文件名补 mime。
    fd.append("audio", wav, "voice.wav");
    fd.append("format", "wav");

    let b;
    try {
      b = await postAsr(fd);
    } catch (e) {
      // 网络断了 / 后端没起来。和「服务端说它不会识别」是两件事，措辞分开。
      log(`语音输入：上传失败 ${e.message}`, "err");
      complain(`语音上传失败：${e.message}`);
      paint(btn, "idle");
      return;
    }

    const reason = b.degraded?.[0]?.reason || "";
    const text = stripPrompt(b.text);

    if (b.degraded?.length) {
      // ★ 服务端**没在听**（≠ 没听清）。原文进日志，人话念给耳朵。
      log(`语音输入：服务端识别不可用 ${reason} ${b.degraded[0]?.detail || ""}`, "err");
      degrade(reason);
      return;
    }
    if (!text) {
      complain("没听清，请再按住说一次");
      paint(btn, "idle");
      return;
    }
    serverFails = 0;
    paint(btn, "idle");
    handOver(text);
  }

  /** 服务端那条不通：永久性问题就换通道，暂时性失败只重试。 */
  function degrade(reason) {
    const why = ASR_REASONS[reason] || "不可用";
    if (isPermanent(reason) && RecogCtor()) {
      // ★ 换引擎要**说出来**，否则用户按住之后听到的反馈换了一套，他不知道为什么。
      engine = "browser";
      netWarned = false;                 // 新通道，那条解释该重新说一遍
      btn.classList.add("warn");
      paint(btn, "idle");
      localNote(
        `服务端语音识别${why}，已切到浏览器识别。请再按住说一次。`,
        { priority: 2, hapticKind: "double" },
      );
      return;
    }
    serverFails += 1;
    paint(btn, "idle");
    // 第二次起加一句「可以打字」——一次失败不值得劝人放弃，一直失败就该劝了。
    complain(serverFails >= 2
      ? `语音识别${why}，请直接打字`
      : `这次没认出来（${why}），请再按住说一次`);
  }

  const startServer = async (e) => {
    e.preventDefault();
    if (listening) return;
    listening = true;
    heard = false;
    timedOut = false;
    haptic("short");
    paint(btn, "listening");

    let s;
    try {
      s = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      // 权限被拒 / 没有设备 / 被系统占用 —— 三件事的措辞不同，分开说。
      const msg = err?.name === "NotAllowedError"
        ? "麦克风没被允许，请允许麦克风（或用 HTTPS / localhost 打开）"
        : err?.name === "NotFoundError" ? "没找到麦克风，请直接打字"
        : `麦克风打不开（${err?.name || "未知原因"}），请直接打字`;
      finish();
      complain(msg);
      return;
    }

    // ★ 第一次按住会弹权限框，用户往往在框还没点完就松手了。这时**不能**假装
    //   在听：把流关掉，如实说「已就绪，请再来一次」—— 下一次就是秒开。
    if (!listening) {
      s.getTracks().forEach((t) => t.stop());
      paint(btn, "idle");
      localNote("麦克风已就绪，请再按住说一次。", { priority: 2 });
      return;
    }

    stream = s;
    chunks = [];
    rec = new MediaRecorder(s);
    rec.ondataavailable = (ev) => { if (ev.data && ev.data.size) chunks.push(ev.data); };
    rec.onstop = () => { closeStream(); uploadRecording(); };
    try {
      rec.start();
    } catch (err) {
      closeStream();
      finish();
      complain("开始录音失败，请直接打字");
      return;
    }
    // 按下去立刻出声说「在听了」：看不见屏幕的人只有这一条线索，而且它同时
    // 确认了「麦克风被允许了」。（这句会被自己录进去 —— 见 stripPrompt。）
    speak("请说目的地", { priority: 2, interrupt: true });
    setTimeout(() => {
      if (rec && rec.state === "recording") {
        timedOut = true;
        rec.stop();
        listening = false;
      }
    }, MAX_RECORD_MS);
  };

  const stopServer = () => {
    if (!rec) return;                        // 麦克风还没开（在等权限框）
    if (rec.state !== "recording") return;
    haptic("short");
    paint(btn, "busy");
    try {
      rec.stop();                            // 停 → onstop → 上传
    } catch {
      closeStream();
      paint(btn, "idle");
    }
  };

  /** 指针被系统抢走（来电、手势返回）—— 丢掉这一段，别卡住也别上传。 */
  const cancelServer = () => {
    if (rec && rec.state === "recording") {
      rec.onstop = null;                     // 不上传
      try { rec.stop(); } catch { /* 已经停了 */ }
    }
    closeStream();
  };

  // ---- 引擎乙：浏览器（SpeechRecognition）----
  let brec = null;

  const startBrowser = (e) => {
    e.preventDefault();
    if (listening) return;

    let r;
    try {
      r = new (RecogCtor())();
    } catch {
      complain(whyUnavailable());
      return;
    }
    brec = r;
    heard = false;
    listening = true;

    r.lang = "zh-CN";
    r.continuous = false;        // 一句话，松开就完
    r.interimResults = false;    // 只要最终结果：中间结果会让人以为已经听到了
    r.maxAlternatives = 1;

    r.onresult = (ev) => {
      const text = stripPrompt((ev.results?.[0]?.[0]?.transcript || "").trim());
      if (!text) return;         // 空结果交给 onend 去报「没听清」
      heard = true;
      paint(btn, "busy");
      finish();
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
      } else if (why === "network") {
        // ★ 单独一条：它是**浏览器厂商的云服务**连不上，不是本项目的后端。
        complainNetwork();
      } else if (why === "audio-capture") {
        complain("没找到麦克风，请直接打字");
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
    speak("请说目的地", { priority: 2, interrupt: true });
    try {
      r.start();
    } catch {
      finish();
      complain("麦克风启动失败，请稍后再试");
    }
  };

  const stopBrowser = () => {
    if (!listening || !brec) return;
    // 松手**立刻**给回执，不等识别结果 —— 弱网/慢机器下 onend 可能还要一会儿。
    haptic("short");
    paint(btn, "busy");
    try {
      brec.stop();               // stop 会让引擎交出最终结果
    } catch {
      finish();
    }
  };

  // ---- 统一的按住 / 松开 ----

  btn.addEventListener("pointerdown", (e) => {
    // ★ 捕获指针：手指按住后滑出按钮、再松开，也要算「松开」。
    //   不捕获的话按钮会一直停在「正在听…」，而用户以为自己已经松手了。
    try { btn.setPointerCapture(e.pointerId); } catch { /* 老浏览器没有，忽略 */ }
    if (engine === "server") startServer(e);
    else startBrowser(e);
  });

  btn.addEventListener("pointerup", () => {
    if (engine === "server") stopServer();
    else stopBrowser();
  });

  btn.addEventListener("pointercancel", () => {
    // 系统把指针抢走了（来电、手势返回）——收尾，别卡住。
    if (engine === "server") cancelServer();
    listening = false;
    brec = null;
    paint(btn, "idle");
  });

  // 键盘：没有「按住」的语义，退化成「按空格开始、松空格结束」——
  // 和外接键盘/开关设备用户的预期一致。
  const keyStart = (e) => {
    if (e.repeat) return;
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      if (engine === "server") startServer(e); else startBrowser(e);
    }
  };
  const keyStop = (e) => {
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      if (engine === "server") stopServer(); else stopBrowser();
    }
  };
  btn.addEventListener("keydown", keyStart);
  btn.addEventListener("keyup", keyStop);
}
