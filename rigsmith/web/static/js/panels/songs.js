/* Songs: the record, and a map of its parts.
 *
 * A track is not one tone. The map lays the song out as sections against
 * guitar layers - the double-tracked verse on the left and right, the lead
 * in the middle - and every cell is a part you can choose, hear on its own,
 * play on your guitar, and match.
 */

import { api } from "../api.js";
import { track } from "../jobs.js";
import { goTo, onArrive } from "../nav.js";
import { $, attempt, clear, el, prompt, toast } from "../ui.js";
import { dropZone } from "../upload.js";
import { ask } from "../chat.js";
import { toggleNotes } from "../charts.js";

let songId = null;
let song = null;
const audio = new Audio();

const SECTION_COLOURS = ["#f0a020", "#5aa3e0", "#48b884", "#c2687f", "#a07ae0", "#e0c050", "#4fb0b0", "#e08050"];
const LAYERS = ["stem", "left", "centre", "right", "wide"];
const LAYER_NAMES = { stem: "all", left: "left", centre: "centre", right: "right", wide: "wide" };

// What the panel's one audio element is playing, by key - a URL, or
// "part:<id>" for a part whose audio is cut on demand. Buttons read their
// "stop" from this rather than holding it, so a re-render (show all,
// choosing a part) cannot leave one saying "play" over playing audio.
let playing = null;

function isPlaying(key) {
  return !audio.paused && playing === key;
}

function syncPlayButtons() {
  document.querySelectorAll('section[data-panel="songs"] button[data-play]').forEach((button) => {
    button.textContent = isPlaying(button.dataset.play) ? "stop" : button.dataset.label;
  });
}
["play", "pause", "ended"].forEach((name) => audio.addEventListener(name, syncPlayButtons));

/** A button that plays ``key`` and says "stop" while it does. */
function playButton(label, key, onclick, attrs = {}) {
  return el("button", { class: "btn small", ...attrs, text: isPlaying(key) ? "stop" : label, "data-label": label, "data-play": key, onclick });
}

function play(url, key = url) {
  if (!url) return;
  if (isPlaying(key)) {
    audio.pause();
    return;
  }
  document.querySelectorAll("audio").forEach((a) => a.pause());
  audio.src = url;
  playing = key;
  audio.play().catch(() => {});
}

function time(seconds) {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
}

async function refreshList(select = songId) {
  const { songs } = await api.get("/api/songs");
  const picker = clear($("#song-select"));
  if (!songs.length) picker.append(el("option", { value: "", text: "No songs yet" }));
  for (const item of songs) {
    picker.append(el("option", { value: item.song_id, text: `${item.title}${item.artist ? " - " + item.artist : ""}` }));
  }
  const wanted = select && songs.some((s) => s.song_id === select) ? select : songs[0]?.song_id || null;
  picker.value = wanted || "";
  return wanted;
}

function newSongForm() {
  const host = clear($("#song-body"));
  const title = el("input", { type: "text", placeholder: "Title" });
  const artist = el("input", { type: "text", placeholder: "Artist" });
  const lookup = el("input", { type: "checkbox", checked: true });
  const mix = dropZone({ label: "Your copy of the song", accept: "audio/*" });
  const stem = dropZone({ label: "A guitar stem, if you have one", accept: "audio/*" });
  host.append(
    el(
      "div",
      { class: "card hero" },
      el("h3", { text: "A new song" }),
      el("div", { class: "form-grid" }, el("label", { class: "field" }, el("span", { text: "Title" }), title), el("label", { class: "field" }, el("span", { text: "Artist" }), artist)),
      el("div", { class: "form-grid", style: "margin-top:10px" }, mix, stem),
      el(
        "div",
        { class: "row" },
        el("label", { class: "check hint" }, lookup, "look up the album, year and credits online"),
        el("span", { class: "grow" }),
        el("button", { class: "btn ghost", text: "Cancel", onclick: () => load(songId) }),
        el("button", {
          class: "btn primary",
          text: "Create and map it",
          onclick: () =>
            attempt(async () => {
              if (!title.value.trim()) throw new Error("A song needs a title");
              const made = await api.post("/api/songs", { title: title.value.trim(), artist: artist.value.trim() || null, mix: mix.path(), stem: stem.path(), lookup: lookup.checked });
              await refreshList(made.song_id);
              await load(made.song_id);
              if (made.mix || made.stem) analyse();
            }),
        })
      )
    )
  );
  title.focus();
}

