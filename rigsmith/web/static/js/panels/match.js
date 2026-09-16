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
import { $, attempt, clear, el, jobRow, toast } from "../ui.js";
import { createWave, describeRegion } from "../wave.js";

const CROSSFADE_S = 0.008;

let context = null;
let player = null; // { sources, gains, startedAt, active }
let chart = null;
let waves = null; // { target, candidate } - built on first use, see ensureWaves
let found = null; // {label: path} from the last capture-folder scan
let searchJob = null; // the offline sweep currently running, if any
let lastRanking = null; // {label: path} of what was scored most recently

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

/* -------------------------------------------------------------- waveforms */

/* Built on first use rather than at init, because wavesurfer measures its
 * container when it is created and the Match panel starts hidden - a width
 * of zero there draws a waveform one pixel wide. */
function ensureWaves() {
  if (waves) return waves;
  waves = {
    target: createWave($("#target-wave"), {
      onChange: (span) => onRegionChanged("target", span),
    }),
    candidate: createWave($("#candidate-wave"), {
      onChange: (span) => onRegionChanged("candidate", span),
    }),
  };
  return waves;
}

/** True when the two sides must describe the same span - the server agrees. */
const regionsAreLocked = () => $("#match-mode").value === "aligned";

let mirroring = false;

function onRegionChanged(which, span) {
  const other = which === "target" ? "candidate" : "target";
  $(`#${which}-region-label`).textContent = describeRegion(span);

  // Aligned mode compares sample against sample, so trimming the two sides
  // differently would shift one against the other - the exact contamination
  // that mode exists to avoid. Keep them together rather than letting the
  // server refuse the score after the fact.
  if (regionsAreLocked() && !mirroring && waves?.[other]?.info()) {
    mirroring = true;
    try {
      waves[other].setRegion(span);
      $(`#${other}-region-label`).textContent = describeRegion(span);
    } finally {
      mirroring = false;
    }
  }
}

async function openInto(which, path) {
  const wave = ensureWaves()[which];
  const info = await wave.load(path);
  $(`#${which}-wave-block`).hidden = false;
  $(`#${which}-wave-name`).textContent = info.duration
    ? `${info.name}  ${info.duration.toFixed(1)}s  ${info.sample_rate} Hz`
    : info.name;
  return info;
}

function regionFor(which) {
  return waves?.[which]?.region() ?? null;
}

/* -------------------------------------------------- offline render + rank */

/* Layer 1: one DI through every capture in a folder, all scored against the
 * target. It is minutes of work, so it runs as a job and the page watches it
 * rather than hanging on a request. */

async function scanCaptures() {
  const directory = $("#search-captures").value.trim();
  if (!directory) throw new Error("Give a folder of .nam captures");

  const { captures } = await api.scanCaptures(directory);
  found = Object.fromEntries(captures.map((capture) => [capture.name, capture.path]));

  const count = captures.length;
  // The .nam format carries no generation field, so this is inferred from the
  // file's own contents - see rigsmith/tone/nam.py.
  const a2 = captures.filter((capture) => capture.generation === "A2").length;
  $("#search-found").textContent = count
    ? `${count} capture(s), ${a2} of them A2. A2 models sag and compression ` +
      "better, which is most of what separates a good capture from a close one."
    : "No .nam files there.";
  $("#search-run").disabled = !count;
  return captures;
}

function watchSearch(jobId) {
  searchJob = jobId;
  const host = clear($("#search-progress"));
  host.append(el("div", { class: "hint", text: "starting..." }));
}

/** Called from the live socket for every job event. */
export function onJobs(jobs) {
  if (!searchJob) return;
  const job = jobs.find((candidate) => candidate.id === searchJob);
  if (!job) return;

  clear($("#search-progress")).append(
    jobRow(job, {
      onCancel: () => attempt(() => api.cancelJob(job.id)),
    })
  );
  if (job.finished) {
    const id = searchJob;
    searchJob = null;
    if (job.state === "succeeded") attempt(() => showSearchResult(id));
  }
}

