/* Autopilot: a song in, a rig and a listening test out.
 *
 * The landing page, because it is the shortest path from "I want that tone"
 * to something to listen to. Everything it does is also a panel of its own -
 * the stages link out to the song map, the draft and the blind test - so the
 * run is a starting point you can take apart, not a black box.
 */

import { api } from "../api.js";
import { goTo, onArrive } from "../nav.js";
import { $, attempt, clear, el, toast } from "../ui.js";
import { dropZone } from "../upload.js";
import { ask } from "../chat.js";

let stages = ["song", "stem", "map", "parts", "research", "di", "candidates", "tune", "rig"];
let current = null;
let polling = null;
let chosenMatch = null;
const zones = {};

function field(label, input) {
  return el("label", { class: "field" }, el("span", { text: label }), input);
}

async function profileOptions(select) {
  const { profiles } = await api.get("/api/learn/profiles").catch(() => ({ profiles: [] }));
  const settings = await api.get("/api/settings").catch(() => ({}));
  clear(select);
  for (const profile of profiles) {
    select.append(el("option", { value: profile.profile_id, text: profile.label || profile.profile_id }));
  }
  select.append(el("option", { value: "synthetic", text: "no profile yet - the synthetic string" }));
  select.value = settings.default_profile && profiles.some((p) => p.profile_id === settings.default_profile)
    ? settings.default_profile
    : profiles[0]?.profile_id || "synthetic";
}

async function routeOptions(select) {
  const { routes, default: fallback } = await api.get("/api/loop/routes").catch(() => ({ routes: [] }));
  clear(select);
  if (!routes.length) select.append(el("option", { value: "", text: "no loop routes - set one up in Loop" }));
  for (const route of routes) {
    select.append(el("option", { value: route.route_id, text: `${route.route_id} (${route.kind})` }));
  }
  if (fallback) select.value = fallback;
}

function renderForm() {
  const host = clear($("#ap-form"));
  const query = el("input", { type: "text", placeholder: "Search a song - 'going under evanescence'", class: "grow" });
  const results = el("div", { class: "matches" });
  const title = el("input", { type: "text", placeholder: "Title" });
  const artist = el("input", { type: "text", placeholder: "Artist" });
  const profile = el("select");
  const bench = el(
    "select",
    {},
    el("option", { value: "virtual", text: "virtual - rehearse on the simulated amp" }),
    el("option", { value: "device", text: "device - switch amps live on the Core" })
  );
  const route = el("select");
  const research = el("input", { type: "checkbox", checked: true });
  const budget = el("input", { type: "number", value: 24, min: 6, max: 200, class: "narrow" });

  zones.mix = dropZone({ label: "Your copy of the song - drop or click", accept: "audio/*" });
  zones.stem = dropZone({ label: "An isolated guitar stem (optional)", accept: "audio/*" });
  zones.midi = dropZone({ label: "A tab's MIDI export of the part (optional)", accept: ".mid,.midi" });

  const search = async () => {
    const text = query.value.trim();
    if (!text) return;
    clear(results).append(el("div", { class: "hint", text: "searching..." }));
    const found = await attempt(() => api.get("/api/songs/search", { q: text }));
    clear(results);
    if (!found) return;
    if (!found.matches.length) results.append(el("div", { class: "hint", text: "nothing found - type the title and artist instead" }));
    for (const match of found.matches) {
      const card = el(
        "div",
        { class: "match-card", onclick: () => {
          chosenMatch = match;
          title.value = match.title;
          artist.value = match.artist;
          results.querySelectorAll(".match-card").forEach((c) => c.classList.toggle("chosen", c === card));
        } },
        match.artwork_url ? el("img", { src: match.artwork_url, alt: "" }) : el("img", { alt: "" }),
        el("div", {}, el("div", { class: "t", text: match.title }), el("div", { class: "m", text: `${match.artist} - ${match.album || ""} ${match.year || ""}` }))
      );
      results.append(card);
    }
  };
  query.addEventListener("keydown", (event) => event.key === "Enter" && search());

  const start = el("button", { class: "btn primary", text: "Start autopilot" });
  start.onclick = () =>
    attempt(async () => {
      const body = {
        title: title.value.trim(),
        artist: artist.value.trim() || null,
        mix: zones.mix.path(),
        stem: zones.stem.path(),
        midi: zones.midi.path(),
        profile_id: profile.value === "synthetic" ? null : profile.value,
        bench: bench.value,
        route_id: bench.value === "device" ? route.value || null : null,
        research: research.checked,
        tune_budget: Number(budget.value) || 24,
      };
      if (!body.title) throw new Error("Give the song a title - search for it, or type it");
      const run = await api.post("/api/autopilot", body);
      show(run);
      toast("Autopilot started", { kind: "ok" });
      refreshRuns();
    });

  host.append(
    el("div", { class: "row" }, query, el("button", { class: "btn", text: "Look up", onclick: search })),
    results,
    el("div", { class: "form-grid", style: "margin-top:12px" }, field("Title", title), field("Artist", artist), field("Your guitar", profile), field("Bench", bench), field("Loop route (device bench)", route)),
    el("div", { class: "form-grid", style: "margin-top:12px" }, zones.mix, zones.stem, zones.midi),
    el(
      "div",
      { class: "row", style: "margin-top:12px" },
      el("label", { class: "check hint" }, research, "research the gear with an agent"),
      el("span", { class: "hint" }, "tuning budget"),
      budget,
      el("span", { class: "grow" }),
      el("button", { class: "btn ghost", text: "Ask instead", title: "Describe what you want to the assistant", onclick: () => ask(`I want the guitar tone from ${title.value || "a song"}${artist.value ? " by " + artist.value : ""}. What do you need from me?`) }),
      start
    )
  );
  profileOptions(profile);
  routeOptions(route);
}

