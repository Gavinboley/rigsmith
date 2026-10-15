/* Guitar lab: teach rigsmith your guitar, then hear what it learned.
 *
 * The guided session is the heart of it. Pick a protocol, press start, and
 * the page counts you in and walks a cursor through the steps in time with a
 * click while the interface records - so every note arrives knowing which
 * string and fret it was. The fretboard afterwards shows what is covered,
 * and every cell plays.
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
const player = new Audio();

const NOTE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const noteName = (midi) => `${NOTE[Math.round(midi) % 12]}${Math.floor(Math.round(midi) / 12) - 1}`;

function playUrl(url) {
  document.querySelectorAll("audio").forEach((a) => a.pause());
  player.src = url;
  player.play();
}

async function refreshList(select = profileId) {
  const { profiles } = await api.get("/api/learn/profiles");
  const picker = clear($("#profile-select"));
  if (!profiles.length) picker.append(el("option", { value: "", text: "No guitars yet" }));
  for (const item of profiles) picker.append(el("option", { value: item.profile_id, text: item.label || item.profile_id }));
  const wanted = select && profiles.some((p) => p.profile_id === select) ? select : profiles[0]?.profile_id || null;
  picker.value = wanted || "";
  return wanted;
}

async function newProfile() {
  if (!tunings.length) tunings = (await api.get("/api/learn/tunings")).tunings;
  const answer = await prompt("A new guitar", [
    { type: "note", text: "One profile per guitar and pickup - the bridge and neck pickups of one guitar are two instruments as far as an amp is concerned." },
    { name: "name", label: "Name", placeholder: "LP bridge" },
    { name: "guitar", label: "Guitar", placeholder: "Les Paul Standard" },
    { name: "pickup", label: "Pickup", placeholder: "bridge humbucker" },
    { name: "tuning", label: "Tuning", type: "select", value: "E standard", options: tunings.map((t) => ({ value: t.name, label: `${t.name} (${t.strings.join(" ")})` })) },
  ], { okLabel: "Create" });
  if (!answer?.name) return;
  const made = await attempt(() => api.post("/api/learn/profiles", answer));
  if (made) await load(await refreshList(made.profile_id));
}

async function load(id) {
  profileId = id;
  const host = clear($("#guitar-body"));
  if (!id) {
    host.append(
      el(
        "div",
        { class: "rig-empty" },
        el("p", { text: "No guitars learned yet. Play a short guided sequence once and rigsmith can play any part on your guitar - including a song's part, on the record's clock." }),
        el("button", { class: "btn primary", text: "Add my guitar", onclick: newProfile }),
        " ",
        el("button", { class: "btn", text: "Try with a synthetic guitar", onclick: demo })
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
  const print = profile.fingerprint || {};
  const stat = (k, v) => el("div", { class: "stat" }, el("div", { class: "k", text: k }), el("div", { class: "v", text: v }));
  const picks = (profile.samples || []).filter((s) => s.technique === "pick");
  const range = picks.length ? `${noteName(Math.min(...picks.map((s) => s.midi)))}-${noteName(Math.max(...picks.map((s) => s.midi)))}` : "--";
  host.append(
    el(
      "div",
      { class: "card" },
      el("div", { class: "chain-head" }, el("h2", { class: "rig-name", text: [profile.guitar, profile.pickup].filter(Boolean).join(" ") || profile.profile_id }), el("span", { class: "tag", text: profile.tuning }), profile.has_voice ? el("span", { class: "tag a2", text: profile.voice_net ? "voice model + network" : "voice model", title: profile.voice_net ? "Unheard frets come from interpolation averaged with a small network trained on every note - a few percent closer on held-out notes" : "Unheard frets come from interpolating the nearest notes; a network joins in from 48 notes" }) : null),
      profile.note ? el("div", { class: "hint", text: profile.note }) : null,
      el(
        "div",
        { class: "stat-grid" },
        stat("samples", String((profile.samples || []).length)),
        stat("range", range),
        stat("covered to fret 12", `${profile.coverage.filled}/${profile.coverage.positions}`),
        stat("typical peak", `${(print.median_peak_db ?? -120).toFixed(1)} dBFS`),
        stat("noise floor", `${(print.noise_floor_db ?? -120).toFixed(0)} dBFS`),
        stat("tuning", print.tuning_cents == null ? "--" : Math.abs(print.tuning_cents) < 0.5 ? "in tune" : `${print.tuning_cents > 0 ? "+" : ""}${print.tuning_cents.toFixed(0)} c`),
        stat("brightness", `${Math.round(print.centroid_hz || 0)} Hz`),
        stat("hum", print.hum_hz ? `${print.hum_hz} Hz` : "none"),
        profile.resonance ? el("div", { class: "stat", title: profile.resonance.describe }, el("div", { class: "k", text: "pickup resonance" }), el("div", { class: "v", text: `${(profile.resonance.f0_hz / 1000).toFixed(1)} kHz Q${profile.resonance.q.toFixed(1)}` })) : null,
        profile.resonance ? stat("reads as", profile.resonance.clear ? profile.resonance.reads_as : "no clear peak") : null
      )
    ),
    sessionCard(),
    fretboardCard(),
    tryCard(),
    fingerprintCard(),
    samplesCard()
  );
  drawFingerprint();
}

/* ----------------------------------------------------------- the session */