async function load(id) {
  songId = id;
  const host = clear($("#song-body"));
  if (!id) {
    host.append(el("div", { class: "rig-empty" }, el("p", { text: "No songs yet. A song is the record you are chasing, mapped into the parts you can pick." }), el("button", { class: "btn primary", text: "Add a song", onclick: newSongForm })));
    return;
  }
  song = await attempt(() => api.get(`/api/songs/${id}`));
  if (!song) return;
  render();
}

function render() {
  const host = clear($("#song-body"));
  const art = song.lookup?.artwork_url;
  const credits = song.credits?.guitarists || [];
  host.append(
    el(
      "div",
      { class: "card" },
      el(
        "div",
        { class: "song-head" },
        art ? el("img", { src: art, alt: "" }) : null,
        el(
          "div",
          {},
          el("h2", { text: song.title }),
          el("div", { class: "song-meta", text: [song.artist, song.album, song.year].filter(Boolean).join("  -  ") }),
          credits.length ? el("div", { class: "hint", text: "guitar: " + credits.map((c) => c.name).join(", ") }) : null,
          song.structure ? tempoLine() : null
        )
      ),
      el(
        "div",
        { class: "row" },
        song.urls?.mix ? playButton("play the mix", song.urls.mix, () => play(song.urls.mix)) : null,
        song.urls?.stem ? playButton("play the guitar", song.urls.stem, () => play(song.urls.stem)) : null,
        ...Object.keys(song.urls || {}).filter((k) => k.startsWith("layer-")).map((k) => playButton(k.replace("layer-", ""), song.urls[k], () => play(song.urls[k]), { class: "btn small ghost", title: "play this layer alone" })),
        el("span", { class: "grow" }),
        el("button", { class: "btn small", text: "Add audio", onclick: addAudio }),
        el("button", { class: "btn small", text: "Look up", title: "Album, year, credits", onclick: () => attempt(async () => { song = await api.post(`/api/songs/${songId}/lookup`); render(); }) }),
        !song.stem && song.mix ? el("button", { class: "btn small", text: "Isolate guitar", onclick: isolate }) : null,
        el("button", { class: `btn small ${song.parts?.length ? "" : "primary"}`, text: song.parts?.length ? "Re-map" : "Map it", onclick: analyse }),
        el("button", { class: "btn small", text: "Research", title: "Hand the research brief to the researcher", onclick: research }),
        song.parts?.length ? el("button", {
          class: "btn small primary",
          text: "Autopilot",
          title: "Chase the chosen parts - one run each, then one rig with a scene per part",
          onclick: () => goTo("autopilot", { songId: song.song_id, title: song.title, artist: song.artist, chosen: song.parts.filter((p) => p.chosen).length }),
        }) : null
      ),
      el("div", { class: "hint", id: "song-progress" })
    )
  );
  for (const note of song.notes || []) host.lastChild.append(el("div", { class: "notice", text: note }));
  if (song.stale) {
    host.lastChild.append(
      el("div", { class: "notice warn" },
        "This map was made by an older analyser - re-map it for what the newer one hears: the reverb on each part, deep tremolo and how deep it is, chorus and vibrato, a fairer gain reading on leads and chords, and sections that do not cut a chorus in two. Your chosen parts are kept - where a section moved, by the part that covers it now. ",
        el("button", { class: "btn small", text: "Re-map now", onclick: analyse }))
    );
  }

  if (!song.structure) {
    host.append(el("div", { class: "rig-empty" }, el("p", { text: song.mix || song.stem ? "Not mapped yet." : "Give it the song's audio, then map it." }), song.mix || song.stem ? el("button", { class: "btn primary", text: "Map it", onclick: analyse }) : el("button", { class: "btn primary", text: "Add audio", onclick: addAudio })));
    return;
  }
  host.append(timeline(), partsGrid());
}

/* The tempo decides each part's delay note value and the tempo a draft rig
 * is saved with, and the detector's known mistake is an octave - so it can
 * be halved, doubled, tapped along with the record, or typed. */
