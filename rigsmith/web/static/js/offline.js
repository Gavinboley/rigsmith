/* When no model answers, the chat still does what needs none.
 *
 * In local mode with nothing on the model server - or accuracy mode signed
 * out - every message would end at "start LM Studio". Some of what people
 * type needs no model at all: a turn by ear ("a bit less bright"), an undo,
 * or which page does a thing. Under the error, those are offered as
 * buttons - each still a click you make, never done on the words alone.
 */

import { api } from "./api.js";
import { suggest } from "./palette.js";
import { currentDraft } from "./panels/rig.js";
import { el, toast } from "./ui.js";

const UNDO = /^\s*(undo|undo (that|it|the last (edit|change))|go back|put it back|revert( that)?)\s*[.!]*\s*$/i;

/** The rig a by-ear word would turn: the one open on the Rig page, else the latest edited. */
async function rigInPlay() {
  const open = currentDraft();
  if (open) return open;
  const { drafts } = await api.get("/api/drafts").catch(() => ({ drafts: [] }));
  const latest = [...(drafts || [])].sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))[0];
  return latest ? { draft_id: latest.draft_id, name: latest.name } : null;
}

function button(text, title, action) {
  return el("button", {
    class: "btn small",
    text,
    title,
    onclick: async (event) => {
      event.target.disabled = true;
      try {
        await action();
      } catch (error) {
        toast(error.message || String(error), { kind: "error", hint: error.hint });
        event.target.disabled = false;
      }
    },
  });
}

/** Buttons for what ``text`` asked that needs no model, or null when there is nothing to offer. */
export async function helpWithout(text) {
  const offers = [];
  const rig = await rigInPlay();
  if (rig && UNDO.test(text)) {
    offers.push(button(`Undo the last edit to "${rig.name}"`, "Step the draft back - the Rig page's Undo", async () => {
      const result = await api.post(`/api/drafts/${encodeURIComponent(rig.draft_id)}/undo`);
      toast(result.message, { kind: "ok" });
    }));
  } else if (rig) {
    const heard = await api.get("/api/nudge/parse", { text }).catch(() => null);
    if (heard?.understood?.length) {
      offers.push(button(`Turn "${rig.name}": ${heard.understood.join(", ")}`, "Move its knobs a step that way - the Rig page's By ear", async () => {
        const result = await api.post(`/api/drafts/${encodeURIComponent(rig.draft_id)}/nudge`, { how: heard.understood.join(", ") });
        toast(result.message, { kind: "ok", timeout: 8000 });
      }));
    }
  }
  // A turn or an undo is the answer itself; otherwise, where to go.
  for (const item of offers.length ? [] : suggest(text, 3)) {
    offers.push(button(item.kind === "do" ? item.title : `Open ${item.title}`, item.hint, async () => item.go()));
  }
  if (!offers.length) return null;
  return el(
    "div",
    { class: "toolcard offline" },
    el("div", { class: "hint", text: "Without a model, this much can be done from here:" }),
    el("div", { class: "row" }, ...offers)
  );
}
