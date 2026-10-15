/* Device panel: discovery, USB storage, and MIDI. */

import { api } from "../api.js";
import { describeJob, track } from "../jobs.js";
import { $, attempt, clear, el, setPill, toast, when } from "../ui.js";

/* ------------------------------------------------------------- pairing */

/* The Core authorises a session, not a user: it shows a PIN on its own
 * screen and somebody standing at it accepts. So the PIN is shown here, big,
 * the moment the device offers it - the wait only starts after that. */

let lastHost = null;

async function renderPairing() {
  const box = clear($("#pair-box"));
  const { sessions } = await api.get("/api/device/sessions").catch(() => ({ sessions: [] }));
  const list = el("div", { class: "list", style: "margin-top:10px" });
  for (const session of sessions) {
    list.append(
      el(
        "div",
        { class: "item" },
        el("span", { class: "tag a2", text: "paired" }),
        el("span", { class: "title", text: session.host }),
        el("span", { class: "meta", text: [session.server, session.paired_at ? `since ${when(session.paired_at)}` : ""].filter(Boolean).join("  ") }),
        el("span", { class: "spacer" }),
        el("button", { class: "btn small", text: "snapshot", title: "Read every rig's name and the loaded rig in full - writes nothing", onclick: () => snapshot(session.host, out) }),
        el("button", { class: "btn small ghost danger", text: "forget", onclick: () => attempt(async () => { await api.post("/api/device/forget", { host: session.host }); renderPairing(); }) })
      )
    );
  }
  const pin = el("div", { class: "pin", hidden: true });
  const out = el("div", { class: "hint" });
  const host = el("input", { type: "text", placeholder: "host or IP (blank: discover)", value: lastHost || "" });
  box.append(
    ...[
    el("h3", { text: "Pairing", style: "margin-top:16px" }),
    el("p", { class: "hint", text: sessions.length ? "Writing a rig needs a paired session. Pairing again replaces it." : "Not paired yet. Writing a rig needs a paired session: the Core shows a PIN, and you accept it on the device." }),
    sessions.length ? list : null,
    el("div", { class: "row" }, host, el("button", { class: "btn primary", text: "Pair", onclick: () => pair(host.value.trim() || null, pin, out) })),
    pin,
    out,
    ].filter(Boolean)
  );
}

async function pair(host, pin, out) {
  await attempt(async () => {
    const { job_id } = await api.post("/api/device/pair", { host });
    out.textContent = "asking the device for a PIN...";
    const result = await track(job_id, {
      onUpdate: (job) => {
        const match = /PIN (\S+)/.exec(job.message || "");
        if (match) {
          pin.hidden = false;
          pin.textContent = match[1];
          out.textContent = "Accept this PIN on the Core's screen. Waiting...";
        } else {
          out.textContent = describeJob(job);
        }
      },
    });
    pin.hidden = true;
    toast(result.message, { kind: "ok" });
    renderPairing();
  });
}

async function snapshot(host, out) {
  await attempt(async () => {
    const { job_id } = await api.post("/api/device/snapshot", { host });
    const result = await track(job_id, { onUpdate: (job) => (out.textContent = describeJob(job)) });
    clear(out).append(el("pre", { class: "out", text: `${result.render}\n  saved to ${result.path}` }));
  });
}

async function renderCatalog() {
  const status = await api.get("/api/device/catalog");
  const host = clear($("#catalog-status"));
  host.append(
    el("div", { class: "item" },
      el("span", { class: `tag ${status.verified ? "a2" : "warn"}`, text: status.verified ? "verified" : "unverified" }),
      el("span", { class: "title", text: `${status.models} models` }),
      el("span", { class: "meta", text: `from ${status.source}${status.captured_at ? `, ${status.captured_at.slice(0, 10)}` : ""}` })),
    el("div", { class: "item" },
      el("span", { class: `tag ${status.measured ? "a2" : "warn"}`, text: `${status.measured} of ${status.controls}` }),
      el("span", { class: "title", text: "knob curves measured" }),
      el("span", { class: "meta", text: !status.controls ? "this catalog has no ranges to measure - extract the device's own" : status.measured < status.controls ? "the rest are read as if linear" : "every one" }))
  );
}

async function deviceJob(path, body, describe) {
  const out = $("#catalog-out");
  await attempt(async () => {
    const { job_id } = await api.post(path, body);
    const result = await track(job_id, { onUpdate: (job) => (out.textContent = describeJob(job)) });
    out.textContent = describe(result);
  });
}

function deviceItem(device) {
  return el(
    "div",
    { class: "item" },
    el("span", {
      class: `tag ${device.confirmed ? "a2" : "warn"}`,
      text: device.confirmed ? "HeadRush" : "unknown",
    }),
    el("span", { class: "title", text: device.host }),
    el("span", { class: "meta", text: device.address || "" }),
    el("span", { class: "spacer" }),
    el("span", { class: "meta", text: `via ${device.source}` })
  );
}