function tempoLine() {
  const s = song.structure;
  const bpm = Number(s.tempo || 0);
  const byHand = s.tempo_source === "hand";
  const unsure = !byHand && (s.tempo_confidence ?? 1) < 0.35;
  const setTempo = (body) =>
    attempt(async () => {
      song = await api.post(`/api/songs/${songId}/tempo`, body);
      render();
    }, { success: "Tempo set - delay note values re-timed" });
  const exact = async () => {
    const values = await prompt("Song tempo", [
      { type: "note", text: "Synced delays and tremolos in a draft follow this, and the rig is saved with it." },
      { name: "bpm", label: "BPM", type: "number", value: bpm || "" },
    ]);
    if (values && Number(values.bpm)) setTempo({ bpm: Number(values.bpm) });
  };
  return el(
    "div",
    { class: "tempo-line hint mono" },
    el("button", { class: "btn small ghost", id: "song-tempo", text: `${bpm.toFixed(Number.isInteger(bpm) ? 0 : 1)} BPM`, title: "Set the tempo exactly", onclick: exact }),
    byHand ? el("span", { class: "tag", text: "set", title: "Set by hand - kept when the song is re-mapped" }) : null,
    unsure ? el("span", { class: "tag warn", text: "unsure", title: "The beat was not clear - check it against the record" }) : null,
    el("button", { class: "btn small ghost", text: "\u00bd", title: "Halve it (the detector heard eighth notes as beats)", onclick: () => setTempo({ factor: 0.5 }) }),
    el("button", { class: "btn small ghost", text: "\u00d72", title: "Double it (the detector heard half notes as beats)", onclick: () => setTempo({ factor: 2 }) }),
    tapButton(setTempo),
    el("span", { text: `${s.key}  ${Number(s.duration || 0).toFixed(0)}s` })
  );
}

function tapButton(setTempo) {
  let taps = [];
  let timer = null;
  const button = el("button", { class: "btn small ghost", id: "tempo-tap", text: "tap", title: "Tap along with the record - four taps or more, then stop" });
  button.onclick = () => {
    const now = performance.now();
    if (taps.length && now - taps.at(-1) > 2000) taps = [];
    taps.push(now);
    clearTimeout(timer);
    if (taps.length < 2) {
      button.textContent = "tap...";
      return;
    }
    const bpm = 60000 / ((taps.at(-1) - taps[0]) / (taps.length - 1));
    button.textContent = `tap ${bpm.toFixed(1)}`;
    if (taps.length >= 4) {
      timer = setTimeout(() => {
        taps = [];
        setTempo({ bpm: Math.round(bpm * 10) / 10 });
      }, 1600);
    }
  };
  return button;
}

function timeline() {
  const sections = song.structure.sections;
  const duration = song.structure.duration || sections.at(-1)?.end || 1;
  const letters = [...new Set(sections.map((s) => s.label))];
  const colour = (label) => SECTION_COLOURS[letters.indexOf(label) % SECTION_COLOURS.length];
  const chosen = new Set(song.parts.filter((p) => p.chosen).map((p) => p.part_id));
  const byKey = new Map(song.parts.map((p) => [`${p.section}:${p.layer}`, p]));

  const box = el("div", { class: "card" }, el("div", { class: "scenes-head" }, el("h3", { text: "Map" }), el("span", { class: "hint", text: song.layers?.summary || "" })));
  const wrap = el("div", { class: "timeline" });
  const sectionTrack = el("div", { class: "lane-track" });
  sections.forEach((section, index) => {
    sectionTrack.append(
      el("div", {
        class: "seg",
        style: `left:${(section.start / duration) * 100}%;width:${((section.end - section.start) / duration) * 100}%;background:${colour(section.label)}`,
        title: `${section.name} (${section.label}) ${time(section.start)} - click to rename`,
        text: section.name || section.label,
        onclick: () => rename(index, section.name),
      })
    );
  });
  wrap.append(el("div", { class: "lane" }, el("span", { class: "lane-label", text: "sections" }), sectionTrack));

  for (const layer of LAYERS) {
    if (layer !== "stem" && !song.layer_files?.[layer]) continue;
    const track = el("div", { class: "lane-track" });
    sections.forEach((section, index) => {
      const part = byKey.get(`${index}:${layer}`);
      if (!part) return;
      const alpha = 0.18 + 0.6 * Math.min(1, part.presence);
      track.append(
        el("div", {
          class: `seg layer ${chosen.has(part.part_id) ? "chosen" : ""}`,
          style: `left:${(section.start / duration) * 100}%;width:${((section.end - section.start) / duration) * 100}%;background:rgba(90,163,224,${alpha})`,
          title: `${part.label}: ${part.sketch?.summary || ""} - click to choose`,
          // The short form - "saturated", not "saturated (fuzz or very high
          // gain)" - with a mark for movement; the tooltip has the rest.
          text: `${(part.sketch?.gain_label || "").split(" (")[0]}${part.sketch?.modulation ? " ~" : ""}`,
          onclick: () => toggle(part),
        })
      );
    });
    wrap.append(el("div", { class: "lane" }, el("span", { class: "lane-label", text: LAYER_NAMES[layer] }), track));
  }
  box.append(wrap, el("div", { class: "hint", text: "Click a cell to choose that part. Click a section to correct its name - the names are a guess." }));
  return box;
}

