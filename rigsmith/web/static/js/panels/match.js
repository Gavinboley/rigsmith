/* Tone matching: scoring, spectral comparison, and blind A/B.
 *
 * The A/B player is the part worth being careful about. Two <audio> elements
 * are not a fair comparison - switching restarts playback or rebuffers, and
 * that gap alone is enough to bias which clip sounds "tighter". So both clips
 * are decoded to AudioBuffers up front, both sources start together and run
 * in sync, and switching is a short gain crossfade. The position never moves,
 * so you are comparing the same instant of each.
 */

import { api } from "../api.js";
import { $, attempt, clear, el, toast } from "../ui.js";

const CROSSFADE_S = 0.008;

let context = null;
let player = null; // { sources, gains, startedAt, active }
let chart = null;

/* ------------------------------------------------------------ audio engine */

function audioContext() {
  if (!context) context = new (window.AudioContext || window.webkitAudioContext)();
  return context;
}

async function loadBuffer(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Could not load audio: ${response.status}`);
  return audioContext().decodeAudioData(await response.arrayBuffer());
}

function stopPlayback() {
  if (!player) return;
  for (const source of player.sources) {
    try {
      source.stop();
    } catch {
      /* already stopped */
    }
  }
  player = null;
}

/** Start both clips together, with only `active` audible. */
async function startPair(urls, active = 0) {
  stopPlayback();
  const ctx = audioContext();
  await ctx.resume();

  const buffers = await Promise.all(urls.map(loadBuffer));
  const sources = [];
  const gains = [];

  const startAt = ctx.currentTime + 0.05;
  buffers.forEach((buffer, index) => {
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.loop = true;
    const gain = ctx.createGain();
    gain.gain.value = index === active ? 1 : 0;
    source.connect(gain).connect(ctx.destination);
    source.start(startAt);
    sources.push(source);
    gains.push(gain);
  });

  player = { sources, gains, active };
  return player;
}

/** Crossfade to the other clip without moving the playhead. */
function switchTo(index) {
  if (!player) return;
  const ctx = audioContext();
  const now = ctx.currentTime;
  player.gains.forEach((gain, position) => {
    gain.gain.cancelScheduledValues(now);
    gain.gain.setValueAtTime(gain.gain.value, now);
    gain.gain.linearRampToValueAtTime(position === index ? 1 : 0, now + CROSSFADE_S);
  });
  player.active = index;
  markActive(index);
}

function markActive(index) {
  document.querySelectorAll(".ab-side").forEach((node, position) => {
    node.classList.toggle("playing", position === index);
  });
}

/* --------------------------------------------------------------- spectrum */

async function drawSpectrum(paths) {
  const host = clear($("#spectrum"));
  if (!paths.length) return;

  const series = [];
  for (const path of paths.slice(0, 4)) {
    const data = await api.spectrum(path).catch(() => null);
    if (data && data.freqs.length) series.push({ path, ...data });
  }
  if (!series.length) return;

  const colours = ["#f0a020", "#48b884", "#5aa3e0", "#a07ae0"];
  const data = [series[0].freqs, ...series.map((s) => s.db)];

  if (chart) chart.destroy();
  chart = new uPlot(
    {
      width: host.clientWidth || 700,
      height: 190,
      scales: { x: { distr: 3 } }, // log frequency
      axes: [
        { stroke: "#6b7583", grid: { stroke: "#1e242b" } },
        { stroke: "#6b7583", grid: { stroke: "#1e242b" }, label: "dB" },
      ],
      series: [
        { label: "Hz" },
        ...series.map((s, index) => ({
          label: s.path.split(/[\\/]/).pop(),
          stroke: colours[index % colours.length],
          width: 1.5,
        })),
      ],
    },
    data,
    host
  );
}

/* ---------------------------------------------------------------- scoring */

function metricRow(metric) {
  return el(
    "span",
    { class: "meta", title: metric.interpretation || "" },
    `${metric.name} ${metric.value.toFixed(4)}`
  );
}

function rankingItem(entry, index) {
  return el(
    "div",
    { class: "item" },
    el("span", { class: "tag", text: `#${index + 1}` }),
    el("span", { class: "title", text: entry.label }),
    el("span", { class: "spacer" }),
    ...entry.metrics
      .filter((m) => ["esr", "spectral_profile", "mfcc_distribution"].includes(m.name))
      .map(metricRow)
  );
}

