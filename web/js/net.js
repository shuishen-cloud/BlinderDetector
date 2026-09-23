/* 传输层 —— 与后端的通道，全在这里。
 *
 *   ① POST /v1/frame     上传一帧（multipart）—— 摄像头 / 视频抽帧 / 图片共用
 *   ② WS   /v1/stream    播报唯一入口（★ 页面只从这里拿播报，见 ui.js）
 *   ③ 其余 POST 路由      求助 / 取消 / 跌倒 / 导航 / 推进时钟
 *   ④ POST /v1/asr       上传一段录音换识别文本（multipart）—— 见 postAsr
 *
 * ★ 播报为什么不直接拿 HTTP 响应渲染：端侧真实拿播报的通道只有 /v1/stream
 *   一条。调试台要是绕过它渲染，就等于「用一条生产不存在的路径验证契约」
 *   —— WS 断了你也看不出来。所以哪怕多一次往返，渲染也一律走 WS。
 */

import { $ } from "./dom.js";
import { state, stats, bump } from "./state.js";
import { log } from "./log.js";

/** 闸门结果。响应里的 `arbiter.sent` 是**累计**放行过的 id 集合，
 *  所以只能拿「本次产出的 id 在不在里面」来数，不能拿集合长度当本次数量。
 *  /v1/emergency/tick 的回包是平铺的 {sent:[...]}，一并兼容。 */
export function gateSummary(b) {
  const sentIds = new Set(b.arbiter?.sent ?? b.sent ?? []);
  const produced = b.announcements ?? [];
  const passed = produced.filter((a) => sentIds.has(a.id)).length;
  // ★ sentIds 也一并交出去：调用方要**逐条**标出「这条放行了没有」。
  //   让它自己再解析一遍 `arbiter.sent`，等于把上面这行约定复制成两份。
  return { produced: produced.length, passed, dropped: produced.length - passed, sentIds };
}

/** 上传一帧。三个帧源（视频抽帧 / 摄像头 / 单张图片）共用这一个出口。
 *
 * ★ 返回值**只给调试用**：帧源模拟器拿它把「这一帧被读成了什么」摆在
 *   同一张卡片上，好和播报界面上的播报逐条对照。
 *   它**不是**第二条渲染路径 —— 播报界面上的每一条仍然只从
 *   `WS /v1/stream` 来（见文件头那条约定），否则 WS 断了你也看不出来。
 */
export async function sendFrame(blob, source) {
  const index = state.frameSeq++;
  const fd = new FormData();
  fd.append("image", blob, `frame_${index}.jpg`);
  fd.append("source", source);
  fd.append("frame_id", `web_${index}`);
  fd.append("ts", String(Date.now()));
  fd.append("extra", JSON.stringify({ index, origin: "web-console" }));

  try {
    const r = await fetch("/v1/frame", { method: "POST", body: fd });
    const b = await r.json();
    if (!r.ok) {
      const error = b.error || `HTTP ${r.status}`;
      log(`${source} 上传失败 ${r.status}：${b.error || ""}`, "err");
      return { index, source, ok: false, error, produced: 0, passed: 0, dropped: 0, said: [] };
    }

    bump("frames");
    const g = gateSummary(b);
    bump("dropped", g.dropped);
    log(`${source} idx=${index} 产出 ${g.produced} 条，放行 ${g.passed} 条` +
        (g.dropped ? `，丢弃 ${g.dropped} 条（重复/过期）` : ""), g.dropped ? "dim" : "ok");
    return {
      index, source, ok: true,
      produced: g.produced, passed: g.passed, dropped: g.dropped,
      // 逐条带上「放行没有」—— 被闸门丢掉的那几条也要显示出来，
      // 否则「这一帧明明看到台阶却没播报」会查不出是闸门吃的还是模型没看出来。
      said: (b.announcements ?? []).map((a) => ({
        id: a.id, source: a.source, priority: a.priority, text: a.text,
        passed: g.sentIds.has(a.id),
      })),
    };
  } catch (e) {
    log(`${source} 上传异常：${e.message}`, "err");
    throw e;   // 交给调用方决定要不要提示用户（帧源卡片会显示状态）
  }
}

/** 上传一段录音，换回识别文本（`POST /v1/asr`）。
 *
 *  ★ 这是**唯一一条不返回播报**的业务请求：识别出的文本是**数据**（要填进
 *    目的地那一格），不是要播出去的话 —— 见 `app/api/speech.py` 的头注释。
 *    所以这里不像 postJson 那样回报闸门结果，而是把 `{text, impl, degraded}`
 *    原样交回调用方，由它决定「念什么」。
 *
 *  ★ 失败也回 200：`degraded` 非空 = 服务端**没在听**（原因在 reason 里），
 *    `text` 为空且 `degraded` 为空 = 听到了但没听清。两者的措辞完全不同，
 *    所以不能把前者折成异常 —— 那会把「系统哑了」说成「你没说清」。
 */
export async function postAsr(form) {
  const r = await fetch("/v1/asr", { method: "POST", body: form });
  const b = await r.json();
  // 4xx / 413 这些是**请求本身**的问题（缺字段、音频太大），回到调用方去说。
  if (!r.ok) throw new Error(b.error || `HTTP ${r.status}`);
  return b;
}

/** POST 一个 JSON 路由，并回报闸门结果。 */
export async function postJson(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const b = await r.json();
  const g = gateSummary(b);
  bump("dropped", g.dropped);
  return g;
}

/** 连 WS 播报通道，断线自愈。
 *
 *  ★ 回调由调用方注入（main.js），而不是在这里直接 import ui ——
 *    避免 net 与 ui 互相依赖。
 */
export function connectStream({ onHello, onAnnouncement, onStatus }) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/v1/stream`);

  ws.onopen = () => onStatus(true);
  // 断线自愈：2 秒后重连。端侧是移动网络，断是常态不是异常。
  ws.onclose = () => { onStatus(false); setTimeout(() => connectStream({ onHello, onAnnouncement, onStatus }), 2000); };
  ws.onerror = () => onStatus(false);

  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "hello") { onHello?.(m.data); return; }
    if (m.type === "pong") return;
    if (m.type === "announcement") onAnnouncement?.(m.data);
  };
}

/** 健康检查。★「没出声」必须能和「系统哑了」区分开。 */
export async function fetchHealth() {
  const r = await fetch("/v1/health");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
