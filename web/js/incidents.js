/* 意外统计 —— **给陪同者/家属看的**，不是给用户看的。
 *
 * ★ 定位：盲人用户看不到这一屏，所以它不是产品主界面的一部分，而是
 *   「这次出门出了几次事」的一个交代。放在手机视图里（陪同者看得见），
 *   但刻意做得紧凑，不跟播报流抢位置。
 *
 * ★ 为什么分「意外」和「预警」两组：
 *     意外 —— 已经发生的事（跌倒、求助）。次数少，每一个都要紧。
 *     预警 —— 系统替他躲过去的事（危险障碍、系统降级）。次数多，
 *             它是「差点出事」，跟「出了事」不是一回事，混在一起会让
 *             「意外 = 8 次」这种读起来很吓人的数字失去意义。
 *
 * ★ 判据全部取自 `detail`（契约里的既有字段），不额外加状态：
 *     emergency + state=suspected/confirmed/cancelled  → 跌倒
 *     emergency + state=notifying（escalation_step）    → 求助
 *     safety    + risk=danger                          → 危险预警
 *     system                                           → 系统降级
 *   这样统计口径与后端契约同源，后端改措辞不会让统计失真。
 *
 * ★ 目前只统计**本次会话**（刷新即清零）。跨会话的累积属于「家属远程查看」
 *   那条线（见 新功能与接口改动.md §1.4），要落库，不在这一轮。
 */

import { $ } from "./dom.js";

//: 分类 → 展示名。顺序即展示顺序。
export const KINDS = {
  fall_suspected: "跌倒·疑似",
  fall_confirmed: "跌倒·确认",
  fall_cancelled: "跌倒·取消",
  sos_sent: "求助·发出",
  sos_escalated: "求助·升级",
  danger: "危险障碍",
  degraded: "系统降级",
};

/** 「已经发生的事」和「替他躲过去的事」——分开展示，见文件头。 */
const ACCIDENTS = ["fall_suspected", "fall_confirmed", "fall_cancelled", "sos_sent", "sos_escalated"];
const WARNINGS = ["danger", "degraded"];

const counts = Object.fromEntries(Object.keys(KINDS).map((k) => [k, 0]));

/** 一条播报算不算「意外/预警」。不算就返回 null。 */
export function classify(a) {
  const d = a.detail || {};

  if (a.source === "emergency") {
    switch (d.state) {
      case "suspected": return "fall_suspected";
      case "confirmed": return "fall_confirmed";
      case "cancelled": return "fall_cancelled";
      case "notifying":
        // escalation_step 是升级链上的位置：0 = 刚通知家属，>=1 = 已升级
        return (d.escalation_step ?? 0) >= 1 ? "sos_escalated" : "sos_sent";
      default: return null;
    }
  }

  if (a.source === "safety" && d.risk === "danger") return "danger";
  if (a.source === "system") return "degraded";
  return null;
}

export function count(a) {
  const kind = classify(a);
  if (!kind) return;
  counts[kind] += 1;
  render();
}

export function reset() {
  for (const k of Object.keys(counts)) counts[k] = 0;
  render();
}

function summarize(keys) {
  const parts = keys.filter((k) => counts[k] > 0).map((k) => `${KINDS[k]} ${counts[k]}`);
  return parts.length ? parts.join("，") : "无";
}

function render() {
  const acc = $("incAccidents");
  const warn = $("incWarnings");
  if (acc) acc.textContent = summarize(ACCIDENTS);
  if (warn) warn.textContent = summarize(WARNINGS);
}

export function initIncidents() {
  $("incReset").onclick = reset;
  render();
}
