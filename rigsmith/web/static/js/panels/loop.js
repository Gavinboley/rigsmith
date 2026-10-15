/* Loop: the real Core, between an output and an input.
 *
 * Wiring it right takes a minute with the manual open and getting it wrong
 * is silent (the Core's USB channels 1-2 bypass the rig), so this panel
 * suggests routes for what is plugged in, says what is wrong with each, and
 * checks the wiring with a tone before anything is measured through it.
 */

import { api } from "../api.js";
import { COLOURS, lineChart } from "../charts.js";
import { describeJob, track } from "../jobs.js";
import { $, attempt, clear, el, prompt, toast } from "../ui.js";
import { dropZone } from "../upload.js";

let metering = null;

function deviceList(devices, hardware, error) {
  if (error) return el("div", { class: "notice", text: `No audio devices: ${error}` });
  const list = el("div", { class: "list" });
  for (const box of hardware || []) {
    list.append(
      el(
        "div",
        { class: "item" },
        el("span", { class: `tag ${box.headrush ? "a2" : ""}`, text: box.headrush ? "HeadRush" : box.inputs.length && box.outputs.length ? "interface" : box.inputs.length ? "input" : "output" }),
        el("span", { class: "title", text: box.name }),
        el("span", { class: "meta", text: [box.inputs.length ? `in: ${box.inputs.join(", ")}` : "", box.outputs.length ? `out: ${box.outputs.join(", ")}` : ""].filter(Boolean).join("  |  ") }),
        el("span", { class: "spacer" }),
        el("span", { class: "meta", text: (box.hostapis || []).map((h) => h.replace("Windows ", "")).join(", ") })
      )
    );
  }
  if (!(hardware || []).length) list.append(el("div", { class: "hint", text: "Nothing visible." }));
  // Every PortAudio device, for setting a route up by hand.
  const raw = el("details", {}, el("summary", { class: "hint", text: `every device the system lists (${devices.length})` }));
  const table = el("div", { class: "list" });
  for (const device of devices) {
    table.append(el("div", { class: "item" }, el("span", { class: "tag", text: (device.hostapi || "").replace("Windows ", "") }), el("span", { class: "title", text: device.name }), el("span", { class: "spacer" }), el("span", { class: "meta", text: `${device.inputs} in / ${device.outputs} out  ${Math.round(device.sample_rate)} Hz` })));
  }
  raw.append(table);
  return el("div", {}, list, devices.length ? raw : null);
}

function routeCard(route, isDefault) {
  const cal = route.calibration;
  const card = el(
    "div",
    { class: `route-card ${isDefault ? "default" : ""}` },
    el(
      "div",
      { class: "r-title" },
      el("span", { text: route.route_id }),
      el("span", { class: "tag", text: route.kind }),
      isDefault ? el("span", { class: "tag a2", text: "default" }) : null,
      route.backend === "virtual" ? el("span", { class: "tag warn", text: "simulated" }) : null
    ),
    el("div", { class: "hint mono", text: `${route.output_device} out ${route.output_channels.join(",")}  ->  ${route.input_device} in ${route.input_channels.join(",")}${route.reference_channel ? `  (ref ${route.reference_channel})` : ""}  @ ${route.sample_rate} Hz  send ${route.send_gain_db >= 0 ? "+" : ""}${route.send_gain_db.toFixed(1)} dB` }),
    route.note ? el("div", { class: "hint", text: route.note }) : null,
    ...(route.problems || []).map((p) => el("div", { class: "problems", text: p })),
    cal ? el("div", { class: "hint", text: `calibrated ${cal.measured_at.slice(0, 16).replace("T", " ")}: latency ${cal.latency_samples?.toFixed(1)} samples, ${cal.level_method} level, noise ${cal.noise_floor_db} dBFS${cal.drift_ppm ? `, drift ${cal.drift_ppm} ppm` : ""}` }) : el("div", { class: "problems", text: "never calibrated - the send level is a guess" })
  );
  const vu = el("div", { class: "vu", hidden: true }, el("span", { class: "hint mono vu-label", text: "--" }), el("div", { class: "vu-track" }, el("div", { class: "vu-fill" })));
  const out = el("div", {});
  card.append(
    el(
      "div",
      { class: "row" },
      !isDefault ? el("button", { class: "btn small", text: "make default", onclick: () => attempt(async () => { await api.post(`/api/loop/routes/${route.route_id}/default`); refresh(); }) }) : null,
      el("button", { class: "btn small", text: "test tone", onclick: () => attempt(async () => { const r = await api.post("/api/loop/tone", {}, { route_id: route.route_id }); toast(r.message, { kind: r.connected ? "ok" : "error" }); }) }),
      el("button", { class: "btn small", text: "meter", onclick: () => toggleMeter(route.route_id, vu) }),
      el("button", { class: "btn small", text: "calibrate", onclick: () => calibrate(route, out) }),
      el("button", { class: "btn small primary", text: "reamp a take", onclick: () => reamp(route, out) }),
      el("span", { class: "grow" }),
      el("button", { class: "btn small ghost danger", text: "forget", onclick: () => attempt(async () => { await api.del(`/api/loop/routes/${route.route_id}`); refresh(); }) })
    ),
    vu,
    out
  );
  if (cal?.response_db?.length) {
    const chart = el("div", { class: "chart" });
    out.append(chart);
    requestAnimationFrame(() => lineChart(chart, { x: cal.bands_hz, logX: true, height: 130, yLabel: "dB", series: [{ label: "loop + rig response", values: cal.response_db, color: COLOURS.blue, points: true }] }));
  }
  return card;
}

