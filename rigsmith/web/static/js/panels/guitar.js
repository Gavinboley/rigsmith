/* Guitar & Bass lab: tune up, dial in gain staging, monitor live, and teach
 * rigsmith your instrument.
 *
 * Step 1 (Pre-Flight): Built-in chromatic & open-string tuner (supporting
 * 4/5/6-string Bass down to 25 Hz and Guitar), real-time dBFS gain-staging
 * meter with sweet-spot coaching (-18 to -6 dBFS), live low-latency hardware
 * pass-through monitoring, and 1-click check recording & playback.
 *
 * Step 2 (Guided Session): Pick a protocol, press start, and the page counts
 * you in and walks a cursor through the steps in time with a click while the
 * interface records.
 */

import { api } from "../api.js";
import { lineChart, COLOURS } from "../charts.js";
import { track } from "../jobs.js";
import { onArrive } from "../nav.js";
import { likeControls, refreshPickups } from "../pickups.js";
import { $, attempt, clear, el, prompt, toast } from "../ui.js";
import { dropZone } from "../upload.js";

let profileId = null;
let profile = null;
let tunings = [];
let session = null;
let monitorTimer = null;
let monitorActive = false;
let tunedStrings = new Set();
let audioCtx = null;
const player = new Audio();

const NOTE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const noteName = (midi) => `${NOTE[Math.round(midi) % 12]}${Math.floor(Math.round(midi) / 12) - 1}`;

const pref = {
  get inputDevice() { return localStorage.getItem("rigsmith.guitar.inputDevice") || ""; },
  set inputDevice(v) { if (v) localStorage.setItem("rigsmith.guitar.inputDevice", v); },
  get inputChannel() { return Number(localStorage.getItem("rigsmith.guitar.inputChannel") || 1); },
  set inputChannel(v) { localStorage.setItem("rigsmith.guitar.inputChannel", String(v || 1)); },
  get outputDevice() { return localStorage.getItem("rigsmith.guitar.outputDevice") || ""; },
  set outputDevice(v) { localStorage.setItem("rigsmith.guitar.outputDevice", v || ""); },
  get listen() { return localStorage.getItem("rigsmith.guitar.listen") === "true"; },
  set listen(v) { localStorage.setItem("rigsmith.guitar.listen", v ? "true" : "false"); },
  get gainDb() { return Number(localStorage.getItem("rigsmith.guitar.gainDb") || 0); },
  set gainDb(v) { localStorage.setItem("rigsmith.guitar.gainDb", String(v || 0)); },
};

function playUrl(url) {
  document.querySelectorAll("audio").forEach((a) => a.pause());
  player.src = url;
  player.play().catch(() => {});
}

function playReferenceTone(hz, duration = 1.6) {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
    const now = audioCtx.currentTime;
    const master = audioCtx.createGain();
    master.gain.setValueAtTime(0.001, now);
    master.gain.exponentialRampToValueAtTime(0.28, now + 0.015);
    master.gain.exponentialRampToValueAtTime(0.0008, now + duration);
    master.connect(audioCtx.destination);
    // Include overtones so low bass notes (B0/E1/A1) ring clearly on all headphones/speakers
    const weights = hz < 90 ? [0.55, 0.45, 0.3, 0.18, 0.08] : [0.65, 0.25, 0.12, 0.05];
    weights.forEach((amp, i) => {
      const osc = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      osc.type = i === 0 ? "triangle" : "sine";
      osc.frequency.setValueAtTime(hz * (i + 1), now);
      g.gain.setValueAtTime(amp, now);
      osc.connect(g);
      g.connect(master);
      osc.start(now);
      osc.stop(now + duration);
    });
  } catch (_) {}
}

async function stopLiveMonitor() {
  if (monitorTimer) {
    clearInterval(monitorTimer);
    monitorTimer = null;
  }
  if (monitorActive) {
    monitorActive = false;
    await api.post("/api/loop/monitor/stop", {}).catch(() => {});
  }
}

async function refreshList(select = profileId) {
  const { profiles } = await api.get("/api/learn/profiles");
  const picker = clear($("#profile-select"));
  if (!profiles.length) picker.append(el("option", { value: "", text: "No instruments yet" }));
  for (const item of profiles) picker.append(el("option", { value: item.profile_id, text: item.label || item.profile_id }));
  const wanted = select && profiles.some((p) => p.profile_id === select) ? select : profiles[0]?.profile_id || null;
  picker.value = wanted || "";
  return wanted;
}

async function newProfile() {
  if (!tunings.length) tunings = (await api.get("/api/learn/tunings")).tunings;
  const answer = await prompt("Add guitar or bass", [
    { type: "note", text: "One profile per instrument and pickup - e.g. a P-Bass, a J-Bass bridge pickup, or a Les Paul bridge humbucker." },
    { name: "name", label: "Name", placeholder: "P-Bass / LP bridge" },
    { name: "guitar", label: "Instrument (Guitar or Bass)", placeholder: "Fender Precision Bass / Les Paul" },
    { name: "pickup", label: "Pickup", placeholder: "p-bass / j-bass-bridge / bridge humbucker" },
    {
      name: "tuning",
      label: "Tuning (Guitar & Bass)",
      type: "select",
      value: "E standard",
      options: tunings.map((t) => ({
        value: t.name,
        label: `${t.is_bass ? "[Bass] " : "[Guitar] "}${t.name} (${t.strings.join(" ")})`,
      })),
    },
  ], { okLabel: "Create" });
  if (!answer?.name) return;
  const made = await attempt(() => api.post("/api/learn/profiles", answer));
  if (made) await load(await refreshList(made.profile_id));
}