async function showSearchResult(jobId) {
  const { result } = await api.job(jobId);
  const host = clear($("#search-out"));
  if (!result || !result.ranking) return;

  lastRanking = Object.fromEntries(
    result.rendered.map((rendered) => [rendered.label, rendered.path])
  );

  for (const [index, entry] of result.ranking.entries()) {
    const path = lastRanking[entry.label];
    const node = el(
      "div",
      { class: "item clickable", title: "Show this render's waveform" },
      el("span", { class: "tag", text: `#${index + 1}` }),
      el("span", { class: "title", text: entry.label }),
      el("span", { class: "spacer" }),
      el("span", { class: "meta", text: `composite ${entry.composite.toFixed(4)}` })
    );
    node.onclick = () => attempt(() => showCandidate(path));
    host.append(node);
  }

  for (const failure of result.failures || []) {
    host.append(el("div", { class: "finding warning", text: failure }));
  }
  host.append(
    el("div", {
      class: "notice subtle",
      text:
        "Layer 1 is an approximation used to shortlist. Confirm the top few " +
        "on the device, then decide by ear.",
    }),
    el("button", {
      class: "btn primary",
      text: "Blind test the top 3",
      onclick: () =>
        attempt(() =>
          startBlindTest(
            result.ranking.slice(0, 3).map((entry) => entry.label),
            lastRanking
          )
        ),
    })
  );
}

/* ---------------------------------------------------- score -> listen loop */

/** The step that was previously only reachable from the CLI. */
async function startBlindTest(labels, paths) {
  const usable = labels.filter((label) => paths[label]);
  if (usable.length < 2) {
    throw new Error("A blind test needs at least two candidates");
  }
  const sessionId = `s${Date.now().toString(36)}`;
  const created = await api.createAb({
    session_id: sessionId,
    target_label: $("#target-path").value.trim() || "target",
    candidates: Object.fromEntries(usable.map((label) => [label, paths[label]])),
  });
  if (created.leaks?.length) {
    toast(
      `${created.leaks.length} clip(s) could not be blinded - their paths ` +
        "still identify them, so treat this result with suspicion.",
      { kind: "error", timeout: 12000 }
    );
  }
  await refreshSessions();
  $("#ab-session").value = sessionId;
  await renderSession(sessionId);
  toast(`Blind test ${sessionId} started with ${usable.length} candidates`, {
    kind: "ok",
  });
  $("#ab-trial").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* --------------------------------------------------------------- spectrum */

async function drawSpectrum(target, candidates) {
  const host = clear($("#spectrum"));
  const wanted = [
    { path: target, region: regionFor("target") },
    ...candidates.slice(0, 3).map((path) => ({
      path,
      // Only the candidate actually on screen has a region of its own.
      region:
        path === waves?.candidate?.info()?.path ? regionFor("candidate") : null,
    })),
  ];

  const series = [];
  for (const { path, region } of wanted) {
    const data = await api.spectrum(path, region).catch(() => null);
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

function rankingItem(entry, index, path) {
  const node = el(
    "div",
    { class: "item" },
    el("span", { class: "tag", text: `#${index + 1}` }),
    el("span", { class: "title", text: entry.label }),
    el("span", { class: "spacer" }),
    ...entry.metrics
      .filter((m) => ["esr", "spectral_profile", "mfcc_distribution"].includes(m.name))
      .map(metricRow)
  );
  if (path) {
    node.classList.add("clickable");
    node.title = "Show this candidate's waveform";
    node.onclick = () => attempt(() => showCandidate(path));
  }
  return node;
}

async function showCandidate(path) {
  await openInto("candidate", path);
  $("#candidate-wave-hint").textContent = regionsAreLocked()
    ? "Locked to the target's region - aligned mode compares the same span " +
      "of the same performance."
    : "Drag to pick the matching passage here. It rarely sits at the same " +
      "time code as it does on the record.";
  if (regionsAreLocked()) waves.candidate.setRegion(regionFor("target"));
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
      "is arbitrary. Regions are locked together here for the same reason.";
    if (waves?.target?.info()) onRegionChanged("target", regionFor("target"));
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
  unbindKeys();
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
    }),
    el("div", {
      class: "hint",
      text:
        "Keys:  A / B switch  -  space toggles  -  1 A is closer, 2 tie, " +
        "3 B is closer. Worth using: deciding without looking at the screen " +
        "removes one more thing that is not the sound.",
    })
  );

  bindKeys(sessionId, trial.trial_id);

  await attempt(async () => {
    await startPair(urls, 0);
    markActive(0);
  });
}

/* Deciding by ear works better when the hands are not on a mouse and the
 * eyes are not on a button. One handler at a time, replaced per trial. */

let keyHandler = null;

function unbindKeys() {
  if (keyHandler) document.removeEventListener("keydown", keyHandler);
  keyHandler = null;
}