let showAll = false;

/** The parts worth a card by default: chosen ones, and per section the
 * layer that is most present - every layer of every section is dozens of
 * near-identical cards, and the map above already offers the rest. */
function featured() {
  const best = new Map();
  for (const part of song.parts) {
    if (part.layer === "stem") continue;
    const current = best.get(part.section);
    if (!current || part.presence > current.presence) best.set(part.section, part);
  }
  const keep = new Set([...best.values()].map((p) => p.part_id));
  for (const part of song.parts) if (part.chosen) keep.add(part.part_id);
  if (!keep.size) song.parts.forEach((p) => keep.add(p.part_id));
  return song.parts.filter((p) => keep.has(p.part_id));
}

function partsGrid() {
  const shown = showAll ? song.parts : featured();
  const box = el(
    "div",
    { class: "card" },
    el(
      "div",
      { class: "scenes-head" },
      el("h3", { text: "Parts" }),
      el("span", { class: "hint", text: showAll ? `all ${song.parts.length}` : `chosen, and the main layer of each section - ${shown.length} of ${song.parts.length}` }),
      el("button", { class: "btn small", text: showAll ? "show fewer" : "show all", onclick: () => { showAll = !showAll; render(); } })
    )
  );
  const grid = el("div", { class: "parts" });
  const parts = [...shown].sort((a, b) => Number(b.chosen) - Number(a.chosen) || a.start - b.start);
  for (const part of parts) {
    const sketch = part.sketch || {};
    const card = el(
      "div",
      { class: `part ${part.chosen ? "chosen" : ""}` },
      el("div", { class: "p-title" }, el("span", { text: part.label }), el("span", { class: "p-time", text: `${time(part.start)}  ${(part.end - part.start).toFixed(0)}s` })),
      el("div", { class: "gainbar", title: `gain ${(sketch.gain ?? 0).toFixed(2)}` }, el("i", { style: `left:${(sketch.gain ?? 0) * 100}%` })),
      el("div", { class: "p-sketch", text: sketch.summary || "" }),
      part.layer !== "stem" ? el("div", { class: "hint", text: `present ${Math.round(part.presence * 100)}% of its loudest` }) : null,
      part.best ? el("div", { class: "p-best", text: `best so far: ${part.best.label} (${part.best.composite.toFixed(3)}, ${part.best.mode})` }) : null,
      part.di
        ? el(
            "div",
            { class: "p-di hint" },
            el("span", { class: `tag ${part.di.kind === "yours" ? "a2" : ""}`, text: part.di.kind === "yours" ? "your take" : "rendered DI" }),
            el("span", { text: part.di.kind === "yours" ? " autopilot uses it" : ` on ${part.di.guitar || "the synthetic string"}${readingText(part)}` }),
            playButton("play DI", part.di.url, () => play(part.di.url), { class: "btn small ghost" }),
            part.di.kind === "yours" ? null : el("button", { class: "btn small ghost", text: "notes", title: "See the notes this DI plays", "aria-expanded": "false", onclick: (e) => showNotes(part, e.currentTarget) })
          )
        : null,
      sketch.suggestions?.length ? el("details", {}, el("summary", { class: "hint", text: "what the measurements suggest" }), el("ul", { class: "hint" }, ...sketch.suggestions.map((s) => el("li", { text: s })))) : null,
      el(
        "div",
        { class: "p-actions" },
        el("button", { class: `btn small ${part.chosen ? "primary" : ""}`, text: part.chosen ? "chosen" : "choose", onclick: () => toggle(part) }),
        playButton("play", `part:${part.part_id}`, () => playPart(part)),
        el("button", { class: "btn small", text: "on my guitar", title: "Render this part as a DI on a learned guitar", onclick: () => renderDi(part) }),
        el("button", { class: "btn small", text: "my take", title: "Your own recording of this part, put on the record's clock", onclick: () => useTake(part) }),
        el("button", { class: "btn small", text: "tune", title: "Tune a rig against this part", onclick: () => goTo("tune", { songId: songId, partId: part.part_id, diTake: part.di_take }) }),
        el("button", { class: "btn small ghost", text: "ask", onclick: () => ask(`For ${song.title}, part ${part.part_id} (${part.label}, ${sketch.summary}): what gear made this, and how should I start building it?`) })
      )
    );
    grid.append(card);
  }
  box.append(grid);
  return box;
}