async function load(id) {
  await stopLiveMonitor();
  tunedStrings = new Set();
  profileId = id;
  const host = clear($("#guitar-body"));
  if (!id) {
    host.append(
      el(
        "div",
        { class: "rig-empty" },
        el("p", { text: "No guitars or basses learned yet. Tune up, dial in your interface gain in Step 1, and play a short guided sequence once so rigsmith can play any part on your instrument." }),
        el("button", { class: "btn primary", text: "Add my guitar or bass", onclick: newProfile }),
        " ",
        el("button", { class: "btn", text: "Try with a synthetic instrument", onclick: demo })
      )
    );
    return;
  }
  profile = await attempt(() => api.get(`/api/learn/profiles/${id}`, { samples: true }));
  if (!profile) return;
  render();
}

function render() {
  const host = clear($("#guitar-body"));
  if (!tunings.length) {
    api.get("/api/learn/tunings").then(({ tunings: list }) => {
      tunings = list || [];
      render();
    });
    return;
  }
  const tuneObj = tunings.find((t) => t.name === profile.tuning) || tunings[0];
  const isBass = Boolean(tuneObj?.is_bass || /bass/i.test(profile.guitar || "") || /bass/i.test(profile.tuning || ""));
  const print = profile.fingerprint || {};
  const stat = (k, v) => el("div", { class: "stat" }, el("div", { class: "k", text: k }), el("div", { class: "v", text: v }));
  const picks = (profile.samples || []).filter((s) => s.technique === "pick");
  const range = picks.length ? `${noteName(Math.min(...picks.map((s) => s.midi)))}-${noteName(Math.max(...picks.map((s) => s.midi)))}` : "--";
  const grid = profile.coverage.grid || [];
  const low = grid.reduce((n, row) => n + row.slice(0, 13).filter(Boolean).length, 0);
  const high = grid.reduce((n, row) => n + row.slice(13).filter(Boolean).length, 0);
  const covered = `${low}/${grid.length * 13}` + (high ? ` +${high} higher` : "");

  const tuningSelect = el(
    "select",
    {
      class: "inline-select",
      title: "Switch instrument tuning (supports Guitar and 4/5/6-string Bass)",
      "aria-label": "Instrument tuning",
      onchange: (e) =>
        attempt(async () => {
          await stopLiveMonitor();
          tunedStrings = new Set();
          await api.patch(`/api/learn/profiles/${profileId}`, { tuning: e.target.value });
          profile = await api.get(`/api/learn/profiles/${profileId}`, { samples: true });
          toast(`Tuning updated to ${profile.tuning}`, { kind: "ok" });
          render();
        }),
    },
    ...tunings.map((t) =>
      el("option", {
        value: t.name,
        selected: t.name === profile.tuning,
        text: `${t.is_bass ? "Bass · " : "Guitar · "}${t.name} (${t.strings.join(" ")})`,
      })
    )
  );

  host.append(
    el(
      "div",
      { class: "card" },
      el(
        "div",
        { class: "chain-head" },
        el("h2", { class: "rig-name", text: [profile.guitar, profile.pickup].filter(Boolean).join(" ") || profile.profile_id }),
        el("span", { class: `tag ${isBass ? "a1" : "a2"}`, text: isBass ? `${tuneObj?.strings?.length || 4}-string Bass` : "Guitar" }),
        tuningSelect,
        profile.has_voice
          ? el("span", {
              class: "tag a2",
              text: profile.voice_net ? "voice model + network" : "voice model",
              title: profile.voice_net
                ? "Unheard frets come from interpolation averaged with a small network trained on every note"
                : "Unheard frets come from interpolating the nearest notes; a network joins in from 48 notes",
            })
          : null
      ),
      profile.note ? el("div", { class: "hint", text: profile.note }) : null,
      el(
        "div",
        { class: "stat-grid" },
        stat("samples", String((profile.samples || []).length)),
        stat("range", range),
        stat("covered to fret 12", covered),
        stat("typical peak", `${(print.median_peak_db ?? -120).toFixed(1)} dBFS`),
        stat("noise floor", `${(print.noise_floor_db ?? -120).toFixed(0)} dBFS`),
        stat("tuning", print.tuning_cents == null ? "--" : Math.abs(print.tuning_cents) < 0.5 ? "in tune" : `${print.tuning_cents > 0 ? "+" : ""}${print.tuning_cents.toFixed(0)} c`),
        stat("brightness", `${Math.round(print.centroid_hz || 0)} Hz`),
        stat("hum", print.hum_hz ? `${print.hum_hz} Hz` : "none"),
        profile.resonance ? el("div", { class: "stat", title: profile.resonance.describe }, el("div", { class: "k", text: "pickup resonance" }), el("div", { class: "v", text: `${(profile.resonance.f0_hz / 1000).toFixed(1)} kHz Q${profile.resonance.q.toFixed(1)}` })) : null,
        profile.resonance ? stat("reads as", profile.resonance.clear ? profile.resonance.reads_as : "no clear peak") : null
      )
    ),
    preflightCard(tuneObj),
    sessionCard(),
    fretboardCard(),
    tryCard(isBass),
    fingerprintCard(),
    samplesCard()
  );
  drawFingerprint();
  populateAudioDevices();
}

