/* Bootstrap: tabs, live socket, status pills. */

import { api } from "./api.js";
import { $, $$, setPill, toast } from "./ui.js";

import * as recipes from "./panels/recipes.js";
import * as rig from "./panels/rig.js";
import * as takes from "./panels/takes.js";
import * as captures from "./panels/captures.js";
import * as match from "./panels/match.js";
import * as device from "./panels/device.js";
import * as workspace from "./panels/workspace.js";

const PANELS = { recipes, rig, takes, captures, match, device, workspace };

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

      if (message.type === "jobs") {
        const running = message.jobs.filter((j) => !j.finished);
        if (running.length) {
          setPill("pill-jobs", `${running.length} running`, "busy");
        } else {
          const pill = document.getElementById("pill-jobs");
          if (pill) pill.hidden = true;
        }
        if (activePanel === "workspace") workspace.refreshJobs(message.jobs);
        // A panel that started a job watches it wherever the user is, so
        // wandering off to another tab does not lose the progress.
        for (const panel of Object.values(PANELS)) panel.onJobs?.(message.jobs);
      }

      if (message.type === "job" && message.kind === "state") {
        if (message.state === "succeeded") {
          toast(`Finished: ${message.message || "job"}`, { kind: "ok" });
        } else if (message.state === "failed") {
          toast(`Job failed: ${message.message}`, { kind: "error", timeout: 10000 });
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

  await Promise.allSettled(
    Object.values(PANELS).map((panel) => panel.init?.())
  );

  await refreshStatus();
  connectLive();

  const wanted = location.hash.replace("#", "");
  show(PANELS[wanted] ? wanted : "rig");
}

boot();