function toggleMeter(routeId, vu) {
  if (metering) {
    clearInterval(metering);
    metering = null;
    document.querySelectorAll(".vu").forEach((v) => (v.hidden = true));
    return;
  }
  vu.hidden = false;
  metering = setInterval(async () => {
    const data = await api.get("/api/loop/meter", { route_id: routeId }).catch(() => null);
    const level = data?.levels?.[0];
    if (!level) return;
    vu.querySelector(".vu-fill").style.width = `${Math.max(0, Math.min(100, ((level.peak_db + 60) / 60) * 100))}%`;
    vu.querySelector(".vu-label").textContent = `${level.peak_db.toFixed(1)} dB${level.clipping ? " CLIP" : ""}`;
  }, 450);
}

async function calibrate(route, out) {
  const { profiles } = await api.get("/api/learn/profiles");
  const answer = await prompt(`Calibrate ${route.route_id}`, [
    { type: "note", text: "Measures latency, noise and the loop's response, and sets the send level so the Core receives your DI as hard as your guitar hits it. For an exact level: record your guitar played straight into the Core (on the Core's USB channel 5 for a hybrid route) and give that recording as 'direct'." },
    { name: "profile_id", label: "Your guitar (its notes are the DI)", type: "select", value: profiles[0]?.profile_id || "", options: [{ value: "", label: "none - measure the loop only" }, ...profiles.map((p) => ({ value: p.profile_id, label: p.label || p.profile_id }))] },
    { name: "direct", label: "Guitar straight into the Core (take id or path, optional)" },
    { name: "direct_is_reference", label: "...recorded on the reference channel", type: "checkbox", value: Boolean(route.reference_channel) },
  ], { okLabel: "Calibrate" });
  if (!answer) return;
  await attempt(async () => {
    const { job_id } = await api.post("/api/loop/calibrate", { route_id: route.route_id, profile_id: answer.profile_id || null, direct: answer.direct || null, direct_is_reference: answer.direct_is_reference });
    const result = await track(job_id, { onUpdate: (job) => (out.textContent = describeJob(job)) });
    clear(out).append(el("pre", { class: "out", text: result.describe }));
    toast("Calibrated", { kind: "ok" });
    setTimeout(refresh, 400);
  });
}

async function reamp(route, out) {
  const { takes } = await api.get("/api/takes", { kind: "di" });
  const answer = await prompt(`Reamp through ${route.route_id}`, [
    { type: "note", text: "Load the rig you want to hear on the Core first. The return comes back on the DI's own timeline, ready to compare sample for sample." },
    { name: "di", label: "DI", type: "select", value: takes[0]?.take_id || "", options: takes.map((t) => ({ value: t.take_id, label: t.describe })) },
    { name: "label", label: "Call it", value: "loop pass" },
  ], { okLabel: "Reamp" });
  if (!answer?.di) return;
  await attempt(async () => {
    const { job_id } = await api.post("/api/loop/reamp", { di: answer.di, route_id: route.route_id, label: answer.label || "loop" });
    const result = await track(job_id, { onUpdate: (job) => (out.textContent = describeJob(job)) });
    clear(out).append(el("pre", { class: "out", text: result.render }), el("button", { class: "btn small", text: "play the return", onclick: () => new Audio(result.url).play() }));
  });
}

