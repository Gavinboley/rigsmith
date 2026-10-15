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

let songId = null;
let song = null;
const audio = new Audio();

const SECTION_COLOURS = ["#f0a020", "#5aa3e0", "#48b884", "#c2687f", "#a07ae0", "#e0c050", "#4fb0b0", "#e08050"];
const LAYERS = ["stem", "left", "centre", "right", "wide"];
const LAYER_NAMES = { stem: "all", left: "left", centre: "centre", right: "right", wide: "wide" };

function play(url, button) {
  if (!url) return;
  if (!audio.paused && audio.dataset.url === url) {
    audio.pause();
    if (button) button.textContent = button.dataset.label;
    return;
  }
  document.querySelectorAll("audio").forEach((a) => a.pause());
  audio.src = url;
  audio.dataset.url = url;
  audio.play();
  if (button) {
    button.dataset.label = button.dataset.label || button.textContent;
    button.textContent = "stop";
    audio.onended = audio.onpause = () => (button.textContent = button.dataset.label);
  }
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
          song.structure ? el("div", { class: "hint mono", text: `${song.structure.tempo.toFixed(0)} BPM  ${song.structure.key}  ${song.structure.duration.toFixed(0)}s` }) : null
        )
      ),
      el(
        "div",
        { class: "row" },
        song.urls?.mix ? el("button", { class: "btn small", text: "play the mix", onclick: (e) => play(song.urls.mix, e.target) }) : null,
        song.urls?.stem ? el("button", { class: "btn small", text: "play the guitar", onclick: (e) => play(song.urls.stem, e.target) }) : null,
        ...Object.keys(song.urls || {}).filter((k) => k.startsWith("layer-")).map((k) => el("button", { class: "btn small ghost", text: k.replace("layer-", ""), title: "play this layer alone", onclick: (e) => play(song.urls[k], e.target) })),
        el("span", { class: "grow" }),
        el("button", { class: "btn small", text: "Add audio", onclick: addAudio }),
        el("button", { class: "btn small", text: "Look up", title: "Album, year, credits", onclick: () => attempt(async () => { song = await api.post(`/api/songs/${songId}/lookup`); render(); }) }),
        !song.stem && song.mix ? el("button", { class: "btn small", text: "Isolate guitar", onclick: isolate }) : null,
        el("button", { class: `btn small ${song.parts?.length ? "" : "primary"}`, text: song.parts?.length ? "Re-map" : "Map it", onclick: analyse }),
        el("button", { class: "btn small", text: "Research", title: "Hand the research brief to the researcher", onclick: research })
      ),
      el("div", { class: "hint", id: "song-progress" })
    )
  );
  for (const note of song.notes || []) host.lastChild.append(el("div", { class: "notice", text: note }));

  if (!song.structure) {
    host.append(el("div", { class: "rig-empty" }, el("p", { text: song.mix || song.stem ? "Not mapped yet." : "Give it the song's audio, then map it." }), song.mix || song.stem ? el("button", { class: "btn primary", text: "Map it", onclick: analyse }) : el("button", { class: "btn primary", text: "Add audio", onclick: addAudio })));
    return;
  }
  host.append(timeline(), partsGrid());
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
      sketch.suggestions?.length ? el("details", {}, el("summary", { class: "hint", text: "what the measurements suggest" }), el("ul", { class: "hint" }, ...sketch.suggestions.map((s) => el("li", { text: s })))) : null,
      el(
        "div",
        { class: "p-actions" },
        el("button", { class: `btn small ${part.chosen ? "primary" : ""}`, text: part.chosen ? "chosen" : "choose", onclick: () => toggle(part) }),
        el("button", { class: "btn small", text: "play", onclick: (e) => playPart(part, e.target) }),
        el("button", { class: "btn small", text: "on my guitar", title: "Render this part as a DI on a learned guitar", onclick: () => renderDi(part) }),
        el("button", { class: "btn small", text: "tune", title: "Tune a rig against this part", onclick: () => goTo("tune", { songId: songId, partId: part.part_id, diTake: part.di_take }) }),
        el("button", { class: "btn small ghost", text: "ask", onclick: () => ask(`For ${song.title}, part ${part.part_id} (${part.label}, ${sketch.summary}): what gear made this, and how should I start building it?`) })
      )
    );
    grid.append(card);
  }
  box.append(grid);
  return box;
}

async function playPart(part, button) {
  const result = await attempt(() => api.get(`/api/songs/${songId}/parts/${part.part_id}/audio`));
  if (result) play(result.url, button);
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

async function renderDi(part) {
  const [{ profiles }, pickups] = await Promise.all([api.get("/api/learn/profiles"), api.get("/api/learn/pickups").catch(() => ({ types: [] }))]);
  const answer = await prompt(`Play "${part.label}" on your guitar`, [
    { type: "note", text: "Renders the part's notes on a learned guitar, on the record's clock - then candidates can be scored frame by frame. Give a tab's MIDI path for the right notes; otherwise the stem is transcribed." },
    { name: "profile_id", label: "Guitar", type: "select", value: profiles[0]?.profile_id || "synthetic", options: [...profiles.map((p) => ({ value: p.profile_id, label: p.label || p.profile_id })), { value: "synthetic", label: "the synthetic string" }] },
    { name: "midi", label: "Tab MIDI (path, optional)", placeholder: "C:/tabs/riff.mid" },
    { name: "like", label: "The record's guitar, if it was not yours", type: "select", value: "", options: [{ value: "", label: "same as mine" }, ...pickups.types.map((t) => ({ value: t.name, label: `${t.name} - ${t.description}` }))] },
    { name: "amount", label: "How far toward it (%)", type: "number", value: 100 },
    { name: "power_chords", label: "Put power-chord fifths back", type: "checkbox", value: false },
  ]);
  if (!answer) return;
  await attempt(async () => {
    const amount = Math.max(0, Math.min(100, Number(answer.amount ?? 100))) / 100;
    const { job_id } = await api.post(`/api/songs/${songId}/parts/${part.part_id}/di`, { profile_id: answer.profile_id, midi: answer.midi || null, power_chords: answer.power_chords, like: answer.like || null, amount });
    const result = await track(job_id, { onUpdate: progressInto });
    progressInto(null);
    toast(`Rendered: ${result.events} notes (${result.source})`, { kind: "ok", hint: result.warnings?.[0] });
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
