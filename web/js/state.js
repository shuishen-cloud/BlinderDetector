/* 共享状态 —— 单一真源。
 *
 * ★ 为什么单独一个文件：这些可变状态原先散落在 app.js 全程（12 处），
 *   任何一段逻辑都能改任何一个 —— 出问题时无从下手。集中在这里之后，
 *   「谁会改状态」一眼可见。
 *
 * ★ 分两类，别混：
 *     · 产品状态（ttsOn / paused / filterMin / history / queue / feed）
 *       —— 决定用户听到什么、看到什么
 *     · 调试计数（stats）—— 只喂开发者面板，产品逻辑不依赖
 */

import { $ } from "./dom.js";

// ---- 产品状态 --------------------------------------------------------

/** 已渲染的 .ann 元素，最新在前。只用于切换筛选与相对时间刷新。 */
export const history = [];
/** 暂停期间攒下的播报，恢复时补上。 */
export const queue = [];

export const state = {
  /** mock 靠 `index % len(场景表)` 轮换场景 —— 必须全局递增。
   *  固定 index 会让闸门把后续全部当重复丢掉，页面上只剩一条。 */
  frameSeq: 0,
  /** 真实初值由 setTts(true) 定 —— 这是个用耳朵的产品。 */
  ttsOn: false,
  /** 发帧循环的两个 interval；null 表示没在发。 */
  loopTimer: null,
  paused: false,
  filterMin: 0,
  /** 帧源当前的告警文案（空串 = 正常）。见 dev.js 的 setFrameMsg。 */
  frameWarn: "",
};

// ---- 调试计数（只喂开发者面板）---------------------------------------

export const stats = { total: 0, critical: 0, expired: 0, dropped: 0, frames: 0 };

const STAT_EL = {
  total: "stTotal", critical: "stCritical", expired: "stExpired",
  dropped: "stDropped", frames: "stFrames",
};

export function bump(key, by = 1) {
  stats[key] += by;
  const el = $(STAT_EL[key]);
  if (el) el.textContent = stats[key];
}
