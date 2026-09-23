/* 入口 —— 只做装配，不放业务逻辑。
 *
 * 各模块按 index.html 已经划好的界线分工：
 *
 *   ui.js    产品界面（播报流 / 紧急全屏 / 提示 / 语音 / 筛选暂停清空）
 *   dev.js   调试件（面板开关 / 其余路由 / 健康 / 统计）
 *            ★ 帧源不在这一页：它在 js/sender.js + sender.html（另一页）
 *   net.js   传输（POST /v1/frame、WS /v1/stream、其余 POST 路由）
 *   state.js 共享状态（单一真源）
 *   map.js   路线可视化（★ 唯一依赖第三方运行时：百度 JSAPI GL）
 *
 * ★ 末尾派发 `lm:booted`：那是启动自检（boot-check.js）的**报到信号** ——
 *   它判「页面起来没有」靠的是这个事件，不是「有没有报错」（第三方脚本
 *   天天在抛，跨域的还会被抹成 `Script error.`）。见 boot-check.js 头注释。
 *
 * ★ map.js 也从这里 import —— 加载顺序交给模块图，不再依赖 <script> 的
 *   先后。它改成 export default 之后，`window.LingmouMap` 那个约定就去掉了：
 *   跨文件接口走 import，而不是靠全局变量。
 */

import { connectStream } from "./net.js";
import { log } from "./log.js";
import { receive, initUI, localNote } from "./ui.js";
import { initDev, setDev, devFromUrl } from "./dev.js";
import { initVoiceInput } from "./voice.js";
import { initOrigin } from "./nav.js";
import { $ } from "./dom.js";
import LingmouMap from "../map.js";
import { count as countIncident, initIncidents } from "./incidents.js";

initUI();
initDev();
initIncidents();
// ★ 语音输入（按住说话）—— 产品功能，所以和 initUI 一起，不在 initDev 里。
initVoiceInput();
// ★ 导航的起点与目的地 —— 同样是产品功能。原先这两样散在 dev.js 的两个
//   bind 函数里（「开始导航」按钮 + 「用当前位置」按钮），现在合成 nav.js：
//   进页面自动定位起点，打字回车 / 语音松开都直接出发。
initOrigin();
// ★ 地图的启动**显式放在这里**，而不是藏在 map.js 的 IIFE 里自启 ——
//   原先那样写，一个 `return` 就能把它变成死代码，而且**静默**：
//   不报错，只是地图永远不出现。改成显式调用之后，启动顺序在这一处看得全，
//   也和其他模块（initUI / initDev / initIncidents）一个写法。
LingmouMap.init();

// 先定视图再做别的 —— 免得新播报到达时版面在跳
setDev(devFromUrl());

/* ★ 连接状态 —— 既要变色，也要**出声**（2026-09-23）。
 *
 *   对看不见屏幕的人，「网络断了」和「一切正常、只是暂时没人说话」是同一件
 *   事，可后果差得远：前者意味着**系统已经瞎了**，而用户会按「没出声＝环境
 *   安全」继续往前走。README 里早写着这句话，但页面上一直只有一枚小药丸在
 *   变色 —— 那是给陪同者看的。
 *
 *   两条配套的规矩：
 *     · **迟滞**：断线自愈是 2 秒一轮，`onStatus(false)` 会被反复调用 ——
 *       不加迟滞就会刷屏。等 LOST_GRACE_MS 还没回来才说，且只说一次。
 *     · **恢复也要说，还要说断了多久**：只说断不说恢复，用户会一直以为系统
 *       哑着；而「断过又好了」意味着中间那几秒的播报是**缺的**，必须如实讲，
 *       不能让他以为听到了全程。
 *
 *   ★ 走 localNote 而不是直接 speak：那条路同时喂 TTS、读屏（TTS 关时）和
 *     播报流，三个通道说的是同一句话 —— 见 ui.js::localNote。
 */
const LOST_GRACE_MS = 5000;
let everConnected = false;      // 首次连上之前不算「断开」，那是还没开始
let lostAt = 0;                 // 断开的起点；0 表示「没断着」
let lostTimer = null;

function setConnectionStatus(ok) {
  $("wsDot").className = "dot " + (ok ? "on" : "off");
  $("wsTxt").textContent = ok ? "已连接" : "断开，重连中…";
  // ★ 整枚药跟着状态变色（见 app.css 的 .pill.ok / .pill.bad）：7px 的小圆点
  //   对低视力用户太小了，整块色域才认得出。颜色仍只是第三重编码 ——
  //   文字那句「断开，重连中…」永远在。
  $("wsPill").classList.toggle("ok", ok);
  $("wsPill").classList.toggle("bad", !ok);

  if (ok) {
    if (lostTimer) { clearTimeout(lostTimer); lostTimer = null; }
    const back = lostAt ? Math.round((Date.now() - lostAt) / 1000) : 0;
    // ★ 只在**报过**断线之后才报恢复（`back` > 0）：否则页面一加载就会念
    //   一句「网络已恢复」，而它刚才根本没断过 —— 那是最招人烦的假消息。
    if (back) localNote(`网络已恢复。刚才断开的 ${back} 秒里没有播报。`, { priority: 2, hapticKind: "short" });
    everConnected = true;
    lostAt = 0;
    return;
  }

  if (!everConnected || lostAt) return;   // 还没连上过，或已经记着在断
  lostAt = Date.now();
  lostTimer = setTimeout(() => {
    lostTimer = null;
    localNote("网络断开，正在重连。这期间不会有播报。", { priority: 2, hapticKind: "long" });
  }, LOST_GRACE_MS);
}

connectStream({
  onHello: (d) => log(`WS 已连接 ts=${d.ts}`, "ok"),
  onStatus: (ok) => setConnectionStatus(ok),
  onAnnouncement: (a) => {
    // 意外统计**先于** receive()：receive 在「暂停」时直接入队就返回，
    // 而暂停只是「别往墙上贴」—— 出过的事照样要记进统计。
    countIncident(a);
    receive(a);
    // 路线可视化面板（web/map.js，可选加载）。
    //
    // ★ 刻意挂在这里而**不是 receive() 里**：receive() 在「暂停」时
    //   直接入队就返回，恢复时走的是 paint()，会绕过 receive ——
    //   挂在那儿会让**暂停期间到达的路线永远画不出来**，而「暂停」正是
    //   这个调试台最常点的按钮。
    // ★ 地图也不该受 paused / filterMin 影响：它不是播报墙，
    //   而是「当前这条路线长什么样」的一个视图。
    LingmouMap.onAnnouncement(a);
  },
});

// ★ 报到：走到这一行说明整条模块链都执行了、装配也没抛。
//   放在**最后**而不是最前面 —— 放前面的话「init 里抛异常」这类失败会漏过去。
document.dispatchEvent(new Event("lm:booted"));
