/* 调试件逻辑 —— 契约验证的那一半。
 *
 * 与 ui.js 的分界：这里的东西**交付形态里不该有**（请求日志、健康明细、
 * 其余路由按钮、地图折叠）。它们在 index.html 里都在 `#devPanel`
 * （默认 hidden）内，JS 跟着分。
 *
 * ★ 帧源**不在这里**（2026-09-23 搬走）：视频抽帧、发帧频率、单张图片、
 *   摄像头搬到了 `js/sender.js` + `sender.html` —— 那是**设备端**的东西，
 *   而且它得在页面加载后就一直跑，跟「默认隐藏的调试件」是两种生命期。
 *   两边共用同一个上传出口 `net.js::sendFrame()`。
 *
 * ★ 隐藏 ≠ 删除：这些元素仍在 DOM 里，各模块一律按 id 取用，
 *   真删掉会当场报错。所以是 `hidden`，翻出来就能继续验证契约。
 */

import { $, readGeo } from "./dom.js";
import { log } from "./log.js";
import { postJson, fetchHealth } from "./net.js";
import { toast, speak, haptic, localNote } from "./ui.js";
import LingmouMap from "../map.js";

// =====================================================================
// 开发者面板开关
// =====================================================================

const DEV_KEY = "lingmou.dev";

export function setDev(on) {
  $("devPanel").hidden = !on;
  $("devBtn").classList.toggle("on", on);
  $("devBtn").setAttribute("aria-pressed", on ? "true" : "false");
  document.body.classList.toggle("dev", on);
  try { localStorage.setItem(DEV_KEY, on ? "1" : "0"); } catch { /* 隐私模式下会拒绝 */ }
  if (on) log("开发者模式：开（测试件已显示）", "dim");
}

export function devFromUrl() {
  const q = new URLSearchParams(location.search).get("dev");
  if (q === "1") return true;
  if (q === "0") return false;
  try { return localStorage.getItem(DEV_KEY) === "1"; } catch { return false; }
}

// =====================================================================
// 其余路由 —— 一次性触发各层的按钮
// =====================================================================

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

/** 动作确认 —— 三通道一起给，因为**这一层不能假设用户在看屏幕**。
 *
 *   眼睛（陪同者）—— toast
 *   耳朵（用户）  —— TTS 念一遍结果，否则按了「一键求助」也不知道按上没有
 *   手            —— 震一下；关了播报开关时，这是唯一的反馈
 */
function confirmAction(msg, kind = "") {
  toast(msg, kind);
  // ★ 带 interrupt：用户**自己按的**按钮优先于一条没人要的场景描述 ——
  //   按了按钮却要等十几秒才听到回声，用户只会再按一次。
  speak(msg, { priority: 2, interrupt: true });
  haptic("short");
}

/** 按下就要给回执的路由 —— 生死按钮不能等网络回来才响。
 *
 *  ★ 起因：一键求助原来只有「请求返回之后」那一次确认（confirmAction）。
 *    可那中间是**一整个网络往返**，弱网下几秒起步，而这几秒里用户完全不知道
 *    自己按上没有 —— 他只会再按一次，或者更糟：以为按上了，站着等。
 *    所以耳朵和手先给一次「我收到了」，结果回来再覆盖它。
 */
const PRESS_ACK = {
  "/v1/emergency/sos": "正在请求帮助",
  "/v1/safety/fall": "收到跌倒信号，正在确认",
};

function bindRouteButtons() {
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
        const ack = PRESS_ACK[path];
        if (ack) {
          haptic("long");
          speak(ack, { priority: 3, interrupt: true });
        }
        const g = await postJson(path, body);
        log(`POST ${path} → 产出 ${g.produced} 条，放行 ${g.passed} 条`,
            g.produced ? "ok" : "dim");
        confirmAction(routeToast(path, g), g.produced ? "ok" : "");
      } catch (e) {
        log(`POST ${path} 失败：${e.message}`, "err");
        confirmAction(`发送失败：${e.message}`, "err");
      }
    };
  });
}

