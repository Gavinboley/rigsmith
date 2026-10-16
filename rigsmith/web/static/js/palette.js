/* Ctrl+K: go anywhere, or ask.
 *
 * Fourteen panels and everything inside them is a lot of places to know
 * about. Type part of a name - a panel, a song, a rig, a guitar, a recipe -
 * and Enter goes there; anything that matches nothing is a question for the
 * assistant, so the palette is never a dead end.
 *
 * It also knows the things you can do, by what they are called rather than
 * where they live: "knob curves" or "local model" opens the page and points
 * at the control. It points rather than presses - several of them play
 * audio into the Core or write to it, and that should stay a click you make.
 */

import { api } from "./api.js";
import { ask } from "./chat.js";
import { goTo } from "./nav.js";
import { clear, el, toast, when } from "./ui.js";

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

/* [what it is called, page, the control, what it does, and what to do first
 * when the control is not there yet]. The control is a selector, or the text
 * on a button the page draws for itself. */
const ACTIONS = [
  ["Start autopilot", "autopilot", { text: "Start autopilot" }, "a song in, a rig out"],
  ["Add a song", "songs", "#song-new", "the record you are chasing"],
  ["Isolate the guitar", "songs", { text: "Isolate guitar" }, "split the stem out of the mix", "Pick a song with a mix and no stem yet - the button is on it."],
  ["Research a tone", "recipes", "#new-recipe", "the gear behind a sound"],
  ["Teach it my guitar", "guitar", "#profile-new", "every note on every string"],
  ["Check my level", "guitar", { text: "check my level" }, "is the input hot enough", "Pick or add a guitar first - the check is on its page."],
  ["Record a take", "takes", "#take-record", "over USB, both halves kept"],
  ["Compare guitars", "takes", "#guitar-compare", "yours against the record's"],
  ["Search TONE3000", "captures", "#t3k-search", "NAM captures online"],
  ["Scan for captures", "captures", "#scan-btn", "NAM files already on disk"],
  ["Nudge the tone by ear", "rig", "#nudge-text", "a bit brighter, too boomy"],
  ["Tone sheet", "rig", "#sheet-btn", "the rig as you would dial it by hand"],
  ["Write the rig to the Core", "rig", "#write-btn", "save it to the HeadRush"],
  ["Score against the record", "match", "#score-btn", "how close is it"],
  ["Start tuning", "tune", { text: "Start tuning" }, "let it turn the knobs"],
  ["Calibrate the loop", "loop", { text: "calibrate" }, "level and latency of the cables", "Set up a route first: which output feeds the Core and which input hears it."],
  ["Reamp a take", "loop", { text: "reamp a take" }, "through the real Core", "Set up a route first: which output feeds the Core and which input hears it."],
  ["Pair with the Core", "device", { text: "Pair" }, "over the network", "Already paired - forget the pairing on this page to pair again."],
  ["Back up the Core", "device", "#backup-btn", "copy its rigs over USB"],
  ["Measure knob curves", "device", "#catalog-calibrate", "what each knob really does"],
  ["Learn scene numbers", "device", "#device-scenes-learn", "so scenes can be written"],
  ["Tap tempo", "device", "#midi-tap", "over MIDI"],
  ["Verify a rig over MIDI", "device", "#midi-verify", "is the Core playing what was sent"],
  ["Clean up the workspace", "workspace", "#prune-dry", "see what would go first"],
  ["Test the local model", "settings", "#check-model", "can it drive the tools"],
];

let overlay = null;
let items = [];
let index = 0;

/** The control on ``panel`` that ``find`` names, if the page has drawn it. */
function locate(panel, find) {
  const section = document.querySelector(`.panel[data-panel="${panel}"]`) || document;
  if (typeof find === "string") return section.querySelector(find);
  const want = find.text.toLowerCase();
  return [...section.querySelectorAll("button")].find((b) => b.textContent.trim().toLowerCase() === want && b.offsetParent !== null) || null;
}

/** Go to the page and draw the eye to the control - it may take a moment to appear. */
function pointAt(panel, find, first) {
  goTo(panel);
  document.querySelectorAll(".spotlight").forEach((old) => old.classList.remove("spotlight"));
  const started = performance.now();
  const look = () => {
    const target = locate(panel, find);
    if (target && target.offsetParent !== null) {
      target.scrollIntoView({ block: "center", behavior: "smooth" });
      target.focus({ preventScroll: true });
      target.classList.remove("spotlight");
      void target.offsetWidth;
      target.classList.add("spotlight");
      setTimeout(() => target.classList.remove("spotlight"), 2000);
    } else if (performance.now() - started < 2500) {
      setTimeout(look, 100);
    } else if (first) {
      toast(first);
    }
  };
  requestAnimationFrame(look);
}

const actionItems = () => ACTIONS.map(([title, panel, find, hint, first]) => ({ kind: "do", title, hint, go: () => pointAt(panel, find, first) }));

async function gather() {
  const [songs, drafts, profiles, recipes] = await Promise.all([
    api.get("/api/songs").catch(() => ({ songs: [] })),
    api.get("/api/drafts").catch(() => ({ drafts: [] })),
    api.get("/api/learn/profiles").catch(() => ({ profiles: [] })),
    api.get("/api/recipes").catch(() => ({ recipes: [] })),
  ]);
  return [
    ...PANELS.map(([panel, title, hint]) => ({ kind: "panel", title, hint, go: () => goTo(panel) })),
    ...actionItems(),
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

/** Lower is better: the title starts with what was typed, then has every word, then only the hint does. */
function rank(item, text) {
  const title = item.title.toLowerCase();
  const typed = text.toLowerCase();
  if (title.startsWith(typed)) return 0;
  return typed.split(/\s+/).filter(Boolean).every((word) => title.includes(word)) ? 1 : 2;
}

function render(list, input) {
  const text = input.value.trim();
  const found = text ? items.filter((item) => matches(item, text)).map((item, i) => [rank(item, text), i, item]).sort((a, b) => a[0] - b[0] || a[1] - b[1]).map(([, , item]) => item) : items.filter((i) => i.kind === "panel");
  const shown = found.slice(0, 9);
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
  const input = el("input", { type: "text", placeholder: "Go to a page, a song, a rig, a guitar, something to do - or ask anything", "aria-label": "Search" });
  const list = el("div", { class: "palette-list" });
  overlay = el("div", { class: "palette-overlay", onmousedown: (event) => event.target === overlay && close() }, el("div", { class: "palette" }, input, list));
  document.body.append(overlay);
  input.focus();
  items = [...PANELS.map(([panel, title, hint]) => ({ kind: "panel", title, hint, go: () => goTo(panel) })), ...actionItems()];
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