/* --------------------------------- shared audio device discovery & scoring */

async function populateAudioDevices() {
  const { devices, error } = await api.get("/api/loop/devices").catch(() => ({ devices: [], error: "offline" }));
  const inScore = (d) =>
    (/sound mapper|primary sound|stereo mix|what u hear/i.test(d.name) ? -100 : 0) +
    (/loopback/i.test(d.name) ? -50 : 0) +
    (/e2x2|focusrite|scarlett|audient|motu|volt|steinberg|behringer/i.test(d.name) ? 35 : 0) +
    (/headrush/i.test(d.name) ? 30 : 0) +
    (/analog|analogue|inst|hi-z|line|input/i.test(d.name) ? 20 : 0) +
    (/microphone|webcam|camera|headset/i.test(d.name) ? -10 : 0) +
    ({ "Windows WASAPI": 6, "Core Audio": 6, ASIO: 8, MME: 3, "Windows DirectSound": 2, "Windows WDM-KS": 0 }[d.hostapi] ?? 1);

  const outScore = (d) =>
    (/sound mapper|primary sound/i.test(d.name) ? -100 : 0) +
    (/e2x2|focusrite|scarlett|audient|motu|volt|steinberg|behringer/i.test(d.name) ? 35 : 0) +
    (/headrush/i.test(d.name) ? 30 : 0) +
    (/playback|analog|headphones|line|out|speakers/i.test(d.name) ? 20 : 0) +
    ({ "Windows WASAPI": 6, "Core Audio": 6, ASIO: 8, MME: 3, "Windows DirectSound": 2, "Windows WDM-KS": 0 }[d.hostapi] ?? 1);

  const inputs = (devices || []).filter((d) => d.inputs > 0).sort((a, b) => inScore(b) - inScore(a));
  const outputs = (devices || []).filter((d) => d.outputs > 0).sort((a, b) => outScore(b) - outScore(a));

  for (const sel of document.querySelectorAll(".guitar-in-device")) {
    clear(sel);
    if (!inputs.length) {
      sel.append(el("option", { value: "", text: error ? "no audio devices" : "no input devices" }));
      continue;
    }
    const seen = new Set();
    for (const d of inputs) {
      if (seen.has(d.name)) continue;
      seen.add(d.name);
      sel.append(el("option", { value: d.name, text: `${d.name} (${d.inputs} in)` }));
    }
    if (pref.inputDevice && seen.has(pref.inputDevice)) {
      sel.value = pref.inputDevice;
    } else {
      pref.inputDevice = sel.value;
    }
  }

  for (const sel of document.querySelectorAll(".guitar-out-device")) {
    clear(sel);
    if (!outputs.length) {
      sel.append(el("option", { value: "", text: "no output devices" }));
      continue;
    }
    const seen = new Set();
    for (const d of outputs) {
      if (seen.has(d.name)) continue;
      seen.add(d.name);
      sel.append(el("option", { value: d.name, text: `${d.name} (${d.outputs} out)` }));
    }
    if (pref.outputDevice && seen.has(pref.outputDevice)) {
      sel.value = pref.outputDevice;
    } else {
      pref.outputDevice = sel.value;
    }
  }
}

/* --------------------- Step 1: Tuner, Gain Staging & Live Audio Monitoring */