async function playPart(part) {
  const key = `part:${part.part_id}`;
  if (isPlaying(key)) {
    audio.pause();
    return;
  }
  const result = await attempt(() => api.get(`/api/songs/${songId}/parts/${part.part_id}/audio`));
  if (result) play(result.url, key);
}

async function toggle(part) {
  const updated = await attempt(() => api.post(`/api/songs/${songId}/choose`, { part_ids: [part.part_id], chosen: !part.chosen }));
  if (updated) {
    song = updated;
    render();
  }
}

async function rename(index, current) {
  const answer = await prompt("Rename this section", [{ name: "name", label: "Name", value: current, placeholder: "verse, chorus, solo, bridge..." }]);
  if (!answer?.name) return;
  const updated = await attempt(() => api.post(`/api/songs/${songId}/sections/${index}/rename`, { name: answer.name }));
  if (updated) {
    song = updated;
    render();
  }
}

async function addAudio() {
  const host = $("#song-body");
  const mix = dropZone({ label: "The mix", accept: "audio/*" });
  const stem = dropZone({ label: "A guitar stem", accept: "audio/*" });
  const card = el(
    "div",
    { class: "card hero" },
    el("h3", { text: "Add audio" }),
    el("div", { class: "form-grid" }, mix, stem),
    el(
      "div",
      { class: "row" },
      el("span", { class: "grow" }),
      el("button", { class: "btn ghost", text: "Cancel", onclick: () => card.remove() }),
      el("button", {
        class: "btn primary",
        text: "Add",
        onclick: () =>
          attempt(async () => {
            song = await api.post(`/api/songs/${songId}/audio`, { mix: mix.path(), stem: stem.path() });
            render();
          }),
      })
    )
  );
  host.prepend(card);
}

function progressInto(job) {
  const node = $("#song-progress");
  if (node) node.textContent = job ? `${job.message || job.state}${job.progress != null ? ` (${Math.round(job.progress * 100)}%)` : ""}` : "";
}

async function analyse() {
  await attempt(async () => {
    const { job_id } = await api.post(`/api/songs/${songId}/analyse`);
    song = await track(job_id, { onUpdate: progressInto });
    render();
    toast(`${song.parts.length} parts mapped`, { kind: "ok" });
  });
}

async function isolate() {
  await attempt(async () => {
    const { job_id } = await api.post(`/api/songs/${songId}/isolate`);
    song = await track(job_id, { onUpdate: progressInto });
    render();
  });
}

async function useTake(part) {
  const { takes } = await api.get("/api/takes", { kind: "di" }).catch(() => ({ takes: [] }));
  const answer = await prompt(`Your take of "${part.label}"`, [
    { type: "note", text: "A recording of you playing this part - along with the record, or to a click at its tempo. If it follows the record closely it is stretched onto the part's clock and becomes the part's DI, so tuning can compare it frame by frame." },
    { name: "take", label: "Take", type: "select", value: takes[0]?.take_id || "", options: takes.map((t) => ({ value: t.take_id, label: t.describe })) },
  ], { okLabel: "Align it" });
  if (!answer?.take) return;
  await attempt(async () => {
    const result = await api.post(`/api/songs/${songId}/parts/${part.part_id}/take`, { take: answer.take });
    toast(result.describe, { kind: result.aligned ? "ok" : "warn", timeout: 9000 });
    if (result.aligned) {
      play(result.url);
      await load(songId);
    }
  });
}

