/* Tune: let it turn the knobs, and show its working.
 *
 * Every evaluation is kept, so the result comes with the search that found
 * it - a curve that flattened early means the amp is right and the knobs are
 * nearly there; one that never settled means the amp is the problem, and no
 * amount of turning will fix it.
 */

import { api } from "../api.js";
import { COLOURS, lineChart } from "../charts.js";
import { describeJob, track } from "../jobs.js";
import { onArrive } from "../nav.js";
import { $, attempt, clear, el, toast } from "../ui.js";
import { remainingCard } from "../remaining.js";
import { dropZone } from "../upload.js";

const KNOBS = ["GainA", "Bass", "Mid", "Treble", "Presence", "Master"];
const form = {};
let arrived = null;

function field(label, input) {
  return el("label", { class: "field" }, el("span", { text: label }), input);
}

async function fillSongs(select, partSelect) {
  const { songs } = await api.get("/api/songs").catch(() => ({ songs: [] }));
  clear(select).append(el("option", { value: "", text: "a file instead" }));
  for (const song of songs) select.append(el("option", { value: song.song_id, text: song.title }));
  select.onchange = async () => {
    clear(partSelect);
    if (!select.value) return;
    const song = await api.get(`/api/songs/${select.value}`);
    const parts = [...song.parts].sort((a, b) => Number(b.chosen) - Number(a.chosen));
    for (const part of parts) partSelect.append(el("option", { value: part.part_id, text: `${part.chosen ? "* " : ""}${part.label} (${part.sketch?.summary || ""})`, "data-di": part.di_take || "" }));
    partSelect.onchange?.();
  };
}

async function fillTakes(select) {
  const { takes } = await api.get("/api/takes", { kind: "di" }).catch(() => ({ takes: [] }));
  clear(select).append(el("option", { value: "", text: "your latest DI" }));
  for (const take of takes) select.append(el("option", { value: take.take_id, text: take.describe }));
}

async function fillDrafts(select) {
  const { drafts } = await api.drafts().catch(() => ({ drafts: [] }));
  clear(select).append(el("option", { value: "", text: "the simulated amp's knobs" }));
  for (const draft of drafts || []) select.append(el("option", { value: draft.draft_id, text: draft.name || draft.draft_id }));
}

async function fillRoutes(select) {
  const { routes, default: fallback } = await api.get("/api/loop/routes").catch(() => ({ routes: [] }));
  clear(select);
  for (const route of routes) select.append(el("option", { value: route.route_id, text: `${route.route_id} (${route.kind})` }));
  if (!routes.length) select.append(el("option", { value: "", text: "no routes - set one up in Loop" }));
  if (fallback) select.value = fallback;
}

function renderForm() {
  const host = clear($("#tune-body"));
  form.song = el("select");
  form.part = el("select");
  form.target = dropZone({ label: "Target audio (or pick a song's part)", accept: "audio/*" });
  form.di = el("select");
  form.mode = el("select", {}, ...["performance", "distribution", "aligned"].map((m) => el("option", { value: m, text: m })));
  form.renderer = el("select", {}, el("option", { value: "virtual", text: "simulated amp (offline)" }), el("option", { value: "loop", text: "the Core, through the loop" }));
  form.route = el("select");
  form.draft = el("select");
  form.budget = el("input", { type: "number", value: 30, min: 4, max: 400, class: "narrow" });
  form.method = el("select", {}, ...["auto", "pattern", "optuna", "random"].map((m) => el("option", { value: m, text: m })));
  form.knobs = KNOBS.map((name) => el("input", { type: "checkbox", value: name, checked: name !== "Master" }));
  form.part.onchange = () => {
    const option = form.part.selectedOptions[0];
    if (option?.dataset.di) {
      form.di.value = option.dataset.di;
      form.mode.value = "performance";
    }
  };
  const progress = el("div", { class: "hint", id: "tune-progress" });

  host.append(
    el(
      "div",
      { class: "card hero" },
      el("div", { class: "form-grid" }, field("Song", form.song), field("Part", form.part), form.target, field("DI", form.di), field("Mode", form.mode), field("Renderer", form.renderer), field("Loop route", form.route), field("Rig", form.draft)),
      el("div", { class: "row" }, el("span", { class: "hint", text: "knobs:" }), ...form.knobs.map((box) => el("label", { class: "check hint", title: box.value }, box, box.value === "GainA" ? "Gain" : box.value)), el("span", { class: "grow" }), el("span", { class: "hint", text: "budget" }), form.budget, form.method),
      el("div", { class: "notice", text: "Performance mode needs the DI to be the part played on your guitar, on its clock (Songs - 'on my guitar'). Distribution mode works with any DI but barely constrains the low end." }),
      el("div", { class: "row" }, progress, el("span", { class: "grow" }), el("button", { class: "btn primary", text: "Start tuning", onclick: start }))
    ),
    el("div", { id: "tune-result" }),
    el("div", { class: "card" }, el("h3", { text: "Past runs" }), el("div", { class: "list", id: "tune-runs" }))
  );
  fillSongs(form.song, form.part).then(() => applyArrival());
  fillTakes(form.di);
  fillDrafts(form.draft);
  fillRoutes(form.route);
}