function preflightCard(tuneObj) {
  const inDevice = el("select", {
    class: "guitar-in-device",
    title: "Audio interface input where your guitar or bass is plugged in",
    "aria-label": "Audio interface input",
    onchange: (e) => {
      pref.inputDevice = e.target.value;
      document.querySelectorAll(".guitar-in-device").forEach((s) => { s.value = e.target.value; });
      if (monitorActive) syncMonitor();
    },
  });
  const inChannel = el("input", {
    type: "number",
    class: "narrow guitar-in-channel",
    value: pref.inputChannel || 1,
    min: 1,
    max: 32,
    "aria-label": "Input channel",
    onchange: (e) => {
      pref.inputChannel = Number(e.target.value) || 1;
      document.querySelectorAll(".guitar-in-channel").forEach((c) => { c.value = pref.inputChannel; });
      if (monitorActive) syncMonitor();
    },
  });
  const outDevice = el("select", {
    class: "guitar-out-device",
    title: "Audio interface / headphone output for live monitoring",
    "aria-label": "Audio output device",
    onchange: (e) => {
      pref.outputDevice = e.target.value;
      if (monitorActive) syncMonitor();
    },
  });

  const listenCheck = el("input", {
    type: "checkbox",
    checked: pref.listen,
    onchange: (e) => {
      pref.listen = e.target.checked;
      if (monitorActive) syncMonitor();
    },
  });

  const gainValLabel = el("span", { class: "mono hint", text: `${pref.gainDb >= 0 ? "+" : ""}${pref.gainDb} dB` });
  const gainSlider = el("input", {
    type: "range",
    min: -24,
    max: 12,
    step: 1,
    value: pref.gainDb,
    style: "width:96px",
    title: "Live monitor playback volume trim",
    oninput: (e) => {
      pref.gainDb = Number(e.target.value) || 0;
      gainValLabel.textContent = `${pref.gainDb >= 0 ? "+" : ""}${pref.gainDb} dB`;
      if (monitorActive) syncMonitor();
    },
  });

  // --- Left Pane: Chromatic & Open-String Tuner ---
  const noteReadout = el("div", { class: "tuner-note", text: "--" });
  const hzReadout = el("div", { text: "-- Hz" });
  const centsReadout = el("div", { text: "play an open string" });
  const needle = el("div", { class: "tuner-needle", style: "left:50%" });
  const stringRow = el("div", { class: "string-targets" });

  const openNames = tuneObj?.strings || ["E1", "A1", "D2", "G2"];
  const openMidi = tuneObj?.open_midi || [28, 33, 38, 43];
  const openHz = tuneObj?.open_hz || openMidi.map((m) => Number((440 * Math.pow(2, (m - 69) / 12)).toFixed(2)));
  const count = openNames.length;
  const pillByString = new Map();

  // In instrument.py, open_midi is ordered lowest-to-highest (sN .. s1), where string 1 is highest pitch
  for (let idx = 0; idx < count; idx += 1) {
    const strNum = count - idx;
    const sName = openNames[idx];
    const sHz = openHz[idx];
    const badge = el("span", { text: `s${strNum} ${sName}` });
    const pill = el(
      "button",
      {
        type: "button",
        class: "string-pill",
        title: `Click to hear reference pitch for string ${strNum} (${sName}, ${sHz} Hz)`,
        onclick: () => playReferenceTone(sHz),
      },
      badge,
      el("small", { text: `${sHz} Hz 🔊` })
    );
    pillByString.set(strNum, { pill, badge, sName, strNum });
    stringRow.append(pill);
  }

  // --- Right Pane: Gain Staging & Level Meter ---
  const rmsFill = el("div", { class: "gain-fill-rms" });
  const peakFill = el("div", { class: "gain-fill-peak" });
  const holdTick = el("div", { class: "gain-hold-tick" });
  const peakStat = el("div", { class: "v", text: "-- dB" });
  const holdStat = el("div", { class: "v", text: "-- dB" });
  const floorStat = el("div", { class: "v", text: "-- dB" });
  const headroomStat = el("div", { class: "v", text: "-- dB" });
  const coachBanner = el("div", {
    class: "coach-banner",
    text: "Click 'Start Live Tuner & Meter' and play your hardest notes to dial in your interface input gain (-18 to -6 dBFS sweet spot).",
  });

  const checkPlayerHost = el("div", { class: "row", hidden: true });

  const dbToPct = (db) => Math.max(0, Math.min(100, ((Math.max(-60, Math.min(0, db ?? -60)) + 60) / 60) * 100));

  function updateTelemetry({ level, tuner }) {
    if (level) {
      const pPct = dbToPct(level.peak_db);
      const rPct = dbToPct(level.rms_db ?? level.noise_floor_db);
      const hPct = dbToPct(level.peak_hold_db ?? level.peak_db);
      peakFill.style.width = `${pPct.toFixed(1)}%`;
      rmsFill.style.width = `${rPct.toFixed(1)}%`;
      holdTick.style.left = `${hPct.toFixed(1)}%`;

      peakStat.textContent = `${level.peak_db.toFixed(1)} dB`;
      holdStat.textContent = `${(level.peak_hold_db ?? level.peak_db).toFixed(1)} dB`;
      floorStat.textContent = `${level.noise_floor_db.toFixed(0)} dB`;
      headroomStat.textContent = level.clipping ? "CLIP!" : `${(level.headroom_db ?? Math.max(0, -level.peak_db)).toFixed(1)} dB`;

      coachBanner.textContent = level.coaching || `${level.verdict}: ${level.advice}`;
      coachBanner.className =
        "coach-banner " +
        (level.clipping || level.verdict === "too hot"
          ? "bad"
          : level.in_sweet_spot
          ? "ok"
          : level.verdict === "good" || level.verdict === "too quiet" || level.verdict === "noisy"
          ? "warn"
          : "");
    }

    if (tuner) {
      pillByString.forEach(({ pill }) => pill.classList.remove("active"));
      if (tuner.pitched && tuner.note) {
        const cents = tuner.target_cents ?? tuner.cents ?? 0;
        const clamped = Math.max(-50, Math.min(50, cents));
        const leftPct = 50 + (clamped / 50) * 46;
        needle.style.left = `${leftPct.toFixed(1)}%`;
        needle.classList.toggle("in-tune", Boolean(tuner.in_tune));

        noteReadout.textContent = tuner.note;
        noteReadout.className = `tuner-note ${tuner.in_tune ? "in-tune" : "off-tune"}`;
        hzReadout.textContent = `${tuner.hz.toFixed(1)} Hz → s${tuner.target_string} ${tuner.target_note} (${tuner.target_hz} Hz)`;
        const sign = cents > 0 ? "+" : "";
        centsReadout.textContent = tuner.in_tune
          ? `IN TUNE (${sign}${cents.toFixed(1)}c)`
          : cents < 0
          ? `${sign}${cents.toFixed(1)}c (FLAT · tune UP ▲)`
          : `${sign}${cents.toFixed(1)}c (SHARP · tune DOWN ▼)`;

        const matchPill = pillByString.get(tuner.target_string);
        if (matchPill) {
          matchPill.pill.classList.add("active");
          if (tuner.in_tune) {
            tunedStrings.add(tuner.target_string);
            matchPill.pill.classList.add("tuned");
            matchPill.badge.textContent = `s${matchPill.strNum} ${matchPill.sName} ✓`;
          }
        }
      } else {
        noteReadout.textContent = "--";
        noteReadout.className = "tuner-note";
        hzReadout.textContent = "Listening...";
        centsReadout.textContent = "Pluck a single string";
        needle.classList.remove("in-tune");
      }
    }
  }

  const toggleBtn = el("button", {
    class: "btn primary",
    text: "Start Live Tuner & Meter",
    onclick: () => toggleMonitor(),
  });

  async function syncMonitor() {
    if (!inDevice.value) return;
    const status = await attempt(() =>
      api.post("/api/loop/monitor/start", {
        input_device: inDevice.value,
        input_channel: Number(inChannel.value) || 1,
        output_device: outDevice.value || null,
        output_channels: [1, 2],
        listen: Boolean(listenCheck.checked),
        gain_db: Number(gainSlider.value) || 0,
        tuning: profile.tuning,
      })
    );
    if (status) updateTelemetry(status);
  }

  async function toggleMonitor() {
    if (monitorActive) {
      await stopLiveMonitor();
      toggleBtn.textContent = "Start Live Tuner & Meter";
      toggleBtn.className = "btn primary";
      return;
    }
    if (!inDevice.value) {
      toast("Select the audio input your instrument is plugged into", { kind: "warn" });
      return;
    }
    toggleBtn.disabled = true;
    try {
      const started = await attempt(() =>
        api.post("/api/loop/monitor/start", {
          input_device: inDevice.value,
          input_channel: Number(inChannel.value) || 1,
          output_device: outDevice.value || null,
          output_channels: [1, 2],
          listen: Boolean(listenCheck.checked),
          gain_db: Number(gainSlider.value) || 0,
          tuning: profile.tuning,
        })
      );
      if (!started?.active) return;
      monitorActive = true;
      toggleBtn.textContent = "Stop Live Tuner & Meter";
      toggleBtn.className = "btn danger";
      updateTelemetry(started);
      monitorTimer = setInterval(async () => {
        if (!monitorActive) return;
        try {
          const data = await api.get("/api/loop/monitor", { tuning: profile.tuning });
          if (data?.active) updateTelemetry(data);
        } catch (_) {}
      }, 150);
    } finally {
      toggleBtn.disabled = false;
    }
  }

  const checkClipBtn = el("button", {
    class: "btn",
    text: "Record 3.5s & Play Back",
    title: "Record 3.5 seconds from your interface, check gain staging & tuning, and immediately hear it back",
    onclick: (event) =>
      attempt(async () => {
        if (!inDevice.value) throw new Error("Choose the input your instrument is plugged into");
        const btn = event.currentTarget;
        btn.disabled = true;
        btn.textContent = "Recording 3.5s — play now!";
        try {
          const res = await api.post("/api/loop/check-clip", {
            device: inDevice.value,
            channel: Number(inChannel.value) || 1,
            seconds: 3.5,
            tuning: profile.tuning,
          });
          updateTelemetry(res);
          clear(checkPlayerHost);
          const audioEl = el("audio", { controls: true, src: `${res.url}?t=${Date.now()}`, style: "height:32px" });
          checkPlayerHost.append(
            el("span", { class: "hint", text: `Captured ${res.seconds}s from ${res.device} (ch ${res.channel}):` }),
            audioEl
          );
          checkPlayerHost.hidden = false;
          audioEl.play().catch(() => {});
          toast(res.level?.coaching || `${res.level?.verdict}: peak ${res.level?.peak_db} dBFS`, {
            kind: res.level?.verdict === "good" ? "ok" : "warn",
            timeout: 7000,
          });
        } finally {
          btn.disabled = false;
          btn.textContent = "Record 3.5s & Play Back";
        }
      }),
  });

  return el(
    "div",
    { class: "card", id: "preflight-card" },
    el(
      "div",
      { class: "scenes-head" },
      el("h3", { text: "Step 1 · Tune, Gain Stage & Live Monitor" }),
      el("span", { class: "hint", text: "Dial in your audio interface input level (-18 to -6 dBFS) and tune every string before teaching rigsmith." })
    ),
    el(
      "div",
      { class: "row" },
      el("span", { class: "hint", text: "Input" }),
      inDevice,
      el("label", { class: "check hint" }, "ch", inChannel),
      el("span", { class: "hint", text: "Output" }),
      outDevice,
      el("label", { class: "check hint", title: "Route live instrument input directly to the selected output device" }, listenCheck, "Live Hear-Through"),
      gainSlider,
      gainValLabel
    ),
    el(
      "div",
      { class: "row" },
      toggleBtn,
      checkClipBtn,
      el("button", {
        class: "btn small ghost",
        text: "Reset peak hold",
        onclick: () => {
          holdStat.textContent = "-- dB";
          if (monitorActive) api.get("/api/loop/monitor", { tuning: profile.tuning, reset_peak: true }).catch(() => {});
        },
      })
    ),
    checkPlayerHost,
    el(
      "div",
      { class: "preflight-grid" },
      // Tuner pane
      el(
        "div",
        { class: "pf-pane" },
        el(
          "div",
          { class: "pf-pane-head" },
          el("span", { class: "pf-pane-title", text: `Chromatic & String Tuner (${profile.tuning})` }),
          el("span", { class: "hint", text: "Click a string for reference tone" })
        ),
        el("div", { class: "tuner-display" }, noteReadout, el("div", { class: "tuner-meta" }, hzReadout, centsReadout)),
        el("div", { class: "tuner-gauge" }, el("div", { class: "tuner-sweet" }), el("div", { class: "tuner-center" }), needle),
        el("div", { class: "tuner-scale" }, el("span", { text: "-50c (flat)" }), el("span", { text: "-5c · 0 · +5c" }), el("span", { text: "+50c (sharp)" })),
        stringRow
      ),
      // Gain staging pane
      el(
        "div",
        { class: "pf-pane" },
        el(
          "div",
          { class: "pf-pane-head" },
          el("span", { class: "pf-pane-title", text: "Interface Gain Staging & Headroom" }),
          el("span", { class: "hint", text: "Sweet spot: -18 to -6 dBFS" })
        ),
        el(
          "div",
          { class: "gain-meter-wrap" },
          el(
            "div",
            { class: "gain-track" },
            el("div", { class: "gain-sweet-zone" }),
            el("div", { class: "gain-sweet-label", text: "SWEET SPOT" }),
            rmsFill,
            peakFill,
            holdTick
          ),
          el(
            "div",
            { class: "gain-scale" },
            el("span", { text: "-60 dB" }),
            el("span", { text: "-36 dB" }),
            el("span", { text: "-18 dB" }),
            el("span", { text: "-6 dB" }),
            el("span", { text: "0 dBFS" })
          )
        ),
        el(
          "div",
          { class: "gain-stats" },
          el("div", { class: "gain-stat" }, el("div", { class: "k", text: "Peak" }), peakStat),
          el("div", { class: "gain-stat" }, el("div", { class: "k", text: "Peak Hold" }), holdStat),
          el("div", { class: "gain-stat" }, el("div", { class: "k", text: "Noise Floor" }), floorStat),
          el("div", { class: "gain-stat" }, el("div", { class: "k", text: "Headroom" }), headroomStat)
        ),
        coachBanner
      )
    )
  );
}