//: How a part's stem can be read into notes, as the user would say it.
const READINGS = [
  { value: "auto", label: "let it decide" },
  { value: "mono", label: "single notes - a riff or a lead" },
  { value: "poly", label: "chords" },
  { value: "arpeggio", label: "an arpeggio - one note per pick, left to ring" },
];
const READ_AS = { mono: "single notes", poly: "chords", arpeggio: "an arpeggio" };
//: How a rendered DI's notes were found, when nobody chose a reading.
const HEARD_AS = { monophonic: "single notes", polyphonic: "chords", arpeggio: "an arpeggio", midi: "the tab", text: "typed notes", "basic-pitch": "basic-pitch" };

function readingText(part) {
  if (READ_AS[part.reading]) return `, read as ${READ_AS[part.reading]}`;
  const heard = HEARD_AS[part.heard_as];
  if (!heard) return "";
  return part.heard_as === "midi" || part.heard_as === "text" ? `, notes from ${heard}` : `, heard as ${heard}`;
}

/** The notes a rendered DI plays, drawn under it - a wrong transcription
 * shows before a tone is tuned against it. Clicking again folds it away. */
function showNotes(part, button) {
  return toggleNotes(button, button.closest(".p-di"), `/api/songs/${songId}/parts/${encodeURIComponent(part.part_id)}/notes`, { label: `The notes of ${part.label}` });
}

async function renderDi(part) {
  const [{ profiles }, pickups] = await Promise.all([api.get("/api/learn/profiles"), api.get("/api/learn/pickups").catch(() => ({ types: [] }))]);
  const answer = await prompt(`Play "${part.label}" on your guitar`, [
    { type: "note", text: "Renders the part's notes on a learned guitar, on the record's clock - then candidates can be scored frame by frame. Give a tab's MIDI path for the right notes; otherwise the stem is transcribed." },
    { name: "profile_id", label: "Guitar", type: "select", value: profiles[0]?.profile_id || "synthetic", options: [...profiles.map((p) => ({ value: p.profile_id, label: p.label || p.profile_id })), { value: "synthetic", label: "the synthetic string" }] },
    { name: "midi", label: "Tab MIDI (path, optional)", placeholder: "C:/tabs/riff.mid" },
    { name: "reading", label: "Without a tab, read the stem as", type: "select", value: part.reading || "auto", options: READINGS },
    { name: "like", label: "The record's guitar, if it was not yours", type: "select", value: "", options: [{ value: "", label: "same as mine" }, ...pickups.types.map((t) => ({ value: t.name, label: `${t.name} - ${t.description}` }))] },
    { name: "amount", label: "How far toward it (%)", type: "number", value: 100 },
    { name: "power_chords", label: "Put power-chord fifths back", type: "checkbox", value: false },
  ]);
  if (!answer) return;
  await attempt(async () => {
    const amount = Math.max(0, Math.min(100, Number(answer.amount ?? 100))) / 100;
    const { job_id } = await api.post(`/api/songs/${songId}/parts/${part.part_id}/di`, { profile_id: answer.profile_id, midi: answer.midi || null, power_chords: answer.power_chords, like: answer.like || null, amount, reading: answer.reading || null });
    const result = await track(job_id, { onUpdate: progressInto });
    progressInto(null);
    toast(`Rendered: ${result.events} notes (${result.method ? `${result.source} - ${result.method}` : result.source})`, { kind: "ok", hint: result.warnings?.[0] });
    play(result.url);
    await load(songId);
  });
}

async function research() {
  const result = await attempt(() => api.get(`/api/songs/${songId}/brief`));
  if (!result) return;
  ask(result.brief, { send: false });
  toast("The research brief is in the chat - send it, or edit it first", { kind: "ok" });
}

export async function init() {
  $("#song-new").onclick = newSongForm;
  $("#song-delete").onclick = async () => {
    if (!songId) return;
    const answer = await prompt(`Delete "${song?.title}"?`, [{ type: "note", text: "Its folder goes too: copied audio, stems, layers, cut parts." }], { okLabel: "Delete" });
    if (!answer) return;
    await attempt(() => api.del(`/api/songs/${songId}`));
    await load(await refreshList(null));
  };
  $("#song-select").onchange = (event) => load(event.target.value || null);
  await load(await refreshList());
}

export function onShow() {
  if (songId) load(songId);
}

export function onHide() {
  audio.pause();
}

onArrive("songs", async ({ songId: wanted }) => {
  if (wanted) {
    await refreshList(wanted);
    await load(wanted);
  }
});
