/* 入口 —— 只做装配，不放业务逻辑。
 *
 * 四个模块按 index.html 已经划好的界线分工：
 *
 *   ui.js    产品界面（播报流 / 紧急全屏 / 提示 / 语音 / 筛选暂停清空）
 *   dev.js   调试件（面板开关 / 帧源 / 单图 / 其余路由 / 健康 / 统计）
 *   net.js   传输（POST /v1/frame、WS /v1/stream、其余 POST 路由）
 *   state.js 共享状态（单一真源）
 *   map.js   路线可视化（★ 唯一依赖第三方运行时：百度 JSAPI GL）
 *
 * ★ map.js 也从这里 import —— 加载顺序交给模块图，不再依赖 <script> 的
 *   先后。它改成 export default 之后，`window.LingmouMap` 那个约定就去掉了：
 *   跨文件接口走 import，而不是靠全局变量。
 */

import { connectStream } from "./net.js";
import { log } from "./log.js";
import { receive, initUI } from "./ui.js";
import { initDev, setDev, devFromUrl } from "./dev.js";
import { $ } from "./dom.js";
import LingmouMap from "../map.js";
import { count as countIncident, initIncidents } from "./incidents.js";

initUI();
initDev();
initIncidents();
// ★ 地图的启动**显式放在这里**，而不是藏在 map.js 的 IIFE 里自启 ——
//   原先那样写，一个 `return` 就能把它变成死代码，而且**静默**：
//   不报错，只是地图永远不出现。改成显式调用之后，启动顺序在这一处看得全，
//   也和其他模块（initUI / initDev / initIncidents）一个写法。
LingmouMap.init();

// 先定视图再做别的 —— 免得新播报到达时版面在跳
setDev(devFromUrl());

connectStream({
  onHello: (d) => log(`WS 已连接 ts=${d.ts}`, "ok"),
  onStatus: (ok) => {
    $("wsDot").className = "dot " + (ok ? "on" : "off");
    $("wsTxt").textContent = ok ? "已连接" : "断开，重连中…";
  },
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