/* ------------------------------------------- Step 2: The Guided Session */

function sessionCard() {
  const box = el(
    "div",
    { class: "card", id: "guide-card" },
    el(
      "div",
      { class: "scenes-head" },
      el("h3", { text: "Step 2 · Teach it (Guided Session)" }),
      el("span", { class: "hint", text: "Once tuned and gain-staged above, record a guided pass so rigsmith learns every string and fret." })
    )
  );
  const protocol = el("select", { "aria-label": "What to play" });
  for (const [value, label] of [
    ["quick", "Quick - four notes a string (1 min)"],
    ["chromatic", "Every note on every string (most accurate)"],
    ["dynamics", "Dynamics - soft, medium, hard"],
    ["techniques", "Techniques - mutes, thumb/dig-in, and double-stops"],
    ["free", "Anything - play freely"],
  ]) {
    protocol.append(el("option", { value, text: label }));
  }
  const frets = el("input", { type: "number", class: "narrow", value: 12, min: 1, max: 24, "aria-label": "Highest fret to play" });
  const fretsField = el("label", { class: "check hint", id: "guide-frets", hidden: true }, "to fret", frets);
  protocol.addEventListener("change", () => (fretsField.hidden = protocol.value !== "chromatic"));

  const device = el("select", {
    class: "guitar-in-device",
    title: "Where your instrument is plugged in",
    "aria-label": "Where your instrument is plugged in",
    onchange: (e) => {
      pref.inputDevice = e.target.value;
      document.querySelectorAll(".guitar-in-device").forEach((s) => { s.value = e.target.value; });
    },
  });
  const channel = el("input", {
    type: "number",
    class: "narrow guitar-in-channel",
    value: pref.inputChannel || 1,
    min: 1,
    max: 32,
    "aria-label": "Input channel",
    onchange: (e) => {
      pref.inputChannel = Number(e.target.value) || 1;
      document.querySelectorAll(".guitar-in-channel").forEach((c) => { c.value = pref.inputChannel; });
    },
  });
  const zone = dropZone({ label: "...or drop a recording of the protocol", accept: "audio/*" });
  const stage = el("div", { id: "guide-stage" });

  const learnFrom = async (source, protocolId) => {
    await attempt(async () => {
      const { job_id } = await api.post(`/api/learn/profiles/${profileId}/learn`, {
        source,
        protocol_id: protocolId,
        frets: Number(frets.value) || null,
      });
      stage.textContent = "learning...";
      const report = await track(job_id, { onUpdate: (job) => (stage.textContent = job.message || job.state) });
      clear(stage).append(el("pre", { class: "out", text: report.render }));
      toast(`${report.samples_added} notes learned`, { kind: "ok" });
      refreshPickups();
      profile = await api.get(`/api/learn/profiles/${profileId}`, { samples: true });
      const keep = stage.cloneNode(true);
      render();
      $("#guide-stage")?.replaceWith(keep);
    });
  };

  box.append(
    el("div", { class: "row" }, protocol, fretsField, el("span", { class: "hint", text: "plugged into" }), device, el("label", { class: "check hint" }, "ch", channel)),
    el(
      "div",
      { class: "row" },
      el("button", {
        class: "btn primary",
        text: "Start the guided session",
        onclick: () => guide(protocol.value, Number(frets.value), device.value, Number(channel.value), stage, learnFrom),
      }),
      el("span", { class: "hint", text: "counts you in, clicks, and records while you play" }),
      el("span", { class: "grow" }),
      el("button", {
        class: "btn small",
        text: "Quick 3s level check",
        title: "Play hard for three seconds - nothing is saved",
        onclick: (event) =>
          attempt(async () => {
            if (!device.value) throw new Error("Choose the input your instrument is plugged into");
            const button = event.target;
            button.disabled = true;
            button.textContent = "play hard now...";
            try {
              const result = await api.get("/api/loop/level", { device: device.value, channel: Number(channel.value) || 1, seconds: 2.5 });
              toast(result.coaching || `${result.verdict}: peak ${result.peak_db} dBFS, noise ${result.noise_floor_db} dBFS - ${result.advice}`, {
                kind: result.verdict === "good" ? "ok" : "warn",
                timeout: 9000,
              });
            } finally {
              button.disabled = false;
              button.textContent = "Quick 3s level check";
            }
          }),
      })
    ),
    zone,
    el("div", { class: "row" }, el("button", { class: "btn", text: "Learn the dropped recording", onclick: () => (zone.path() ? learnFrom(zone.path(), protocol.value) : toast("Drop a recording first", { kind: "error" })) })),
    stage
  );
  return box;
}

