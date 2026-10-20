/* Bootstrap: tabs, live socket, status pills. */

import { api } from "./api.js";
import { $, $$, setPill, toast } from "./ui.js";
import { jobsTick } from "./jobs.js";
import * as chat from "./chat.js";
import * as palette from "./palette.js";

import * as autopilot from "./panels/autopilot.js";
import * as songs from "./panels/songs.js";
import * as guitar from "./panels/guitar.js";
import * as tune from "./panels/tune.js";
import * as loop from "./panels/loop.js";
import * as settings from "./panels/settings.js";
import * as recipes from "./panels/recipes.js";
import * as rig from "./panels/rig.js";
import * as takes from "./panels/takes.js";
import * as captures from "./panels/captures.js";
import * as match from "./panels/match.js";
import * as device from "./panels/device.js";
import * as workspace from "./panels/workspace.js";

/* Job ids to their names, from the jobs ticks - so "finished" can say what. */
const jobNames = new Map();
let runningJobs = new Set();

function showRunning() {
  if (runningJobs.size) {
    setPill("pill-jobs", `${runningJobs.size} running`, "busy");
  } else {
    const pill = document.getElementById("pill-jobs");
    if (pill) pill.hidden = true;
  }
}

const PANELS = { autopilot, songs, recipes, guitar, takes, captures, rig, match, tune, loop, device, workspace, settings };

let activePanel = null;

function show(name) {
  if (!PANELS[name]) return;
  if (activePanel && PANELS[activePanel]?.onHide) PANELS[activePanel].onHide();

  for (const section of $$("#panels .panel")) {
    section.hidden = section.dataset.panel !== name;
  }
  for (const tab of $$("#tabs .tab")) {
    tab.setAttribute("aria-selected", String(tab.dataset.panel === name));
  }

  activePanel = name;
  location.hash = name;
  PANELS[name].onShow?.();
}

/* --------------------------------------------------------------- live feed */

function connectLive() {
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
  let socket;
  let retry = 1000;

  const open = () => {
    socket = new WebSocket(url);

    socket.onopen = () => {
      retry = 1000;
      // A periodic ping keeps intermediaries from closing an idle socket.
      socket.pingTimer = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) socket.send("ping");
      }, 20000);
    };

    socket.onmessage = (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }

      // Chats, dispatched agents and autopilot stream on the same socket.
      if (message.type === "chat" || message.type === "agent" || message.type === "autopilot") {
        chat.onEvent(message);
        for (const panel of Object.values(PANELS)) panel.onEvent?.(message);
        return;
      }

      // A draft changed - perhaps by an agent, or the chat's own buttons.
      if (message.type === "draft") {
        for (const panel of Object.values(PANELS)) panel.onEvent?.(message);
        return;
      }

      if (message.type === "jobs") {
        for (const job of message.jobs) jobNames.set(job.id, job.name);
        jobsTick(message.jobs);
        runningJobs = new Set(message.jobs.filter((j) => !j.finished).map((j) => j.id));
        showRunning();
        if (activePanel === "workspace") workspace.refreshJobs(message.jobs);
        // A panel that started a job watches it wherever the user is, so
        // wandering off to another tab does not lose the progress.
        for (const panel of Object.values(PANELS)) panel.onJobs?.(message.jobs);
      }

      if (message.type === "job" && message.kind === "state") {
        // Said at once, not at the next list of jobs a few seconds later.
        if (["succeeded", "failed", "cancelled"].includes(message.state) && runningJobs.delete(message.job_id)) showRunning();
        const name = jobNames.get(message.job_id) || "a background job";
        // A chat turn says how it went in the chat itself; with the chat
        // open, a toast repeating it only covered the box to type in.
        const inChat = name.startsWith("chat: ") && chat.isOpen();
        if (message.state === "succeeded" && !inChat) {
          toast(`Finished: ${name}`, { kind: "ok" });
        } else if (message.state === "failed" && !inChat) {
          toast(`${name} failed: ${message.message}`, { kind: "error", timeout: 10000 });
        }
      }
    };

    socket.onclose = () => {
      clearInterval(socket.pingTimer);
      // Back off, but stay responsive - the server restarting during
      // development is the common case.
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 15000);
    };
  };

  open();
}

/* ------------------------------------------------------------------ status */

async function refreshStatus() {
  try {
    const caps = await api.capabilities();
    const catalog = caps.catalog;
    setPill(
      "pill-catalog",
      catalog.verified ? `catalog ${catalog.models}` : "catalog unverified",
      catalog.verified ? "ok" : "warn"
    );
    const pill = document.getElementById("pill-catalog");
    if (pill) {
      pill.title = catalog.verified
        ? `${catalog.models} models extracted from ${catalog.source}`
        : "Model ids and parameter ranges are not checked until a catalog is " +
          "extracted. Run: rigsmith catalog extract";
    }
    setPill("pill-device", "not checked", "");
  } catch {
    setPill("pill-catalog", "offline", "bad");
  }
  await refreshMode();
}

async function refreshMode() {
  try {
    const settings = await api.get("/api/settings");
    const pill = document.getElementById("pill-mode");
    if (!pill) return;
    pill.textContent = settings.mode === "local" ? "local mode" : "accuracy mode";
    pill.className = `pill mode-pill ${settings.mode === "local" ? "local" : "ok"}`;
    pill.title = settings.modes[settings.mode] + " - click to change";
  } catch {
    /* the pill is a nicety */
  }
}

/* -------------------------------------------------------------------- boot */

async function boot() {
  for (const tab of $$("#tabs .tab")) {
    tab.onclick = () => show(tab.dataset.panel);
  }

  // A panel handing work to the next one switches the tab as part of the
  // handover, so the thread through the work is not something the user has
  // to reconstruct from the tab bar. The destination's own onArrive listener
  // runs after this, once its panel is visible.
  document.addEventListener("rigsmith:navigate", (event) => {
    show(event.detail.panel);
  });

  // Back and forward move between panels as they would between pages, and a
  // #panel link anywhere opens that panel.
  window.addEventListener("hashchange", () => {
    const wanted = location.hash.replace("#", "");
    if (wanted !== activePanel) show(wanted);
  });

  $("#pill-mode").onclick = () => show("settings");
  $("#pill-jobs").onclick = () => show("workspace");
  palette.install();
  $("#palette-open")?.addEventListener("click", () => palette.open());
  document.addEventListener("rigsmith:settings", refreshMode);

  await Promise.allSettled([
    ...Object.values(PANELS).map((panel) => panel.init?.()),
    chat.init(),
  ]);

  await refreshStatus();
  connectLive();

  const wanted = location.hash.replace("#", "");
  show(PANELS[wanted] ? wanted : "autopilot");
}

boot();
