/* Autopilot: a song in, a rig and a listening test out.
 *
 * The landing page, because it is the shortest path from "I want that tone"
 * to something to listen to. Everything it does is also a panel of its own -
 * the stages link out to the song map, the draft and the blind test - so the
 * run is a starting point you can take apart, not a black box.
 */

import { api } from "../api.js";
import { goTo, onArrive } from "../nav.js";
import { likeControls } from "../pickups.js";
import { remainingCard } from "../remaining.js";
import { $, attempt, clear, el, toast, when } from "../ui.js";
import { dropZone } from "../upload.js";
import { ask } from "../chat.js";

let stages = ["song", "stem", "map", "parts", "research", "di", "candidates", "cabs", "tune", "rig"];
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
  const budget = el("input", { type: "number", value: 24, min: 6, max: 200, class: "narrow", "aria-label": "Tuning budget" });
  // The record's guitar: by default whatever the research names, so a Strat
  // record is chased with a single-coil DI even when you play a humbucker.
  const like = likeControls({ none: "keep my pickup", extra: [["auto", "what the research says"]] });
  like.set("auto");

  zones.mix = dropZone({ label: "Your copy of the song - drop or click", accept: "audio/*" });
  zones.stem = dropZone({ label: "An isolated guitar stem (optional)", accept: "audio/*" });
  zones.midi = dropZone({ label: "A tab's MIDI export of the part (optional)", accept: ".mid,.midi" });
  zones.take = dropZone({ label: "Your own take of the part (optional) - lined up with the record", accept: "audio/*" });

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
          findInLibrary(match.title, match.artist);
        } },
        match.artwork_url ? el("img", { src: match.artwork_url, alt: "" }) : el("img", { alt: "" }),
        el("div", {}, el("div", { class: "t", text: match.title }), el("div", { class: "m", text: `${match.artist} - ${match.album || ""} ${match.year || ""}` }))
      );
      results.append(card);
    }
  };
  query.addEventListener("keydown", (event) => event.key === "Enter" && search());

  // Your own copy, if the music library has it - so looking a song up can
  // be the whole of starting a run.
  const libraryHint = el("div", { class: "hint" });
  const findInLibrary = async (songTitle, songArtist) => {
    libraryHint.textContent = "";
    if (zones.mix.path()) return;
    const found = await api.get("/api/songs/local", { title: songTitle, artist: songArtist || "" }).catch(() => null);
    const best = found?.matches?.find((m) => m.artist_match) || found?.matches?.[0];
    if (!best) {
      libraryHint.textContent = found?.roots?.length ? "Not in your music library - drop your copy in." : "";
      return;
    }
    zones.mix.setPath(best.path);
    libraryHint.textContent = `Found in your music library: ${best.name}`;
  };
  // What your ears keep doing after a run, on the chosen bench - offered,
  // never assumed.
  const useHabits = el("input", { type: "checkbox" });
  const habitsText = el("span", {});
  const habitsBox = el("label", { class: "check hint", hidden: true }, useHabits, habitsText);
  const showHabits = async () => {
    const found = await api.get("/api/autopilot/habits", { bench: bench.value }).catch(() => null);
    habitsBox.hidden = !found?.habits?.length;
    if (found?.habits?.length) {
      habitsText.textContent = `start from my ear habits: ${found.habits.map((h) => h.describe).join("; ")}`;
      habitsBox.title = `Learned from ${found.rigs} finished ${found.bench}-bench rigs: what autopilot built against what the draft holds now`;
    }
  };
  bench.addEventListener("change", showHabits);
  showHabits();
  const start = el("button", { class: "btn primary", text: "Start autopilot" });
  start.onclick = () =>
    attempt(async () => {
      const body = {
        title: title.value.trim(),
        artist: artist.value.trim() || null,
        mix: zones.mix.path(),
        stem: zones.stem.path(),
        midi: zones.midi.path(),
        di: zones.take.path(),
        profile_id: profile.value === "synthetic" ? null : profile.value,
        bench: bench.value,
        route_id: bench.value === "device" ? route.value || null : null,
        research: research.checked,
        tune_budget: Number(budget.value) || 24,
        like: like.like && like.like !== "auto" ? like.like : null,
        like_amount: like.amount,
        match_guitar: like.like === "auto",
        ear_habits: !habitsBox.hidden && useHabits.checked,
      };
      if (fromSong && fromSong.title === body.title) body.song_id = fromSong.songId;
      if (!body.title) throw new Error("Give the song a title - search for it, or type it");
      const run = await api.post("/api/autopilot", body);
      show(run);
      toast("Autopilot started", { kind: "ok" });
      refreshRuns();
    });

  host.append(
    el("div", { class: "hint", id: "ap-song-hint" }),
    el("div", { class: "row" }, query, el("button", { class: "btn", text: "Look up", onclick: search })),
    results,
    el("div", { class: "form-grid", style: "margin-top:12px" }, field("Title", title), field("Artist", artist), field("Your guitar", profile), field("Bench", bench), field("Loop route (device bench)", route)),
    el("div", { class: "form-grid", style: "margin-top:12px" }, zones.mix, zones.stem, zones.midi, zones.take),
    libraryHint,
    el(
      "div",
      { class: "row", style: "margin-top:12px" },
      el("label", { class: "check hint" }, research, "research the gear with an agent"),
      like.node,
      el("span", { class: "hint" }, "tuning budget"),
      budget,
      habitsBox,
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
  // A run that just finished - by the poll or by a pushed event, whichever
  // got here first - changes the past-runs list too.
  const finished = current?.run_id === run.run_id && current.state === "running" && run.state !== "running";
  current = run;
  if (finished) refreshRuns();
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
  if (run.draft_id) {
    links.append(el("button", { class: "btn small", text: "Open the draft", onclick: () => goTo("rig", { draftId: run.draft_id }) }));
    if (run.state === "done") {
      links.append(el("button", { class: "btn small ghost", text: "Finish by ear", title: "Open the draft with the words ready: too boomy, a bit brighter, too much reverb", onclick: () => goTo("rig", { draftId: run.draft_id, focus: "nudge" }) }));
    }
    links.append(el("button", { class: "btn small ghost", text: "Tone sheet", title: "The draft as you would dial it in by hand, and why - printable", onclick: () => window.open(`/api/drafts/${encodeURIComponent(run.draft_id)}/sheet.html`, "_blank") }));
  }
  if (run.ab_session) {
    links.append(el("button", { class: "btn small primary", text: "Blind A/B the finalists", onclick: () => goTo("match", { abSession: run.ab_session }) }));
    // Once the blind test is done, the draft can follow the ears.
    const listening = el("span", { class: "hint" });
    links.append(listening);
    api.get(`/api/ab/${run.ab_session}`).then((ab) => {
      if (!ab.complete) {
        listening.textContent = ab.answered ? `listened ${ab.answered} of ${ab.total}` : "";
        return;
      }
      listening.replaceWith(
        el("button", {
          class: "btn small",
          text: run.ear_pick ? `ears picked ${run.ear_pick}` : "use what my ears picked",
          title: "Rebuild the draft from the blind test's winner, and record the verdicts in the recipe",
          onclick: () => attempt(async () => show(await api.post(`/api/autopilot/${run.run_id}/listen`))),
        })
      );
    }).catch(() => {});
  }
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
  if (run.remaining?.length) card.append(remainingCard(run.remaining));
  if (run.song_rig) {
    card.append(
      el("h3", { text: "One rig for the song", style: "margin-top:14px" }),
      el("div", { class: "list" }, ...run.song_rig.scenes.map((scene) =>
        el("div", { class: "item" }, el("span", { class: "tag a2", text: `FS${scene.footswitch}` }), el("span", { class: "title", text: scene.scene }), el("span", { class: "meta", text: scene.sound }), el("span", { class: "spacer" }), el("span", { class: "meta", text: `run ${scene.run}` }))
      )),
      ...(run.song_rig.notes || []).map((note) => el("div", { class: "notice", text: note })),
      el("div", { class: "row" }, el("button", { class: "btn small primary", text: "Open the song rig", onclick: () => goTo("rig", { draftId: run.song_rig.draft_id }) }))
    );
  }
  // The chart above already says what still differs; its sentence is for
  // the text views (the CLI, an agent's report).
  const notes = (run.notes || []).filter((note) => !(run.remaining?.length && note.startsWith("still different from the record")));
  for (const note of notes) card.append(el("div", { class: "notice", text: note }));
  host.append(card);

  clearInterval(polling);
  if (run.state === "running") {
    polling = setInterval(async () => {
      const fresh = await api.get(`/api/autopilot/${run.run_id}`).catch(() => null);
      if (fresh && current?.run_id === fresh.run_id) {
        show(fresh);
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
        el("span", { class: "meta", text: when(run.created_at) })
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

/* What is set up. Autopilot runs with none of it - a synthetic string, the
 * simulated bench - and every step here swaps a stand-in for the real thing,
 * so the list says what each one buys rather than just ticking boxes. */
async function renderSetup() {
  // Fetch first and swap after, so the card does not blink out while the
  // checks run.
  const setup = await api.get("/api/settings/setup").catch(() => null);
  const host = clear($("#ap-setup"));
  if (!setup || setup.dismissed) return;
  const essential = setup.steps.filter((s) => !s.optional);
  if (setup.steps.every((s) => s.done)) return;
  const doneCount = setup.steps.filter((s) => s.done).length;
  const list = el("div", { class: "setup-steps" });
  for (const step of setup.steps) {
    list.append(
      el(
        "div",
        { class: `setup-step ${step.done ? "done" : ""}` },
        el("span", { class: "tick", text: step.done ? "\u2713" : "" }),
        el("div", { class: "grow" }, el("div", { class: "t", text: step.title + (step.optional && !step.done ? " (optional)" : "") }), el("div", { class: "hint", text: step.detail })),
        step.done ? null : el("button", { class: "btn small", text: "set up", onclick: () => goTo(step.panel) })
      )
    );
  }
  host.append(
    el(
      "div",
      { class: "card" },
      el(
        "div",
        { class: "scenes-head" },
        el("h3", { text: "Getting set up" }),
        el("span", { class: "hint", text: `${doneCount} of ${setup.steps.length} - autopilot already works; each step replaces a stand-in with the real thing` }),
        el("span", { class: "grow" }),
        el("a", { class: "hint", href: "/docs/first-session", target: "_blank", text: "the first session with the Core, step by step" }),
        essential.every((s) => s.done) ? null : el("button", { class: "btn small ghost", text: "ask what's next", onclick: () => ask("What should I set up next, and why?") }),
        el("button", { class: "btn small ghost", text: "hide", onclick: () => attempt(async () => { await api.patch("/api/settings", { onboarding_done: true }); clear(host); }) })
      ),
      list
    )
  );
}

export async function init() {
  renderForm();
  renderSetup();
  await refreshRuns();
}

export function onShow() {
  refreshRuns();
  renderSetup();
}

export function onHide() {
  clearInterval(polling);
}

/* A song handed over from the Songs panel: its map and chosen parts are
 * used as they are, rather than a new song being made from the title. */
let fromSong = null;

onArrive("autopilot", ({ title, artist, songId, chosen }) => {
  const inputs = $("#ap-form").querySelectorAll(".form-grid input");
  if (title) {
    inputs[0].value = title;
    if (artist) inputs[1].value = artist;
  }
  fromSong = songId ? { songId, title, chosen: chosen || 0 } : null;
  const hint = $("#ap-song-hint");
  if (hint) {
    hint.textContent = fromSong
      ? `Using the mapped song "${title}"${fromSong.chosen > 1 ? ` - ${fromSong.chosen} chosen parts, one run each, then a rig with a scene per part` : fromSong.chosen === 1 ? " - its chosen part" : ""}.`
      : "";
  }
});