async function guide(protocolId, frets, device, channel, stage, learnFrom) {
  if (protocolId === "free") {
    toast("The free protocol has no steps - record anything and drop it in", { kind: "warn" });
    return;
  }
  await stopLiveMonitor();
  const protocol = await attempt(() => api.get(`/api/learn/protocols/${protocolId}`, { tuning: profile.tuning, frets, click: true }));
  if (!protocol) return;
  const seconds = protocol.seconds + protocol.count_in + 2;
  clear(stage);
  const big = el("div", { class: "big-step", text: "get ready" });
  const chips = el("div", { class: "protocol-steps" }, ...protocol.steps.map((s) => el("span", { class: "step-chip", text: s.label })));
  stage.append(big, chips);

  let recording = null;
  if (device) {
    recording = api.post("/api/loop/record", {
      seconds,
      device,
      channel,
      take_id: `${profileId}-${protocolId}`,
      guitar: profile.guitar,
      pickup: profile.pickup,
      tuning: profile.tuning,
      note: `${protocol.name} for the profile`,
    });
  } else {
    toast("No input device chosen - follow along, then drop your recording in", { kind: "warn" });
  }
  const click = new Audio(protocol.click_url);
  await click.play().catch(() => {});
  const startedAt = performance.now();
  const total = protocol.steps.length;
  session = setInterval(() => {
    const elapsed = (performance.now() - startedAt) / 1000;
    if (elapsed < protocol.count_in) {
      big.textContent = `${Math.ceil(protocol.count_in - elapsed)}`;
      return;
    }
    const index = Math.floor((elapsed - protocol.count_in) / protocol.spacing_s);
    if (index >= total) {
      clearInterval(session);
      big.textContent = "done - learning...";
      return;
    }
    const step = protocol.steps[index];
    clear(big).append(step.prompt, el("small", { text: `${step.label}  -  ${index + 1} of ${total}` }));
    chips.querySelectorAll(".step-chip").forEach((chip, i) => {
      chip.classList.toggle("now", i === index);
      chip.classList.toggle("done", i < index);
    });
  }, 80);

  if (recording) {
    const started = await attempt(() => recording);
    if (!started) {
      clearInterval(session);
      return;
    }
    const take = await attempt(() => track(started.job_id));
    clearInterval(session);
    if (take) await learnFrom(take.take_id, protocolId);
  }
}