function manualRoute(devices) {
  const generic = /sound mapper|primary sound/i;
  const names = (want) => [...new Set(devices.filter((d) => d[want] > 0 && !generic.test(d.name)).map((d) => d.name))];
  const fields = {
    id: el("input", { type: "text", placeholder: "my-loop" }),
    kind: el("select", {}, ...["analog", "usb-reamp", "hybrid", "custom"].map((k) => el("option", { value: k, text: k }))),
    out: el("select", {}, ...names("outputs").map((n) => el("option", { value: n, text: n }))),
    outCh: el("input", { type: "text", value: "1", class: "narrow" }),
    inp: el("select", {}, ...names("inputs").map((n) => el("option", { value: n, text: n }))),
    inCh: el("input", { type: "text", value: "1", class: "narrow" }),
    ref: el("input", { type: "number", class: "narrow", placeholder: "ref ch" }),
  };
  const channels = (text) => text.split(/[ ,]+/).map(Number).filter((n) => n > 0);
  return el(
    "details",
    { class: "card" },
    el("summary", { class: "hint", text: "Set up a route by hand" }),
    el("div", { class: "row" }, fields.id, fields.kind, el("span", { class: "hint", text: "out" }), fields.out, fields.outCh, el("span", { class: "hint", text: "in" }), fields.inp, fields.inCh, fields.ref),
    el("div", { class: "row" }, el("button", {
      class: "btn",
      text: "Save route",
      onclick: () => attempt(async () => {
        await api.post("/api/loop/routes", { route_id: fields.id.value.trim() || "my-loop", kind: fields.kind.value, output_device: fields.out.value, input_device: fields.inp.value, output_channels: channels(fields.outCh.value), input_channels: channels(fields.inCh.value), reference_channel: Number(fields.ref.value) || null, reference_device: Number(fields.ref.value) ? fields.inp.value : null });
        refresh();
      }),
    }))
  );
}

function assessCard() {
  const target = dropZone({ label: "Reference (e.g. the offline render)", accept: "audio/*" });
  const capture = dropZone({ label: "Capture (e.g. the loop's return)", accept: "audio/*" });
  const out = el("div");
  return el(
    "div",
    { class: "card" },
    el("h3", { text: "Compare two renditions of the same DI" }),
    el("div", { class: "hint", text: "Sample-aligned: ESR, null depth, and where they differ. Only for the same performance - against a record use Match." }),
    el("div", { class: "form-grid" }, target, capture),
    el("div", { class: "row" }, el("button", { class: "btn", text: "Compare", onclick: () => attempt(async () => { const r = await api.post("/api/loop/assess", { target: target.path(), capture: capture.path() }); clear(out).append(el("pre", { class: "out", text: r.render })); }) })),
    out
  );
}

export async function refresh() {
  const host = clear($("#loop-body"));
  const [{ devices, hardware, error }, suggestions, routes] = await Promise.all([
    api.get("/api/loop/devices").catch((e) => ({ devices: [], hardware: [], error: e.message })),
    api.get("/api/loop/suggest").catch(() => ({ routes: [] })),
    api.get("/api/loop/routes").catch(() => ({ routes: [], default: null })),
  ]);

  const saved = el("div", { class: "card" }, el("div", { class: "scenes-head" }, el("h3", { text: "Routes" }), el("button", { class: "btn small", text: "Use the simulated bench", title: "An interface and a Core that do not exist - for trying everything", onclick: () => attempt(async () => { await api.post("/api/loop/demo"); refresh(); }) })));
  if (!routes.routes.length) saved.append(el("div", { class: "hint", text: "No routes yet. Adopt a suggestion below, set one up by hand, or use the simulated bench." }));
  for (const route of routes.routes) saved.append(routeCard(route, route.route_id === routes.default));

  const suggest = el("div", { class: "card" }, el("h3", { text: "Suggested for what is plugged in" }));
  if (!suggestions.routes?.length) suggest.append(el("div", { class: "hint", text: suggestions.error || "Connect your interface (and the Core over USB) to see suggestions." }));
  for (const route of suggestions.routes || []) {
    suggest.append(
      el(
        "div",
        { class: "route-card" },
        el("div", { class: "r-title" }, el("span", { text: route.route_id }), el("span", { class: "tag", text: route.kind }), route.hostapi ? el("span", { class: "tag", text: route.hostapi.replace("Windows ", "") }) : null),
        el("div", { class: "hint mono", text: `${route.output_device} out ${route.output_channels.join(",")}  ->  ${route.input_device} in ${route.input_channels.join(",")}  @ ${route.sample_rate} Hz` }),
        el("div", { class: "hint", text: route.note || "" }),
        el("div", { class: "row" }, el("button", { class: "btn small primary", text: "use this", onclick: () => attempt(async () => { await api.post(`/api/loop/routes/adopt/${route.route_id}`); refresh(); }) }))
      )
    );
  }

  host.append(saved, suggest, el("div", { class: "card" }, el("h3", { text: "What is plugged in" }), deviceList(devices, hardware, error)), manualRoute(devices), assessCard());
}

export async function init() {
  await refresh();
}

export function onShow() {
  refresh();
}

export function onHide() {
  if (metering) {
    clearInterval(metering);
    metering = null;
  }
}
