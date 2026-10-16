/* "Sound like": move a DI toward another guitar's pickup.
 *
 * One control, used wherever a DI is made - a riff in the guitar lab, a
 * song's part, an autopilot run - so the choice reads the same everywhere:
 * as played, a pickup type, or another guitar you have taught it.
 */

import { api } from "./api.js";
import { el } from "./ui.js";

let cache = null;

async function known() {
  if (!cache) cache = api.get("/api/learn/pickups").catch(() => ({ types: [], guitars: [] }));
  return cache;
}

/** Forget the list - after a guitar is learned or removed. */
export function refreshPickups() {
  cache = null;
}

/**
 * A select and an amount slider.
 * @param {object} opts
 * @param {string} [opts.except] a profile id to leave out (the guitar playing)
 * @param {string} [opts.none] the label for "leave it alone"
 * @param {Array<[string, string]>} [opts.extra] options before the types
 */
export function likeControls({ except = null, none = "as played", extra = [] } = {}) {
  const select = el("select", { title: "Move the DI toward another pickup", "aria-label": "Sound like" }, el("option", { value: "", text: none }), ...extra.map(([value, text]) => el("option", { value, text })));
  const amount = el("input", { type: "range", min: 0, max: 100, value: 100, step: 5, style: "width:110px", title: "How far toward it" });
  const shown = el("span", { class: "hint mono", text: "100%" });
  amount.oninput = () => (shown.textContent = `${amount.value}%`);
  const wrap = el("span", { class: "like-controls" }, el("span", { class: "hint", text: "sound like" }), select, amount, shown);
  const sync = () => {
    const off = !select.value || select.value === "auto";
    amount.disabled = off;
    shown.style.opacity = off ? 0.4 : 1;
  };
  select.onchange = sync;
  sync();
  known().then(({ types, guitars }) => {
    const typeGroup = el("optgroup", { label: "pickup types" }, ...types.map((t) => el("option", { value: t.name, text: `${t.name} - ${t.description}` })));
    const mine = guitars.filter((g) => g.profile_id !== except);
    select.append(typeGroup);
    if (mine.length) select.append(el("optgroup", { label: "your guitars" }, ...mine.map((g) => el("option", { value: g.profile_id, text: `${g.label} (${(g.f0_hz / 1000).toFixed(1)} kHz)` }))));
  });
  return {
    node: wrap,
    get like() {
      return select.value || null;
    },
    get amount() {
      return Number(amount.value) / 100;
    },
    set(value) {
      select.value = value;
      sync();
    },
  };
}