/* ----------------------------------------------------------- the neck */

function fretboardCard() {
  const tune = tunings.find((t) => t.name === profile.tuning);
  const openMidi = tune ? [...tune.open_midi].reverse() : [64, 59, 55, 50, 45, 40];
  const grid = profile.coverage.grid;
  const frets = profile.coverage.frets;
  const table = el("table");
  const head = el("tr", {}, el("th", { text: "" }), ...Array.from({ length: frets + 1 }, (_, f) => el("th", { text: String(f) })));
  table.append(head);
  for (let s = 1; s <= grid.length; s += 1) {
    const row = el("tr", {}, el("th", { text: `s${s}` }));
    for (let f = 0; f <= frets; f += 1) {
      const count = grid[s - 1][f];
      const midi = (openMidi[s - 1] ?? 40) + f;
      row.append(
        el("td", {
          class: `${count > 1 ? "has2" : count ? "has" : ""} ${[3, 5, 7, 9, 12, 15, 17, 19, 21, 24].includes(f) ? "marker" : ""}`,
          title: `${noteName(midi)} - ${count ? `${count} sample(s)` : "not recorded; the voice model or a shift plays it"}`,
          text: count ? noteName(midi) : "",
          onclick: () => attempt(async () => playUrl((await api.post(`/api/learn/profiles/${profileId}/note`, { midi, string: s })).url)),
        })
      );
    }
    table.append(row);
  }
  return el(
    "div",
    { class: "card" },
    el("div", { class: "scenes-head" }, el("h3", { text: "What it has heard" }), el("span", { class: "hint", text: "Green cells are real samples. Click any cell to hear the note." })),
    el("div", { class: "fretboard" }, table)
  );
}

