/* Ctrl+K: go anywhere, or ask.
 *
 * Fourteen panels and everything inside them is a lot of places to know
 * about. Type part of a name - a panel, a song, a rig, a guitar, a recipe -
 * and Enter goes there; anything that matches nothing is a question for the
 * assistant, so the palette is never a dead end.
 */

import { api } from "./api.js";
import { ask } from "./chat.js";
import { goTo } from "./nav.js";
import { clear, el, when } from "./ui.js";

const PANELS = [
  ["autopilot", "Autopilot", "a song in, a rig and a listening test out"],
  ["songs", "Songs", "the record mapped into sections and guitar parts"],
  ["recipes", "Research", "the gear behind a tone, with sources"],
  ["guitar", "Guitar lab", "teach it your guitar; play riffs on it"],
  ["takes", "My playing", "your recordings"],
  ["captures", "Captures", "NAM captures and TONE3000"],
  ["rig", "Rig", "the 14-slot pedalboard"],
  ["match", "Match", "score against the record; blind A/B"],
  ["tune", "Tune", "let it turn the knobs"],
  ["loop", "Loop", "the real Core between an output and an input"],
  ["device", "Device", "pairing, MIDI, USB backup"],
  ["workspace", "Workspace", "what is on disk; background jobs"],
  ["settings", "Settings", "accuracy or local mode; who answers"],
];

let overlay = null;
let items = [];
let index = 0;

async function gather() {
  const [songs, drafts, profiles, recipes] = await Promise.all([
    api.get("/api/songs").catch(() => ({ songs: [] })),
    api.get("/api/drafts").catch(() => ({ drafts: [] })),
    api.get("/api/learn/profiles").catch(() => ({ profiles: [] })),
    api.get("/api/recipes").catch(() => ({ recipes: [] })),
  ]);
  return [
    ...PANELS.map(([panel, title, hint]) => ({ kind: "panel", title, hint, go: () => goTo(panel) })),
    ...(songs.songs || []).map((s) => ({ kind: "song", title: s.title + (s.artist ? ` - ${s.artist}` : ""), hint: `${s.parts} parts, ${s.chosen} chosen`, go: () => goTo("songs", { songId: s.song_id }) })),
    ...(drafts.drafts || []).map((d) => ({ kind: "rig", title: d.name || d.draft_id, hint: `${d.blocks} blocks, ${when(d.updated_at)}`, go: () => goTo("rig", { draftId: d.draft_id }) })),
    ...(profiles.profiles || []).map((p) => ({ kind: "guitar", title: p.label || p.profile_id, hint: `${p.sample_count} notes`, go: () => goTo("guitar", { profileId: p.profile_id }) })),
    ...(recipes.recipes || []).map((r) => ({ kind: "recipe", title: r.title || r.recipe_id, hint: "recipe", go: () => goTo("recipes", { recipeId: r.recipe_id }) })),
  ];
}

/** Every word typed appears somewhere in the title (or the hint), in any order. */
function matches(item, text) {
  const haystack = `${item.title} ${item.hint} ${item.kind}`.toLowerCase();
  return text.toLowerCase().split(/\s+/).filter(Boolean).every((word) => haystack.includes(word));
}

function render(list, input) {
  const text = input.value.trim();
  const shown = (text ? items.filter((item) => matches(item, text)) : items.filter((i) => i.kind === "panel")).slice(0, 9);
  if (text) shown.push({ kind: "ask", title: `Ask: "${text}"`, hint: "the assistant, in the chat", go: () => ask(text, { send: true }) });
  index = Math.min(index, shown.length - 1);
  clear(list);
  shown.forEach((item, i) => {
    list.append(
      el(
        "div",
        { class: `palette-item ${i === index ? "on" : ""}`, onmousedown: (event) => { event.preventDefault(); choose(item); } },
        el("span", { class: "tag", text: item.kind }),
        el("span", { class: "title", text: item.title }),
        el("span", { class: "hint", text: item.hint })
      )
    );
  });
  return shown;
}

function choose(item) {
  close();
  item?.go();
}

function close() {
  overlay?.remove();
  overlay = null;
}

export async function open() {
  if (overlay) return;
  const input = el("input", { type: "text", placeholder: "Go to a page, a song, a rig, a guitar - or ask anything", "aria-label": "Search" });
  const list = el("div", { class: "palette-list" });
  overlay = el("div", { class: "palette-overlay", onmousedown: (event) => event.target === overlay && close() }, el("div", { class: "palette" }, input, list));
  document.body.append(overlay);
  input.focus();
  items = PANELS.map(([panel, title, hint]) => ({ kind: "panel", title, hint, go: () => goTo(panel) }));
  index = 0;
  let shown = render(list, input);
  gather().then((found) => {
    items = found;
    if (overlay) shown = render(list, input);
  });
  input.oninput = () => {
    index = 0;
    shown = render(list, input);
  };
  input.onkeydown = (event) => {
    if (event.key === "Escape") close();
    else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      index = (index + (event.key === "ArrowDown" ? 1 : shown.length - 1)) % Math.max(shown.length, 1);
      shown = render(list, input);
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(shown[index]);
    }
  };
}

export function install() {
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      overlay ? close() : open();
    }
  });
}
