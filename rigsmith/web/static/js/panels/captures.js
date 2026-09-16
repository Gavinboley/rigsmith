/* Captures panel: a local .nam library, and TONE3000 search. */

import { api } from "../api.js";
import { $, attempt, clear, el } from "../ui.js";

function compatibilityTag(capture) {
  if (capture.headrush_compatible === true) {
    return el("span", { class: "tag a2", text: "loads" });
  }
  if (capture.headrush_compatible === false) {
    return el("span", {
      class: "tag bad",
      text: "won't load",
      title: "The Core accepts A2 Full/Lite and A1 Standard/Lite/Nano/Feather.",
    });
  }
  return el("span", {
    class: "tag warn",
    text: "unverified",
    title: "Architecture or size could not be determined from the file.",
  });
}

function captureItem(capture) {
  const gear = [capture.gear_make, capture.gear_model].filter(Boolean).join(" ");
  return el(
    "div",
    { class: "item" },
    el("span", {
      class: `tag ${capture.generation === "A2" ? "a2" : "a1"}`,
      text: capture.generation,
    }),
    el("span", { class: "title", text: capture.name || "(unnamed)" }),
    gear ? el("span", { class: "meta", text: gear }) : null,
    capture.tone_type ? el("span", { class: "tag", text: capture.tone_type }) : null,
    el("span", { class: "spacer" }),
    capture.calibrated
      ? el("span", {
          class: "tag",
          text: "calibrated",
          title: "Carries input/output dBu, so it can be gain-staged properly.",
        })
      : null,
    capture.size ? el("span", { class: "meta", text: capture.size }) : null,
    compatibilityTag(capture)
  );
}

function toneItem(tone) {
  return el(
    "div",
    { class: "item" },
    tone.a2 ? el("span", { class: "tag a2", text: `A2 x${tone.a2}` }) : null,
    tone.a1 ? el("span", { class: "tag a1", text: `A1 x${tone.a1}` }) : null,
    el("span", { class: "title", text: tone.title }),
    tone.makes?.length ? el("span", { class: "meta", text: tone.makes.join(", ") }) : null,
    el("span", { class: "spacer" }),
    el("span", { class: "meta", text: `${tone.downloads || 0} downloads` }),
    tone.url
      ? el("a", { class: "tag", href: tone.url, target: "_blank", text: "open" })
      : null
  );
}

async function refreshAuth() {
  const status = await attempt(() => api.tone3000Status());
  const host = clear($("#t3k-status"));
  if (!status) return;

  if (status.ready) {
    host.className = "notice";
    host.textContent = "Authenticated.";
    return;
  }
  host.className = "notice warn";
  host.append(
    el("div", { text: "Not connected to TONE3000." }),
    el("div", {
      class: "hint",
      text: !status.client_id
        ? "Set TONE3000_CLIENT_ID, then run: rigsmith tone3000 login"
        : "Run: rigsmith tone3000 login",
    }),
    el("div", {
      class: "hint",
      text:
        "You can also browse TONE3000 on the Core's own touchscreen - that " +
        "needs no API access and downloads straight to the device.",
    })
  );
}

export function init() {
  $("#scan-btn").onclick = () =>
    attempt(async () => {
      const directory = $("#scan-dir").value.trim();
      if (!directory) throw new Error("Give a folder to scan");
      const { captures } = await api.scanCaptures(directory);
      const host = clear($("#scan-results"));
      if (!captures.length) {
        host.append(el("div", { class: "hint", text: "No .nam files found." }));
        return;
      }
      const a2 = captures.filter((c) => c.generation === "A2").length;
      host.append(
        el("div", {
          class: "hint",
          text: `${captures.length} capture(s) - ${a2} A2`,
        })
      );
      for (const capture of captures) host.append(captureItem(capture));
    });

  $("#t3k-search").onclick = () =>
    attempt(async () => {
      const { tones } = await api.tone3000Search({
        q: $("#t3k-query").value.trim(),
        architecture: $("#t3k-arch").value,
      });
      const host = clear($("#t3k-results"));
      if (!tones.length) {
        host.append(el("div", { class: "hint", text: "Nothing matched." }));
        return;
      }
      for (const tone of tones) host.append(toneItem(tone));
    });

  return refreshAuth();
}

export const onShow = refreshAuth;
