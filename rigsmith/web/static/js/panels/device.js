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

  $("#midi-ports").onclick = () =>
    attempt(async () => {
      const { outputs, inputs } = await api.midiPorts();
      $("#midi-out").textContent =
        `outputs:\n  ${outputs.join("\n  ") || "(none)"}\n\n` +
        `inputs:\n  ${inputs.join("\n  ") || "(none)"}`;
    });

  $("#midi-select").onclick = () =>
    attempt(async () => {
      const program = Number($("#midi-program").value);
      if (!Number.isFinite(program)) throw new Error("Enter a program number 0-127");
      $("#midi-out").textContent =
        "MIDI rig selection is available from the CLI and MCP:\n" +
        `  rigsmith device midi select ${program}\n\n` +
        "It is not wired into the UI yet because sending MIDI from a browser " +
        "session needs a port chosen first - list ports above.";
    });
}

export const onShow = () => {};