function stageNode(name, stage) {
  return el(
    "div",
    { class: `stage ${stage?.state || "waiting"}`, title: stage?.summary || "" },
    el("div", { class: "s-name", text: name }),
    el("div", { class: "s-sum", text: stage?.summary || (stage?.state === "running" ? "working..." : "") })
  );
}

function audioLink(label, url) {
  if (!url) return null;
  const player = new Audio(url);
  const button = el("button", { class: "btn small", text: `play ${label}` });
  button.onclick = () => {
    if (player.paused) {
      document.querySelectorAll("audio").forEach((a) => a.pause());
      player.currentTime = 0;
      player.play();
      button.textContent = `stop ${label}`;
    } else {
      player.pause();
      button.textContent = `play ${label}`;
    }
  };
  player.onended = () => (button.textContent = `play ${label}`);
  return button;
}

function show(run) {
  current = run;
  const host = clear($("#ap-current"));
  const card = el("div", { class: "card" });
  card.append(
    el(
      "div",
      { class: "chain-head" },
      el("h2", { class: "rig-name", text: run.request.title + (run.request.artist ? ` - ${run.request.artist}` : "") }),
      el("span", { class: `tag ${run.state === "done" ? "a2" : run.state === "failed" ? "bad" : "warn"}`, text: run.state }),
      el("span", { class: "hint", text: `${run.request.bench} bench` })
    ),
    el("div", { class: "stages" }, ...stages.map((name) => stageNode(name, run.stages[name])))
  );

  const links = el("div", { class: "row" });
  if (run.song_id) links.append(el("button", { class: "btn small", text: "Song map", onclick: () => goTo("songs", { songId: run.song_id }) }));
  if (run.draft_id) links.append(el("button", { class: "btn small", text: "Open the draft", onclick: () => goTo("rig", { draftId: run.draft_id }) }));
  if (run.ab_session) links.append(el("button", { class: "btn small primary", text: "Blind A/B the finalists", onclick: () => goTo("match", { abSession: run.ab_session }) }));
  links.append(audioLink("the part", run.target_url), audioLink("your DI", run.di_url));
  if (run.state === "failed" || run.state === "stopped") {
    links.append(el("button", { class: "btn small", text: "Resume", onclick: () => attempt(async () => show(await api.post(`/api/autopilot/${run.run_id}/resume`))) }));
  }
  card.append(links);

  if (run.results?.length) {
    const worst = Math.max(...run.results.map((r) => r.composite)) || 1;
    const list = el("div", { class: "list ranking" });
    for (const result of run.results.slice(0, 8)) {
      list.append(
        el(
          "div",
          { class: "item" },
          el("span", { class: "mono", text: result.composite.toFixed(3) }),
          el("span", { class: "score-bar" }, el("span", { style: `width:${Math.max(4, (1 - result.composite / (worst * 1.05)) * 100)}%` })),
          el("span", { class: "title", text: result.label }),
          el("span", { class: "meta", text: `${result.tier} - ${result.reason}` }),
          el("span", { class: "spacer" }),
          audioLink("", result.url)
        )
      );
    }
    card.append(el("h3", { text: "Closest first", style: "margin-top:14px" }), list);
  }
  for (const note of run.notes || []) card.append(el("div", { class: "notice", text: note }));
  host.append(card);

  clearInterval(polling);
  if (run.state === "running") {
    polling = setInterval(async () => {
      const fresh = await api.get(`/api/autopilot/${run.run_id}`).catch(() => null);
      if (fresh && current?.run_id === fresh.run_id) {
        show(fresh);
        if (fresh.state !== "running") refreshRuns();
      }
    }, 2000);
  }
}

async function refreshRuns() {
  const data = await api.get("/api/autopilot").catch(() => null);
  if (!data) return;
  stages = data.stages || stages;
  const list = clear($("#ap-runs"));
  if (!data.runs.length) {
    list.append(el("div", { class: "hint", text: "No runs yet. Look a song up above, drop your copy of it in, and start." }));
  }
  for (const run of data.runs) {
    const best = run.results?.[0];
    list.append(
      el(
        "div",
        { class: "item", style: "cursor:pointer", onclick: () => show(run) },
        el("span", { class: `tag ${run.state === "done" ? "a2" : run.state === "failed" ? "bad" : "warn"}`, text: run.state }),
        el("span", { class: "title", text: run.request.title + (run.request.artist ? ` - ${run.request.artist}` : "") }),
        el("span", { class: "spacer" }),
        el("span", { class: "meta", text: best ? `${best.label} ${best.composite.toFixed(3)}` : "" }),
        el("span", { class: "meta", text: run.created_at.replace("T", " ").slice(0, 16) })
      )
    );
  }
  if (!current && data.runs.length) show(data.runs[0]);
}

export function onEvent(message) {
  if (message.type === "autopilot" && current && message.run_id === current.run_id) {
    api.get(`/api/autopilot/${current.run_id}`).then(show).catch(() => {});
  }
}

export async function init() {
  renderForm();
  await refreshRuns();
}

export function onShow() {
  refreshRuns();
}

export function onHide() {
  clearInterval(polling);
}

onArrive("autopilot", ({ title, artist, songId }) => {
  if (title) {
    const inputs = $("#ap-form").querySelectorAll(".form-grid input");
    inputs[0].value = title;
    if (artist) inputs[1].value = artist;
  }
  void songId;
});