function sessionCard() {
  const box = el("div", { class: "card", id: "guide-card" }, el("h3", { text: "Teach it" }));
  const protocol = el("select");
  for (const [value, label] of [["quick", "Quick - four notes a string (1 min)"], ["chromatic", "Every note on every string (most accurate)"], ["dynamics", "Dynamics - soft, medium, hard"], ["techniques", "Techniques - mutes and chords"], ["free", "Anything - play freely"]]) {
    protocol.append(el("option", { value, text: label }));
  }
  const frets = el("input", { type: "number", class: "narrow", value: 12, min: 1, max: 24, title: "frets (chromatic)" });
  const device = el("select", { title: "Where your guitar is plugged in" });
  const channel = el("input", { type: "number", class: "narrow", value: 1, min: 1, max: 32, title: "input channel" });
  const zone = dropZone({ label: "...or drop a recording of the protocol", accept: "audio/*" });
  const stage = el("div", { id: "guide-stage" });

  api.get("/api/loop/devices").then(({ devices, error }) => {
    clear(device);
    // The jack the guitar is in, first. The Windows "Sound Mapper" and
    // "Primary" pseudo-devices go through the mixer, and an interface's
    // loopback ports record the computer's own playback - neither is a DI.
    // WDM-KS repeats each device without the box's name, so it goes last.
    const score = (d) =>
      (/sound mapper|primary sound|stereo mix|what u hear/i.test(d.name) ? -100 : 0) +
      (/loopback/i.test(d.name) ? -50 : 0) +
      (/headrush/i.test(d.name) ? 30 : 0) +
      (/analog|analogue|inst|hi-z|line|input/i.test(d.name) ? 20 : 0) +
      (/microphone|webcam|camera|headset/i.test(d.name) ? -10 : 0) +
      ({ "Windows WASAPI": 6, "Core Audio": 6, ASIO: 8, MME: 3, "Windows DirectSound": 2, "Windows WDM-KS": 0 }[d.hostapi] ?? 1);
    const inputs = (devices || []).filter((d) => d.inputs > 0).sort((a, b) => score(b) - score(a));
    if (!inputs.length) device.append(el("option", { value: "", text: error ? "no audio devices (sounddevice?)" : "no input devices" }));
    const seen = new Set();
    for (const d of inputs) {
      if (seen.has(d.name)) continue;
      seen.add(d.name);
      device.append(el("option", { value: d.name, text: `${d.name} (${d.inputs} in)` }));
    }
  });

  const learnFrom = async (source, protocolId) => {
    await attempt(async () => {
      const { job_id } = await api.post(`/api/learn/profiles/${profileId}/learn`, { source, protocol_id: protocolId, frets: Number(frets.value) || null });
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
    el("div", { class: "row" }, protocol, frets, el("span", { class: "hint", text: "plugged into" }), device, el("span", { class: "hint", text: "ch" }), channel),
    el(
      "div",
      { class: "row" },
      el("button", { class: "btn primary", text: "Start the guided session", onclick: () => guide(protocol.value, Number(frets.value), device.value, Number(channel.value), stage, learnFrom) }),
      el("span", { class: "hint", text: "counts you in, clicks, and records while you play" }),
      el("span", { class: "grow" }),
      el("button", {
        class: "btn small",
        text: "check my level",
        title: "Play hard for three seconds - nothing is saved",
        onclick: (event) => attempt(async () => {
          if (!device.value) throw new Error("Choose the input your guitar is plugged into");
          const button = event.target;
          button.disabled = true;
          button.textContent = "play hard now...";
          try {
            const result = await api.get("/api/loop/level", { device: device.value, channel: Number(channel.value) || 1, seconds: 3 });
            toast(`${result.verdict}: peak ${result.peak_db} dBFS, noise ${result.noise_floor_db} dBFS - ${result.advice}`, { kind: result.verdict === "good" ? "ok" : "warn", timeout: 9000 });
          } finally {
            button.disabled = false;
            button.textContent = "check my level";
          }
        }),
      })
    ),
    zone,
    el("div", { class: "row" }, el("button", { class: "btn", text: "Learn the dropped recording", onclick: () => zone.path() ? learnFrom(zone.path(), protocol.value) : toast("Drop a recording first", { kind: "error" }) })),
    stage
  );
  return box;
}

async function guide(protocolId, frets, device, channel, stage, learnFrom) {
  if (protocolId === "free") {
    toast("The free protocol has no steps - record anything and drop it in", { kind: "warn" });
    return;
  }
  const protocol = await attempt(() => api.get(`/api/learn/protocols/${protocolId}`, { tuning: profile.tuning, frets, click: true }));
  if (!protocol) return;
  const seconds = protocol.seconds + protocol.count_in + 2;
  clear(stage);
  const big = el("div", { class: "big-step", text: "get ready" });
  const chips = el("div", { class: "protocol-steps" }, ...protocol.steps.map((s) => el("span", { class: "step-chip", text: s.label })));
  stage.append(big, chips);

  let recording = null;
  if (device) {
    recording = api.post("/api/loop/record", { seconds, device, channel, take_id: `${profileId}-${protocolId}`, guitar: profile.guitar, pickup: profile.pickup, tuning: profile.tuning, note: `${protocol.name} for the profile` });
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
    if (!started) return;
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
  return el("div", { class: "card" }, el("div", { class: "scenes-head" }, el("h3", { text: "What it has heard" }), el("span", { class: "hint", text: "Green cells are real samples. Click any cell to hear the note." })), el("div", { class: "fretboard" }, table));
}

function tryCard() {
  const text = el("textarea", { rows: 4, class: "grow", style: "width:100%;font-family:var(--mono)", text: "# notes in order: :4 quarter, :16, :8. dotted, + chord, - rest, x2 repeat, ! accent\n@120  E2 G2 A2:4  (E2+B2+E3):2  -  (A2 A#2)x2 A2!" });
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
    el("div", { class: "scenes-head" }, el("h3", { text: "Play something on it" }), el("span", { class: "hint", text: "Sound like moves the pickup's resonance - a humbucker heard as a Strat, or any amount between." })),
    text,
    el(
      "div",
      { class: "row" },
      el("label", { class: "check hint" }, noise, "include the guitar's own hum and hiss"),
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
    await attempt(() => api.del(`/api/learn/profiles/${profileId}`));
    await load(await refreshList(null));
  };
  $("#profile-select").onchange = (event) => load(event.target.value || null);
  await load(await refreshList());
}

export function onHide() {
  player.pause();
  clearInterval(session);
}

onArrive("guitar", async ({ profileId: wanted }) => {
  if (wanted) await load(await refreshList(wanted));
});