function modeNote() {
  const mode = $("#match-mode").value;
  const host = $("#mode-note");
  if (mode === "aligned") {
    host.className = "notice subtle warn";
    host.textContent =
      "Aligned mode assumes the target and every candidate are the SAME " +
      "performance - your own DI through each rig, against your own " +
      "reference. Against a record it reports nonsense, because the offset " +
      "is arbitrary.";
  } else {
    host.className = "notice subtle";
    host.textContent =
      "Distribution mode compares timbre, not timing. The ordering is " +
      "informative; the absolute numbers are not. Sample-wise metrics are " +
      "deliberately not computed.";
  }
}

/* -------------------------------------------------------------------- A/B */

async function renderSession(sessionId) {
  const host = clear($("#ab-trial"));
  const standings = clear($("#ab-standings"));
  stopPlayback();
  if (!sessionId) return;

  const session = await attempt(() => api.abSession(sessionId));
  if (!session) return;

  host.append(
    el("div", {
      class: "ab-progress",
      text: `${session.answered}/${session.total} trials  -  target: ${session.target_label}`,
    })
  );

  if (!session.trial) {
    const results = await api.abResults(sessionId);
    for (const [index, row] of results.standings.entries()) {
      standings.append(
        el(
          "div",
          { class: "item" },
          el("span", { class: "tag", text: `#${index + 1}` }),
          el("span", { class: "title", text: row.label }),
          el("span", { class: "spacer" }),
          el("span", { class: "meta", text: `${row.points}/${row.trials}` })
        )
      );
    }
    host.append(
      el("div", { class: "notice", text: "All trials answered - identities revealed." })
    );
    return;
  }

  const trial = session.trial;
  const urls = [trial.left_audio, trial.right_audio];

  const pair = el(
    "div",
    { class: "ab-pair" },
    el(
      "div",
      { class: "ab-side" },
      el("h4", { text: "A" }),
      el("button", { class: "btn", text: "Play A", onclick: () => switchTo(0) })
    ),
    el(
      "div",
      { class: "ab-side" },
      el("h4", { text: "B" }),
      el("button", { class: "btn", text: "Play B", onclick: () => switchTo(1) })
    )
  );

  host.append(
    pair,
    el(
      "div",
      { class: "ab-verdicts" },
      el("button", {
        class: "btn primary",
        text: "A is closer",
        onclick: () => answer(sessionId, trial.trial_id, "left"),
      }),
      el("button", {
        class: "btn",
        text: "Tie",
        onclick: () => answer(sessionId, trial.trial_id, "tie"),
      }),
      el("button", {
        class: "btn primary",
        text: "B is closer",
        onclick: () => answer(sessionId, trial.trial_id, "right"),
      })
    ),
    el("div", {
      class: "hint",
      text:
        "Both clips are already decoded and playing in sync - switching only " +
        "changes which one you hear, so the comparison is fair.",
    })
  );

  await attempt(async () => {
    await startPair(urls, 0);
    markActive(0);
  });
}

async function answer(sessionId, trialId, verdict) {
  await attempt(() => api.abVerdict(sessionId, { trial_id: trialId, verdict }));
  await renderSession(sessionId);
}

async function refreshSessions() {
  const { sessions } = await attempt(() => api.abSessions()) || { sessions: [] };
  const select = clear($("#ab-session"));
  select.append(el("option", { value: "", text: sessions.length ? "Choose a session" : "No sessions yet" }));
  for (const id of sessions) select.append(el("option", { value: id, text: id }));
}

/* -------------------------------------------------------------------- init */

export function init() {
  $("#match-mode").onchange = modeNote;
  modeNote();

  $("#score-btn").onclick = () =>
    attempt(async () => {
      const target = $("#target-path").value.trim();
      const candidates = $("#candidate-paths")
        .value.split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      if (!target || !candidates.length) {
        throw new Error("Give a target and at least one candidate");
      }

      const body = {
        target_path: target,
        candidate_paths: Object.fromEntries(
          candidates.map((path) => [path.split(/[\\/]/).pop(), path])
        ),
        mode: $("#match-mode").value,
      };
      const result = await api.score(body);

      const host = clear($("#ranking"));
      result.ranking.forEach((entry, index) =>
        host.append(rankingItem(entry, index))
      );
      if (result.ranking[0]?.notes?.length) {
        host.append(el("div", { class: "notice subtle", text: result.ranking[0].notes[0] }));
      }
      await drawSpectrum([target, ...candidates]);
    });

  $("#ab-session").onchange = (event) => renderSession(event.target.value);
  $("#ab-refresh").onclick = refreshSessions;

  return refreshSessions();
}

export const onShow = refreshSessions;
export const onHide = stopPlayback;
