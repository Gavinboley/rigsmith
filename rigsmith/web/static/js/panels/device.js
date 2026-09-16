/* Device panel: discovery, USB storage, and MIDI. */

import { api } from "../api.js";
import { $, attempt, clear, el, setPill } from "../ui.js";

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

  renderFootswitches();
  return refreshDrafts();
}

export function onShow() {
  attempt(refreshDrafts);
}