function applyArrival() {
  if (!arrived) return;
  const { songId, partId, diTake } = arrived;
  arrived = null;
  if (!songId) return;
  form.song.value = songId;
  form.song.onchange().then(() => {
    if (partId) form.part.value = partId;
    if (diTake) {
      form.di.value = diTake;
      form.mode.value = "performance";
    }
  });
}

async function start() {
  await attempt(async () => {
    let target = form.target.path();
    if (form.song.value && form.part.value) {
      target = (await api.get(`/api/songs/${form.song.value}/parts/${form.part.value}/audio`)).path;
    }
    if (!target) throw new Error("Choose a target - a song's part, or an audio file");
    const knobs = form.knobs.filter((box) => box.checked).map((box) => (form.draft.value ? box.value : `Amp.${box.value}`));
    const body = {
      target,
      di: form.di.value || null,
      mode: form.mode.value,
      renderer: form.renderer.value,
      route_id: form.renderer.value === "loop" ? form.route.value || null : null,
      draft_id: form.draft.value || null,
      knobs,
      budget: Number(form.budget.value) || 30,
      method: form.method.value,
      song_id: form.song.value || null,
      part_id: form.part.value || null,
    };
    const { job_id } = await api.post("/api/tune", body);
    const run = await track(job_id, { onUpdate: (job) => ($("#tune-progress").textContent = describeJob(job)) });
    $("#tune-progress").textContent = "";
    show(run);
    refreshRuns();
  });
}

function show(run) {
  const host = clear($("#tune-result"));
  const result = run.result || {};
  const trials = result.trials || [];
  const card = el("div", { class: "card" });
  card.append(
    el("div", { class: "chain-head" }, el("h2", { class: "rig-name", text: `Run ${run.run_id}` }), el("span", { class: "tag", text: `${run.renderer} / ${run.mode}` }), result.improvement != null ? el("span", { class: "tag a2", text: `${Math.round(result.improvement * 100)}% closer` }) : null),
    el("pre", { class: "out", text: run.describe })
  );
  const buttons = el("div", { class: "row" });
  const player = new Audio();
  for (const [label, url] of [["the target", run.target_url], ["the best", run.best_url]]) {
    if (url) buttons.append(el("button", { class: "btn small", text: `play ${label}`, onclick: () => { document.querySelectorAll("audio").forEach((a) => a.pause()); player.src = url; player.play(); } }));
  }
  const draft = el("select");
  fillDrafts(draft);
  const withEq = el("input", { type: "checkbox" });
  buttons.append(
    el("span", { class: "grow" }),
    draft,
    el("label", { class: "check hint" }, withEq, "+ slot EQ"),
    el("button", { class: "btn", text: "Apply to draft", onclick: () => attempt(async () => { if (!draft.value) throw new Error("Choose a draft"); const r = await api.post(`/api/tune/runs/${run.run_id}/apply`, { draft_id: draft.value, with_eq: withEq.checked }); toast(r.message, { kind: "ok" }); }) })
  );
  card.append(buttons);

  if (trials.length) {
    const chart = el("div", { class: "chart" });
    card.append(el("h3", { text: "The search", style: "margin-top:12px" }), chart);
    let best = Infinity;
    const finite = trials.map((t) => (Number.isFinite(t.loss) ? t.loss : null));
    const running = finite.map((loss) => (loss === null ? best : (best = Math.min(best, loss))));
    requestAnimationFrame(() => lineChart(chart, { x: trials.map((t) => t.index), height: 180, yLabel: "loss", series: [{ label: "each try", values: finite, color: COLOURS.dim, points: true }, { label: "best so far", values: running, color: COLOURS.amber }] }));
  }
  if (run.remaining?.length) card.append(remainingCard(run.remaining));
  if (run.eq?.curve?.length) {
    const chart = el("div", { class: "chart" });
    card.append(el("h3", { text: "What an EQ would still do", style: "margin-top:12px" }), el("div", { class: "hint", text: run.eq.describe }), chart);
    requestAnimationFrame(() => lineChart(chart, { x: run.eq.grid, logX: true, height: 160, yLabel: "dB", series: [{ label: "target minus best", values: run.eq.curve, color: COLOURS.blue }] }));
  }
  host.append(card);
}

async function refreshRuns() {
  const { runs } = await api.get("/api/tune/runs").catch(() => ({ runs: [] }));
  const list = clear($("#tune-runs"));
  if (!runs.length) list.append(el("div", { class: "hint", text: "No tuning runs yet." }));
  for (const run of runs) {
    const best = run.result?.best;
    list.append(
      el(
        "div",
        { class: "item", style: "cursor:pointer", onclick: () => show(run) },
        el("span", { class: "tag", text: run.renderer }),
        el("span", { class: "title", text: run.run_id }),
        el("span", { class: "meta", text: run.mode }),
        el("span", { class: "spacer" }),
        el("span", { class: "meta", text: best ? `best ${best.loss.toFixed(4)}` : "" })
      )
    );
  }
}

export async function init() {
  renderForm();
  await refreshRuns();
}

export function onShow() {
  refreshRuns();
}

onArrive("tune", (payload) => {
  arrived = payload;
  if (form.song) applyArrival();
});