/** 「用当前位置」—— 手机上唯一能自己拿到起点的方式。 */
function bindGeoButton() {
  $("geoBtn").onclick = () => {
    const btn = $("geoBtn");
    const reset = () => { btn.disabled = false; btn.textContent = "用当前位置"; };

    if (!navigator.geolocation) {
      confirmAction("这个浏览器不提供定位", "err");
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
        confirmAction(`起点已换成当前位置（精度约 ${Math.round(pos.coords.accuracy)} 米）`, "ok");
        reset();
      },
      (err) => {
        // 定位只在 https 或 localhost 下可用 —— 局域网 IP 直连会被浏览器拒绝。
        confirmAction(`定位失败：${err.message || err.code}（需要 HTTPS 或 localhost）`, "err");
        reset();
      },
      { enableHighAccuracy: true, timeout: 8000, maximumAge: 0 },
    );

    // 兜底：某些实现两个回调都不来，按钮不能永远卡在「定位中…」。
    setTimeout(reset, 9000);
  };
}

// =====================================================================
// 健康检查 —— 「没出声」必须能和「系统哑了」区分开
// =====================================================================

/** 上一次的健康结论。null = 还没探过 —— 首次探测只记不下结论，
 *  理由见 health() 里「只报变化」那段注释。 */
let lastHealthBad = null;

async function health() {
  const dev = $("devStatus");
  try {
    const b = await fetchHealth();
    const bad = !b.ok;
    // ★ 极性：彩点只作第三重编码，但它**不能反过来**。
    //   .dot.on 是绿的（--ok）、.dot.off 是红的（--bad），与 wsDot 同一套约定。
    //   这里原先写反了（bad ? "on" : "off"）—— 结果是系统**健康时亮红灯**、
    //   降级时才变绿，配着旁边「正常」两个字正好把意思说反。
    //   看不见文字、只认颜色的人会因此以为出了事；而唯一能看出的
    //   「系统哑了」那条线索也就此失效。
    $("hDot").className = "dot " + (bad ? "off" : "on");
    // ★ 手机上只说「正常 / 降级」两个词，但**必须说**：用户若不知道系统
    //   哑了，会把「没出声」理解成「环境安全」。细节放 title 与开发者面板
    //   —— 见 app/api/routes.py::health。
    $("hTxt").textContent = bad ? "降级" : "正常";
    // 整枚药跟着变色，和 wsPill 一套（见 app.css 的 .pill.ok / .pill.bad）。
    $("hPill").classList.toggle("ok", !bad);
    $("hPill").classList.toggle("bad", bad);
    $("hPill").title = bad
      ? "降级：" + b.degraded.map((d) => d.reason).join(", ")
      : "全部实现可用";

    // ★ **只报变化**，不报状态（2026-09-23）。
    //   健康是每 5 秒轮一次的：每条都念一遍就是刷屏，用户三次之后就会开始
    //   无视它 —— 到真出事那次他也不会听了。所以只有「变坏 / 变好」那一刻
    //   才出声，而且要说出**降到哪去了**，光说「降级」等于没说。
    //
    //   ★ 首次探测（lastBad === null）不下结论：页面刚打开时系统本来就是
    //     那个样子，这时候喊一句「系统降级」是假消息 —— 用户会以为刚出事。
    const was = lastHealthBad;
    lastHealthBad = bad;
    if (was !== null && was !== bad) {
      const why = b.degraded.map((d) => d.reason).join("、");
      localNote(
        bad ? `系统降级：${why || "部分功能不可用"}。` : "系统已恢复正常。",
        { priority: 2, hapticKind: bad ? "double" : "short" },
      );
    }

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
    $("hPill").classList.remove("ok");
    $("hPill").classList.add("bad");
    $("hPill").title = `健康检查失败：${e.message}`;
    if (dev) dev.textContent = `健康检查失败：${e.message}`;
    // ★ 「连不上」不出声：同一件事 WS 那条路已经说过一次了（见 main.js 的
    //   setConnectionStatus）。一件事报两遍，用户会以为出了两个问题。
    //   这里只把画面改诚实，声音留给那条更根本的通道。
  }
}

// =====================================================================
// 绑定 + 启动
// =====================================================================

export function initDev() {
  $("devBtn").onclick = () => setDev($("devPanel").hidden);
  $("devClose").onclick = () => setDev(false);

  // 折叠区（地图等）。★ 用 max-height 而不是 <details>：<details> 关闭时
  // 内容 display:none，地图容器没有布局高度，百度 GL 会静默不渲染。
  document.querySelectorAll("[data-fold]").forEach((head) => {
    head.onclick = () => {
      const box = head.closest(".fold");
      if (!box) return;
      const open = box.classList.toggle("open");
      head.setAttribute("aria-expanded", open ? "true" : "false");
      // 展开后再让地图量一次 —— 它是在折叠状态下初始化的
      if (open) LingmouMap.refresh();
    };
  });

  bindRouteButtons();
  bindGeoButton();

  health();
  setInterval(health, 5000);
}