function tryCard(isBass = false) {
  const defaultRiff = isBass
    ? "# bass groove: :4 quarter, :8 eighth, :16, - rest, x2 repeat, ! accent\n@112  E1:8 E1:8 G1:8 A1:4  D2:8 A1:8 G1:8  (E1 E1 G1 A1)x2 E1!"
    : "# notes in order: :4 quarter, :16, :8. dotted, + chord, - rest, x2 repeat, ! accent\n@120  E2 G2 A2:4  (E2+B2+E3):2  -  (A2 A#2)x2 A2!";
  const text = el("textarea", { rows: 4, class: "grow", style: "width:100%;font-family:var(--mono)", "aria-label": "Riff, as notes", text: defaultRiff });
  const noise = el("input", { type: "checkbox" });
  const like = likeControls({ except: profileId });
  const out = el("div", { class: "hint", style: "white-space:pre-wrap" });
  const play = (withLike) =>
    attempt(async () => {
      const result = await api.post(`/api/learn/profiles/${profileId}/play`, { notes: text.value, noise: noise.checked, like: withLike ? like.like : null, amount: like.amount });
      out.textContent = result.describe;
      playUrl(result.url);
    });
  return el(
    "div",
    { class: "card" },
    el("div", { class: "scenes-head" }, el("h3", { text: "Play something on it" }), el("span", { class: "hint", text: "Sound like moves the pickup's resonance - e.g. P-Bass to J-Bass or Humbucker to Strat." })),
    text,
    el(
      "div",
      { class: "row" },
      el("label", { class: "check hint" }, noise, "include the instrument's own hum and hiss"),
      like.node,
      el("span", { class: "grow" }),
      el("button", { class: "btn small", text: "as played", title: "The same riff without the pickup move - for A/B", onclick: () => play(false) }),
      el("button", { class: "btn primary", text: "Play", onclick: () => play(true) })
    ),
    out
  );
}

function fingerprintCard() {
  return el("div", { class: "card" }, el("div", { class: "scenes-head" }, el("h3", { text: "Its voice" }), el("span", { class: "hint", text: "Spectral balance across every note it has heard, third-octave bands." })), el("div", { class: "chart", id: "fingerprint-chart" }));
}

function drawFingerprint() {
  const print = profile.fingerprint;
  const host = $("#fingerprint-chart");
  if (!host || !print?.bands_db?.length) return;
  lineChart(host, { x: print.bands_hz, logX: true, height: 170, yLabel: "dB", series: [{ label: "balance", values: print.bands_db, color: COLOURS.amber, points: true }] });
}

function samplesCard() {
  const list = el("div", { class: "list" });
  for (const sample of (profile.samples || []).slice(0, 300)) {
    list.append(
      el(
        "div",
        { class: "item" },
        el("span", { class: "mono", text: sample.name }),
        el("span", { class: "meta", text: sample.string ? `s${sample.string} f${sample.fret}` : "" }),
        el("span", { class: "meta", text: `${sample.cents >= 0 ? "+" : ""}${sample.cents.toFixed(0)}c` }),
        el("span", { class: "meta", text: `${sample.peak_db.toFixed(1)} dB ${sample.dynamic}` }),
        el("span", { class: "meta", text: sample.technique !== "pick" ? sample.technique : "" }),
        el("span", { class: "spacer" }),
        el("button", { class: "btn small", text: "play", onclick: () => attempt(async () => playUrl((await api.get(`/api/learn/profiles/${profileId}/samples/${sample.sample_id}/audio`)).url)) }),
        el("button", { class: "btn small ghost danger", text: "drop", title: "Remove a bad sample", onclick: () => attempt(async () => { profile = await api.del(`/api/learn/profiles/${profileId}/samples/${sample.sample_id}`); await load(profileId); }) })
      )
    );
  }
  return el("details", { class: "card" }, el("summary", { class: "hint", text: `${(profile.samples || []).length} samples` }), list);
}

async function demo() {
  await attempt(async () => {
    const result = await api.post("/api/learn/demo", {}, { protocol_id: "quick", into: "synthetic-demo" });
    toast(`A synthetic guitar learned ${result.report.samples_added} notes - it says so everywhere it appears`, { kind: "ok" });
    await load(await refreshList("synthetic-demo"));
  });
}

export async function init() {
  tunings = (await api.get("/api/learn/tunings").catch(() => ({ tunings: [] }))).tunings;
  $("#profile-new").onclick = newProfile;
  $("#profile-delete").onclick = async () => {
    if (!profileId) return;
    const answer = await prompt(`Delete "${profile?.guitar || profileId}"?`, [{ type: "note", text: "Its samples and voice model go too. Recordings in My playing stay." }], { okLabel: "Delete" });
    if (!answer) return;
    await stopLiveMonitor();
    await attempt(() => api.del(`/api/learn/profiles/${profileId}`));
    await load(await refreshList(null));
  };
  $("#profile-select").onchange = (event) => load(event.target.value || null);
  await load(await refreshList());
}

export function onHide() {
  player.pause();
  clearInterval(session);
  stopLiveMonitor();
}

onArrive("guitar", async ({ profileId: wanted }) => {
  if (wanted) await load(await refreshList(wanted));
});