async function discover() {
  const host = clear($("#discover-results"));
  host.append(el("div", { class: "hint", text: "Looking..." }));

  const result = await attempt(() => api.discover(true));
  clear(host);

  if (!result || !result.devices.length) {
    host.append(
      el("div", {
        class: "notice warn",
        text:
          "Nothing answered. Check the Core is powered on, on this same " +
          "Wi-Fi network and band, with Global Settings > Wi-Fi > HeadRush " +
          "Remote turned on, and no VPN active.",
      })
    );
    setPill("pill-device", "no device", "bad");
    return;
  }

  for (const device of result.devices) host.append(deviceItem(device));
  const confirmed = result.devices.filter((d) => d.confirmed);
  if (confirmed.length) {
    lastHost = confirmed[0].address || confirmed[0].host;
    renderPairing();
  }
  setPill(
    "pill-device",
    confirmed.length ? confirmed[0].host : `${result.devices.length} hosts`,
    confirmed.length ? "ok" : "warn"
  );
}

/* ------------------------------------------------------------------ MIDI */

/* The one fully documented interface on this device, and the only way to make
 * it do something on a channel independent of whatever performed a write.
 * There is no scene-select CC, so a scene is reached by pressing the
 * footswitch it is assigned to - which is why these are five buttons rather
 * than a list of scenes. */

function midiTarget() {
  const channel = Number($("#midi-channel").value) || 1;
  return {
    port: $("#midi-port").value || null,
    // The device counts channels from 1; the protocol counts from 0.
    channel: Math.min(16, Math.max(1, channel)) - 1,
  };
}

async function refreshPorts() {
  const { outputs, inputs, error, hint } = await api.midiPorts();
  const select = $("#midi-port");
  const chosen = select.value;
  clear(select);
  select.append(el("option", { value: "", text: "Auto-detect the HeadRush" }));
  for (const name of outputs) select.append(el("option", { value: name, text: name }));
  if (outputs.includes(chosen)) select.value = chosen;

  const lines = [
    `outputs:  ${outputs.join(", ") || "(none)"}`,
    `inputs:   ${inputs.join(", ") || "(none)"}`,
  ];
  if (error) {
    lines.push("", error, hint || "");
  } else if (!outputs.length) {
    lines.push(
      "",
      "No MIDI outputs. Connect the Core over USB-MIDI or a MIDI interface.",
      "MIDI is the one fully documented interface on this device, and the only",
      "way to verify a write on a channel independent of the one that made it."
    );
  }
  $("#midi-out").textContent = lines.join("\n");
  return outputs;
}

function renderFootswitches() {
  const host = clear($("#midi-footswitches"));
  for (let number = 1; number <= 5; number += 1) {
    host.append(
      el("button", {
        class: "btn small",
        text: `FS${number}`,
        title: `Press footswitch ${number} on the device`,
        onclick: () =>
          attempt(async () => {
            const { message } = await api.midiFootswitch({
              footswitch: number,
              ...midiTarget(),
            });
            $("#midi-out").textContent = message;
          }),
      })
    );
  }
}

async function refreshDrafts() {
  const { drafts } = await api.drafts().catch(() => ({ drafts: [] }));
  const select = clear($("#midi-draft"));
  if (!drafts.length) {
    select.append(el("option", { value: "", text: "No rigs yet" }));
    return;
  }
  for (const draft of drafts) {
    select.append(el("option", { value: draft.draft_id, text: draft.name }));
  }
}

export function init() {
  $("#discover-btn").onclick = discover;

  $("#storage-btn").onclick = () =>
    attempt(async () => {
      const result = await api.storage();
      $("#storage-out").textContent = result.describe;
    });

  $("#backup-btn").onclick = () =>
    attempt(
      async () => {
        const result = await api.backup();
        $("#storage-out").textContent = result.describe;
      },
      { success: "Backed up" }
    );

  $("#midi-ports").onclick = () => attempt(refreshPorts);

  $("#midi-select").onclick = () =>
    attempt(async () => {
      const program = Number($("#midi-program").value);
      if (!Number.isInteger(program) || program < 0 || program > 127) {
        throw new Error("Enter a program number 0-127");
      }
      const { message } = await api.midiSelect({ program, ...midiTarget() });
      $("#midi-out").textContent = message;
    });

  $("#midi-verify").onclick = () =>
    attempt(async () => {
      const draftId = $("#midi-draft").value;
      if (!draftId) throw new Error("Create a rig first, then verify it");
      const program = $("#midi-program").value;
      const result = await api.midiVerify({
        draft_id: draftId,
        program: program === "" ? null : Number(program),
        ...midiTarget(),
      });
      $("#midi-out").textContent = result.rendered;
    });

  $("#midi-tap").onclick = () =>
    attempt(async () => {
      const bpm = Number($("#midi-bpm").value);
      if (!Number.isFinite(bpm) || bpm < 20 || bpm > 400) {
        throw new Error("Enter a tempo between 20 and 400 BPM");
      }
      const { message } = await api.midiTap({ bpm, ...midiTarget() });
      $("#midi-out").textContent = message;
    });

  $("#catalog-calibrate").onclick = () =>
    deviceJob("/api/device/catalog/calibrate", {}, (result) => {
      renderCatalog();
      return result.lines.join("\n") + (result.saved ? `\n\nsaved to ${result.saved}` : "\n\nnothing measured, nothing saved");
    });
  $("#device-scenes").onclick = () => deviceJob("/api/device/scenes", {}, (result) => result.render);

  renderFootswitches();
  renderPairing();
  attempt(renderCatalog);
  return refreshDrafts();
}

export function onShow() {
  attempt(refreshDrafts);
  attempt(renderCatalog);
  renderPairing();
}
