/* Small line charts on uPlot, styled once. */

import { api } from "./api.js";
import { attempt, el } from "./ui.js";

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
  // Drawn while its panel is hidden, the host has no width yet: fall back to
  // what the window can hold, not a fixed 600 that pushed a phone sideways.
  const width = Math.max(260, host.clientWidth || Math.min(600, window.innerWidth - 48));
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

const NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

/** "E2" for 40. */
export function noteName(midi) {
  const m = Math.round(midi);
  return `${NAMES[((m % 12) + 12) % 12]}${Math.floor(m / 12) - 1}`;
}

/**
 * Notes as a piano roll: time across, pitch up, a faint line on every E -
 * a guitar's own landmarks. Brighter is harder.
 * @param {HTMLElement} host
 * @param {{start: number, duration: number, midi: number, velocity?: number}[]} events
 * @param {{duration?: number, height?: number, label?: string}} [opts]
 */
export function pianoRoll(host, events, opts = {}) {
  host.innerHTML = "";
  if (!events.length) {
    host.append(el("div", { class: "hint", text: "no notes" }));
    return null;
  }
  const width = Math.max(220, host.clientWidth || Math.min(300, window.innerWidth - 48));
  const height = opts.height || 96;
  const ratio = window.devicePixelRatio || 1;
  const canvas = el("canvas", {
    width: Math.round(width * ratio),
    height: Math.round(height * ratio),
    style: `width:${width}px;height:${height}px`,
    role: "img",
    "aria-label": opts.label || `${events.length} notes`,
  });
  const pitches = events.map((e) => Math.round(e.midi));
  const low = Math.min(...pitches) - 1;
  const high = Math.max(...pitches) + 1;
  const end = Math.max(opts.duration || 0, ...events.map((e) => e.start + e.duration)) || 1;
  const row = height / (high - low + 1);
  const top = (m) => height - (m - low + 1) * row;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.strokeStyle = "#262d36";
  ctx.lineWidth = 1;
  for (let m = low; m <= high; m++) {
    if (((m % 12) + 12) % 12 !== 4) continue;
    const y = Math.round(top(m) + row / 2) + 0.5;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }
  ctx.fillStyle = COLOURS.amber;
  for (const e of events) {
    ctx.globalAlpha = 0.4 + 0.6 * Math.min(1, Math.max(0, e.velocity ?? 0.75));
    const x = (e.start / end) * width;
    ctx.fillRect(x, top(Math.round(e.midi)), Math.max(2, (e.duration / end) * width - 1), Math.max(2, row - 1));
  }
  ctx.globalAlpha = 1;
  host.append(canvas);
  return canvas;
}

/**
 * Fold the notes a DI plays open under ``anchor``, or shut again: a piano
 * roll, the count and range, and the MIDI to fix one in a tab editor.
 * @param {HTMLElement} button the control that toggles it (gets aria-expanded)
 * @param {HTMLElement} anchor the box goes right after this
 * @param {string} url an API path answering {events, duration?, midi_url?}
 * @param {{label?: string}} [opts]
 */
export async function toggleNotes(button, anchor, url, opts = {}) {
  const open = anchor.nextElementSibling?.classList.contains("p-notes") ? anchor.nextElementSibling : null;
  if (open) {
    open.remove();
    button.setAttribute("aria-expanded", "false");
    return;
  }
  const notes = await attempt(() => api.get(url));
  if (!notes) return;
  const pitches = notes.events.map((e) => Math.round(e.midi));
  const range = pitches.length ? `, ${noteName(Math.min(...pitches))} to ${noteName(Math.max(...pitches))}` : "";
  const roll = el("div", { class: "p-roll" });
  const box = el(
    "div",
    { class: "p-notes" },
    roll,
    el(
      "div",
      { class: "hint" },
      el("span", { text: `${notes.events.length} notes${range}` }),
      notes.midi_url ? el("a", { href: notes.midi_url, download: "", text: "save as MIDI", title: "Fix a wrong note in a tab editor, then render the part from that MIDI" }) : null
    )
  );
  anchor.after(box);
  button.setAttribute("aria-expanded", "true");
  pianoRoll(roll, notes.events, { duration: notes.duration, label: opts.label });
}

export const COLOURS = { amber: "#f0a020", green: "#48b884", blue: "#5aa3e0", red: "#e0574b", violet: "#a07ae0", dim: "#6b7583" };