function bindKeys(sessionId, trialId) {
  unbindKeys();
  keyHandler = (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const tag = event.target?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (!player) return;

    const key = event.key.toLowerCase();
    const actions = {
      a: () => switchTo(0),
      b: () => switchTo(1),
      " ": () => switchTo(player.active === 0 ? 1 : 0),
      1: () => answer(sessionId, trialId, "left"),
      2: () => answer(sessionId, trialId, "tie"),
      3: () => answer(sessionId, trialId, "right"),
    };
    const action = actions[key];
    if (!action) return;
    event.preventDefault();
    action();
  };
  document.addEventListener("keydown", keyHandler);
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

const candidatePaths = () =>
  $("#candidate-paths")
    .value.split(",")
    .map((text) => text.trim())
    .filter(Boolean);

export function init() {
  $("#match-mode").onchange = modeNote;
  modeNote();

  $("#open-target").onclick = () =>
    attempt(async () => {
      const path = $("#target-path").value.trim();
      if (!path) throw new Error("Give a path to the target audio first");
      await openInto("target", path);
    });

  $("#target-play").onclick = () => ensureWaves().target.playPause();
  $("#target-clear").onclick = () => ensureWaves().target.clear();
  $("#candidate-play").onclick = () => ensureWaves().candidate.playPause();
  $("#candidate-clear").onclick = () => ensureWaves().candidate.clear();

  $("#score-btn").onclick = () =>
    attempt(async () => {
      const target = $("#target-path").value.trim();
      const candidates = candidatePaths();
      if (!target || !candidates.length) {
        throw new Error("Give a target and at least one candidate");
      }

      const targetRegion = regionFor("target");
      const body = {
        target_path: target,
        mode: $("#match-mode").value,
        target_region: targetRegion,
        // Locked modes send one span for both sides; otherwise the candidate
        // region applies only while a candidate is actually on screen.
        candidate_region: regionsAreLocked()
          ? targetRegion
          : regionFor("candidate"),
        candidate_paths: Object.fromEntries(
          candidates.map((path) => [path.split(/[\\/]/).pop(), path])
        ),
      };
      const result = await api.score(body);

      const byLabel = body.candidate_paths;
      const host = clear($("#ranking"));
      result.ranking.forEach((entry, index) =>
        host.append(rankingItem(entry, index, byLabel[entry.label]))
      );
      if (result.ranking[0]?.notes?.length) {
        host.append(el("div", { class: "notice subtle", text: result.ranking[0].notes[0] }));
      }
      if (targetRegion) {
        host.append(
          el("div", {
            class: "hint",
            text: `Scored over ${describeRegion(targetRegion)} of the target.`,
          })
        );
      }

      lastRanking = byLabel;
      if (result.ranking.length > 1) {
        host.append(
          el("button", {
            class: "btn",
            text: "Blind test the top 3",
            title: "Metrics rank; ears decide. This is the deciding step.",
            onclick: () =>
              attempt(() =>
                startBlindTest(
                  result.ranking.slice(0, 3).map((entry) => entry.label),
                  byLabel
                )
              ),
          })
        );
      }

      // Put the winner on screen, so the thing you are about to listen to is
      // the thing the numbers just picked.
      const winner = byLabel[result.ranking[0]?.label];
      if (winner && winner !== waves?.candidate?.info()?.path) {
        await showCandidate(winner).catch(() => {});
      }
      await drawSpectrum(target, candidates);
    });

  $("#search-scan").onclick = () => attempt(scanCaptures);

  $("#search-run").onclick = () =>
    attempt(async () => {
      const target = $("#target-path").value.trim();
      const di = $("#search-di").value.trim();
      if (!target) throw new Error("Give the target audio above first");
      if (!di) throw new Error("Give a dry DI recording to render through");
      if (!found || !Object.keys(found).length) {
        await scanCaptures();
      }

      const targetRegion = regionFor("target");
      const { job_id } = await api.offlineSearch({
        di_path: di,
        target_path: target,
        captures: found,
        ir_path: $("#search-ir").value.trim() || null,
        mode: $("#match-mode").value,
        target_region: targetRegion,
        // Every render comes from the same DI, so one span covers them all.
        candidate_region: regionsAreLocked() ? targetRegion : regionFor("candidate"),
      });
      watchSearch(job_id);
    });

  $("#ab-session").onchange = (event) => renderSession(event.target.value);
  $("#ab-refresh").onclick = refreshSessions;

  return refreshSessions();
}

export const onShow = refreshSessions;

export function onHide() {
  stopPlayback();
  unbindKeys();
  waves?.target.stop();
  waves?.candidate.stop();
}
