/* Captures panel: a local .nam library, and TONE3000 search. */

import { api } from "../api.js";
import { $, attempt, clear, el, jobRow } from "../ui.js";
import { attachPicker } from "../picker.js";

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
    el("button", {
      class: "btn small",
      text: "Download",
      title:
        "Fetch the captures and read each file - the API's architecture " +
        "field and the weights can disagree.",
      onclick: () => attempt(() => startDownload(tone)),
    }),
    tone.url
      ? el("a", { class: "tag", href: tone.url, target: "_blank", text: "open" })
      : null
  );
}

/* Downloading is tens of megabytes over somebody else's rate limit, so it is
 * a job and this watches it. */

let downloadJob = null;

async function startDownload(tone) {
  const { job_id } = await api.tone3000Download({
    tone_id: String(tone.id),
    architecture: $("#t3k-arch").value,
  });
  downloadJob = job_id;
  clear($("#t3k-download")).append(
    el("div", { class: "hint", text: `Downloading "${tone.title}"...` })
  );
}

async function showDownload(jobId) {
  const { result } = await api.job(jobId);
  const host = clear($("#t3k-download"));
  if (!result?.downloaded) return;

  for (const item of result.downloaded) {
    host.append(
      el(
        "div",
        { class: "item" },
        el("span", {
          class: `tag ${item.actual === "A2" ? "a2" : "a1"}`,
          text: item.actual,
        }),
        el("span", { class: "title", text: item.name }),
        item.size ? el("span", { class: "meta", text: item.size }) : null,
        el("span", { class: "spacer" }),
        // The file disagreeing with its own metadata is the thing worth
        // seeing here, so it gets a tag rather than a footnote.
        item.disagrees
          ? el("span", {
              class: "tag warn",
              text: `API said ${item.claimed}`,
              title:
                "The architecture field is what somebody typed; the weights " +
                "are what the model is. Trust the file.",
            })
          : null,
        item.loadable === false
          ? el("span", {
              class: "tag bad",
              text: "won't load",
              title: "Still usable for offline rendering, which has no size limit.",
            })
          : null
      )
    );
  }
  for (const line of result.installed || []) {
    host.append(el("div", { class: "hint", text: line }));
  }
}

async function refreshAuth() {
  const status = await attempt(() => api.tone3000Status());
  const host = clear($("#t3k-status"));
  if (!status) return;

  if (status.ready) {
    host.className = "notice";
    host.textContent = status.username
      ? `Authenticated as ${status.username}.`
      : "Authenticated.";
    return;
  }
  host.className = "notice warn";
  host.append(
    el("div", { text: "Not connected to TONE3000." }),
    status.client_id
      ? el("button", {
          class: "btn primary",
          text: "Sign in",
          title: "Opens TONE3000 in a new tab and catches the redirect here.",
          onclick: () => attempt(startLogin),
        })
      : el("div", {
          class: "hint",
          text:
            "Set TONE3000_CLIENT_ID to a publishable client id from " +
            "https://www.tone3000.com/api, then restart rigsmith.",
        }),
    el("div", { id: "t3k-login", class: "hint" }),
    el("div", {
      class: "hint",
      text:
        "You can also browse TONE3000 on the Core's own touchscreen - that " +
        "needs no API access and downloads straight to the device.",
    })
  );
}

/* The flow waits on a person deciding something in another tab, so it runs
 * as a job and this watches it. The URL is shown as well as opened: a browser
 * that opens somewhere the user is not looking should not be a dead end. */

let loginJob = null;

async function startLogin() {
  const { job_id } = await api.tone3000Login();
  loginJob = job_id;
  const host = $("#t3k-login");
  if (host) host.textContent = "Waiting for you to authorise in the other tab...";
}

function onLoginJobs(jobs) {
  if (!loginJob) return;
  const job = jobs.find((candidate) => candidate.id === loginJob);
  if (!job) return;

  const host = $("#t3k-login");
  if (host && job.message) {
    clear(host);
    if (job.message.startsWith("http")) {
      host.append(
        el("span", { text: "If no tab opened: " }),
        el("a", { href: job.message, target: "_blank", text: "authorise here" })
      );
    } else {
      host.textContent = job.message;
    }
  }

  if (job.finished) {
    loginJob = null;
    if (job.state === "failed" && host) {
      host.textContent = job.error || "Sign-in did not complete.";
    } else if (host) {
      clear(host);
    }
    refreshAuth();
  }
}

export function onJobs(jobs) {
  if (!downloadJob) return;
  const job = jobs.find((candidate) => candidate.id === downloadJob);
  if (job) {
    clear($("#t3k-download")).append(
      jobRow(job, { onCancel: () => attempt(() => api.cancelJob(job.id)) })
    );
    if (job.finished) {
      const id = downloadJob;
      downloadJob = null;
      if (job.state === "succeeded") attempt(() => showDownload(id));
    }
  }
  onLoginJobs(jobs);
}

export function init() {
  attachPicker("#scan-dir", {
    kind: "capture",
    folder: true,
    title: "Choose a folder of .nam captures",
    onPick: () => $("#scan-btn").click(),
  });

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
