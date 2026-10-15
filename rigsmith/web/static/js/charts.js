/* Small line charts on uPlot, styled once. */

import { el } from "./ui.js";

const AXIS = { stroke: "#6b7583", grid: { stroke: "#1e242b" }, ticks: { stroke: "#262d36" } };

/**
 * @param {HTMLElement} host
 * @param {{x: number[], series: {label: string, values: number[], color: string}[], logX?: boolean, height?: number, yLabel?: string}} spec
 */
export function lineChart(host, spec) {
  host.innerHTML = "";
  if (typeof uPlot === "undefined") {
    host.append(el("div", { class: "hint", text: "charting library unavailable" }));
    return null;
  }
  const width = Math.max(260, host.clientWidth || 600);
  const options = {
    width,
    height: spec.height || 200,
    scales: { x: { time: false, distr: spec.logX ? 3 : 1, range: () => [Math.min(...spec.x), Math.max(...spec.x)] } },
    axes: [{ ...AXIS }, { ...AXIS, label: spec.yLabel || "" }],
    legend: { show: spec.series.length > 1 },
    series: [{}, ...spec.series.map((s) => ({ label: s.label, stroke: s.color, width: 1.6, points: { show: s.points ?? false } }))],
  };
  const data = [spec.x, ...spec.series.map((s) => s.values)];
  // eslint-disable-next-line no-undef
  return new uPlot(options, data, host);
}

export const COLOURS = { amber: "#f0a020", green: "#48b884", blue: "#5aa3e0", red: "#e0574b", violet: "#a07ae0", dim: "#6b7583" };
