/* The rig builder.
 *
 * Fourteen slots as a pedalboard, drag to reorder, scenes along the bottom
 * mapped to the five footswitches. Every mutation round-trips through the
 * server and re-renders from the response, so what you see is the validated
 * state rather than an optimistic guess the server might disagree with.
 */

import { api } from "../api.js";
import { $, $$, attempt, clear, el, pct, prompt, spotlight, toast } from "../ui.js";
import { onArrive, goTo } from "../nav.js";

const SLOTS = 14;
const FOOTSWITCHES = 5;

const CATEGORIES = [
  { value: "effect", label: "Effect" },
  { value: "amp", label: "Amp" },
  { value: "nam", label: "NAM capture" },
  { value: "clone", label: "Amp clone" },
  { value: "cab", label: "Cab" },
  { value: "ir", label: "IR" },
];

let current = null; // the draft payload from the server
let sortable = null;

/* ------------------------------------------------------------------ render */

function renderMeter(validation) {
  const cpu = validation?.estimated_cpu ?? 0;
  const fill = $("#cpu-fill");
  fill.style.width = `${Math.min(100, cpu * 100)}%`;
  fill.classList.toggle("warn", cpu >= 0.85 && cpu < 1);
  fill.classList.toggle("bad", cpu >= 1);
  $("#cpu-value").textContent = pct(cpu);
}

/* The controls as the Core's screen reads them (from the server, which has
 * the catalog), not as the wire stores them - "Gain 62%", not "GainA 0.62". */
function readingsFor(block) {
  const rows = current?.readings?.[String(block.slot)];
  if (rows) return rows;
  return Object.entries(block.params || {}).map(([key, value]) => [key, String(value)]);
}

function paramSummary(block) {
  const rows = readingsFor(block);
  if (!rows.length) return null;
  return rows
    .slice(0, 3)
    .map(([label, value]) => `${label} ${value.replace(" of travel", "")}`)
    .join("  ");
}

function blockTile(block) {
  const name = block.model_name || block.model_id;
  return el(
    "li",
    {
      class: `slot${block.enabled === false ? " bypassed" : ""}`,
      "data-slot": block.slot,
      "data-category": block.category,
    },
    el("span", { class: "slot-num", text: String(block.slot).padStart(2, "0") }),
    el("span", { class: "slot-cat", text: block.category }),
    el("span", { class: "slot-name", text: name }),
    paramSummary(block)
      ? el("span", {
          class: "slot-params",
          text: paramSummary(block),
          title: readingsFor(block).map(([label, value]) => `${label}: ${value}`).join("\n"),
        })
      : null,
    el(
      "div",
      { class: "slot-actions" },
      el("button", {
        class: "btn small",
        text: block.enabled === false ? "on" : "off",
        title: "Toggle in the rig's base state",
        onclick: (event) => {
          event.stopPropagation();
          attempt(() =>
            api
              .patchBlock(current.draft_id, block.slot, {
                enabled: block.enabled === false,
              })
              .then(apply)
          );
        },
      }),
      el("button", {
        class: "btn small",
        text: "edit",
        onclick: (event) => {
          event.stopPropagation();
          editParam(block);
        },
      }),
      el("button", {
        class: "btn small danger",
        text: "x",
        title: "Remove",
        onclick: (event) => {
          event.stopPropagation();
          attempt(() =>
            api.removeBlock(current.draft_id, block.slot).then(apply)
          );
        },
      })
    )
  );
}

function emptyTile(slot) {
  return el(
    "li",
    {
      class: "slot empty",
      "data-slot": slot,
      title: `Add a block in slot ${slot}`,
      onclick: () => addBlock(slot),
    },
    el("span", { class: "slot-num", text: String(slot).padStart(2, "0") }),
    el("span", { text: "+" })
  );
}

function renderBoard() {
  const board = clear($("#board"));
  const bySlot = new Map((current.rig.blocks || []).map((b) => [b.slot, b]));
  for (let slot = 1; slot <= SLOTS; slot += 1) {
    const block = bySlot.get(slot);
    board.append(block ? blockTile(block) : emptyTile(slot));
  }

  if (sortable) sortable.destroy();
  sortable = new Sortable(board, {
    animation: 140,
    draggable: ".slot:not(.empty)",
    ghostClass: "sortable-ghost",
    chosenClass: "sortable-chosen",
    onEnd: (event) => {
      // The board always renders all 14 slots in order, filled or not, so a
      // list index maps straight onto a slot number. Reading the slot off
      // the element at newIndex would not work - after the reorder, that
      // element is the dragged item itself.
      const from = Number(event.item.dataset.slot);
      const to = event.newIndex + 1;
      if (from === to) {
        return;
      }
      attempt(() => api.moveBlock(current.draft_id, from, to).then(apply));
    },
  });
}

function sceneDetail(scene) {
  const on = Object.entries(scene.states || {})
    .filter(([, state]) => state === "on")
    .map(([slot]) => slot);
  const off = Object.entries(scene.states || {})
    .filter(([, state]) => state === "off")
    .map(([slot]) => slot);
  const parts = [];
  if (on.length) parts.push(`on ${on.join(",")}`);
  if (off.length) parts.push(`off ${off.join(",")}`);
  return parts.join("  ") || "no changes";
}

function renderScenes() {
  const host = clear($("#footswitches"));
  const scenes = current.rig.scenes || [];

  for (let number = 1; number <= FOOTSWITCHES; number += 1) {
    const assigned = scenes.filter((s) => s.footswitch === number);
    const node = el(
      "div",
      { class: `fs${assigned.length ? " assigned" : " empty"}` },
      el("span", { class: "fs-num", text: `FS${number}` }),
      el("span", { class: "fs-led" })
    );

    if (assigned.length) {
      for (const scene of assigned) {
        node.append(
          el("span", {
            class: "fs-scene",
            text: scene.name,
            title: "Click to edit",
            onclick: () => editScene(scene),
          }),
          el("span", { class: "fs-detail", text: sceneDetail(scene) })
        );
      }
    } else {
      node.append(
        el("span", {
          class: "fs-scene",
          text: "unassigned",
          onclick: () => editScene(null, number),
        })
      );
    }
    host.append(node);
  }

  const loose = scenes.filter((s) => !s.footswitch);
  if (loose.length) {
    host.append(
      el(
        "div",
        { class: "fs" },
        el("span", { class: "fs-num", text: "NO FS" }),
        el("span", { class: "fs-led" }),
        ...loose.map((scene) =>
          el("span", {
            class: "fs-scene",
            text: scene.name,
            onclick: () => editScene(scene),
          })
        )
      )
    );
  }
}

function renderFindings() {
  const host = clear($("#findings"));
  for (const finding of current.validation.findings || []) {
    const where = finding.slot
      ? `slot ${finding.slot}: `
      : finding.scene
      ? `scene ${finding.scene}: `
      : "";
    host.append(
      el("div", {
        class: `finding ${finding.severity}`,
        text: where + finding.message,
      })
    );
  }
}

function apply(payload) {
  current = payload;
  $("#rig-empty").hidden = true;
  $("#rig-body").hidden = false;
  $("#rig-name").textContent = payload.rig.name;
  $("#chain-summary").textContent = `${payload.rig.blocks.length}/${SLOTS} blocks  ${
    payload.stereo ? "stereo" : "mono"
  }`;
  $("#signal-path").value = payload.rig.signal_path;
  $("#rig-tempo").value = payload.rig.tempo ?? "";
  $("#undo-draft").disabled = !payload.undo;
  $("#undo-draft").title = payload.undo ? `Step back before the last edit (Ctrl+Z) - ${payload.undo} to go` : "Nothing to undo";
  $("#redo-draft").disabled = !payload.redo;
  renderMeter(payload.validation);
  renderBoard();
  renderScenes();
  renderFindings();
  return payload;
}

/* ------------------------------------------------------------------ actions */

async function addBlock(slot) {
  const values = await prompt("Add a block", [
    {
      name: "category",
      label: "Kind",
      type: "select",
      value: "effect",
      options: CATEGORIES,
    },
    {
      name: "model_id",
      label: "Model or capture name",
      placeholder: "e.g. 82 LEAD 800 100W",
    },
  ]);
  if (!values || !values.model_id) return;
  await attempt(() =>
    api
      .addBlock(current.draft_id, {
        category: values.category,
        model_id: values.model_id,
        slot,
      })
      .then(apply)
  );
}

async function editParam(block) {
  const existing = Object.entries(block.params || {});
  const values = await prompt(`${block.model_name || block.model_id}`, [
    {
      name: "name",
      label: "Parameter",
      placeholder: "gain, mix, input_gain_db ...",
      value: existing.length ? existing[0][0] : "",
    },
    {
      name: "value",
      label: "Value",
      value: existing.length ? existing[0][1] : "",
    },
  ]);
  if (!values || !values.name) return;

  let parsed = values.value;
  if (parsed !== "" && !Number.isNaN(Number(parsed))) parsed = Number(parsed);
  await attempt(() =>
    api
      .patchBlock(current.draft_id, block.slot, {
        name: values.name,
        value: parsed,
      })
      .then(apply)
  );
}

async function editScene(scene, footswitch = null) {
  const values = await prompt(scene ? `Scene: ${scene.name}` : "New scene", [
    { name: "name", label: "Name", value: scene?.name ?? "", placeholder: "Clean verse" },
    {
      name: "on",
      label: "Slots to switch on (comma separated)",
      value: scene
        ? Object.entries(scene.states || {})
            .filter(([, s]) => s === "on")
            .map(([slot]) => slot)
            .join(",")
        : "",
    },
    {
      name: "off",
      label: "Slots to switch off",
      value: scene
        ? Object.entries(scene.states || {})
            .filter(([, s]) => s === "off")
            .map(([slot]) => slot)
            .join(",")
        : "",
    },
    {
      name: "footswitch",
      label: "Footswitch (1-5, blank for none)",
      value: scene?.footswitch ?? footswitch ?? "",
    },
  ]);
  if (!values || !values.name) return;

  const slots = (text) =>
    text
      .split(",")
      .map((part) => parseInt(part.trim(), 10))
      .filter((n) => Number.isFinite(n));

  await attempt(() =>
    api
      .setScene(current.draft_id, {
        name: values.name,
        on_slots: slots(values.on),
        off_slots: slots(values.off),
        footswitch: values.footswitch ? Number(values.footswitch) : null,
        // The UI shows the whole scene, so it sends the whole scene.
        reset: true,
      })
      .then(apply)
  );
}

/* Two refreshes can be in flight at once - showing the panel starts one, and
 * arriving from a compiled recipe starts another a microtask later. Without
 * a guard they race, and the loser is whichever the network happens to
 * answer second, so "open the rig I just compiled" sometimes opened the
 * previously selected one instead. A sequence token settles it: the newest
 * request wins, and an older response is dropped on arrival. */
let refreshToken = 0;

async function refreshDrafts(selectId = null) {
  const token = ++refreshToken;
  const { drafts } = await api.drafts();
  if (token !== refreshToken) return;

  const select = clear($("#draft-select"));
  for (const draft of drafts) {
    select.append(
      el("option", {
        value: draft.draft_id,
        text: `${draft.rig?.name ?? draft.name} (${draft.blocks} blocks)`,
      })
    );
  }
  if (!drafts.length) {
    $("#rig-body").hidden = true;
    $("#rig-empty").hidden = false;
    current = null;
    return;
  }
  const wanted = selectId && drafts.some((d) => d.draft_id === selectId)
    ? selectId
    : drafts[0].draft_id;
  select.value = wanted;
  const draft = await api.draft(wanted);
  if (token !== refreshToken) return;
  apply(draft);
}

async function newDraft() {
  const values = await prompt("New rig", [
    { name: "name", label: "Name", placeholder: "Cherub Rock" },
    { name: "notes", label: "Notes (what tone are you chasing?)", placeholder: "optional" },
  ]);
  if (!values || !values.name) return;
  const created = await attempt(() =>
    api.createDraft({ name: values.name, notes: values.notes || null })
  );
  if (created) await refreshDrafts(created.draft_id);
}

/* ------------------------------------------------------------------ write */

/* The one irreversible thing this tool does. A rig built by hand on the
 * device's own screen exists nowhere else, so the diff is shown in full
 * before the question is asked, and the backup gate is a checkbox that
 * starts off - waiving it has to be a decision somebody made. */

let writeJob = null;

async function startWrite() {
  if (!current) throw new Error("No rig open");

  const preview = await api.previewWrite(current.draft_id);
  if (!preview.validation?.ok) {
    toast(preview.message, { kind: "error", timeout: 12000 });
    return;
  }

  const answer = await prompt(`Write "${current.rig.name}" to the device?`, [
    { type: "pre", text: preview.diff_rendered || preview.message },
    {
      type: "note",
      text: `Estimated CPU ~${Math.round(
        (preview.validation.estimated_cpu || 0) * 100
      )}%. The device's own meter is the one that counts.`,
    },
    {
      type: "note",
      class: "notice warn",
      text:
        "A backup is taken first, which needs the Core in Menu > USB " +
        "Transfer. Without it the write is refused, because whatever is in " +
        "that slot has nowhere else to exist.",
    },
    {
      name: "no_backup",
      label: "Write anyway if no backup can be taken",
      type: "checkbox",
      value: false,
    },
  ], { okLabel: "Write" });

  if (!answer) return;

  const { job_id } = await api.writeDraft(current.draft_id, {
    allowWithoutBackup: answer.no_backup,
  });
  writeJob = job_id;
  toast("Writing - looking for the device...", { timeout: 4000 });
}

export function onJobs(jobs) {
  if (!writeJob) return;
  const job = jobs.find((candidate) => candidate.id === writeJob);
  if (!job?.finished) return;

  writeJob = null;
  attempt(async () => {
    const { result, state, error } = await api.job(job.id);
    if (state !== "succeeded") {
      toast(error || "The write did not complete.", { kind: "error", timeout: 12000 });
      return;
    }
    // Written but not exact: on the device, and not quite the draft.
    toast(result.message, {
      kind: result.written ? (result.exact ? "ok" : "warn") : "error",
      timeout: 14000,
    });
    // The unverified-protocol warning is the most important sentence in a
    // write, and a boolean in the payload is very easy to skip past.
    for (const caveat of result.report?.caveats || []) {
      toast(caveat, { kind: "error", timeout: 18000 });
    }
    // The device is the new baseline once it has been written to.
    if (result.written) await refreshDrafts(current?.draft_id ?? null);
  });
}

/* -------------------------------------------------------------------- init */

export function init() {
  $("#test-btn").onclick = () =>
    goTo("match", {
      rigName: current?.rig?.name || null,
      // Compiled rigs keep their recipe's id as the draft id, so a result
      // measured here can find its way back to the research it came from.
      recipeId: current?.draft_id || null,
    });

  $("#new-draft").onclick = newDraft;
  $("#new-draft-empty").onclick = newDraft;

  $("#draft-select").onchange = async (event) => {
    await attempt(() => api.draft(event.target.value).then(apply));
  };

  const step = (redo) =>
    current &&
    attempt(async () => {
      const result = await api.post(`/api/drafts/${encodeURIComponent(current.draft_id)}/undo`, {}, { redo });
      apply(result);
      toast(result.message, { kind: "ok", timeout: 4000 });
    });
  $("#undo-draft").onclick = () => step(false);
  $("#redo-draft").onclick = () => step(true);
  document.addEventListener("keydown", (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "z") return;
    // Only on the Rig page, and never inside a text box - that undo is the box's own.
    if ($('.panel[data-panel="rig"]').hidden || event.target.closest("input, textarea, select, [contenteditable]")) return;
    event.preventDefault();
    if (event.shiftKey ? current?.redo : current?.undo) step(event.shiftKey);
  });

  $("#dup-draft").onclick = () =>
    attempt(async () => {
      const copy = await api.duplicateDraft(current.draft_id);
      await refreshDrafts(copy.draft_id);
    }, { success: "Duplicated" });

  $("#del-draft").onclick = async () => {
    if (!current) return;
    if (!window.confirm(`Delete "${current.rig.name}"? This is local only.`)) return;
    await attempt(async () => {
      await api.deleteDraft(current.draft_id);
      await refreshDrafts();
    }, { success: "Deleted" });
  };

  $("#signal-path").onchange = (event) =>
    attempt(() =>
      api
        .updateRig(current.draft_id, { signal_path: event.target.value })
        .then(apply)
    );

  $("#rig-tempo").onchange = (event) =>
    attempt(() =>
      api
        .updateRig(current.draft_id, { tempo: Number(event.target.value) || 0 })
        .then(apply)
    );

  const nudge = (how) =>
    current &&
    attempt(async () => {
      const result = await api.post(`/api/drafts/${encodeURIComponent(current.draft_id)}/nudge`, { how, live: $("#nudge-live").checked });
      apply(result);
      toast(result.message, { kind: "ok", timeout: 6000 });
    });
  for (const button of document.querySelectorAll("#nudge-row [data-nudge]")) button.onclick = () => nudge(button.dataset.nudge);
  $("#nudge-text").onkeydown = (event) => {
    if (event.key === "Enter" && event.target.value.trim()) {
      nudge(event.target.value.trim());
      event.target.value = "";
    }
  };

  $("#new-scene").onclick = () => editScene(null);

  $("#preview-btn").onclick = () =>
    attempt(async () => {
      const preview = await api.preview(current.draft_id);
      toast(preview.summary, { kind: preview.ok ? "ok" : "error", timeout: 9000 });
      $("#findings").prepend(
        el("div", { class: "finding info", text: preview.rendered })
      );
    });

  $("#sheet-btn").onclick = () => {
    if (current) window.open(`/api/drafts/${encodeURIComponent(current.draft_id)}/sheet.html`, "_blank");
  };

  $("#export-btn").onclick = () =>
    attempt(async () => {
      const { path } = await api.exportDraft(current.draft_id);
      toast("Exported", { kind: "ok", hint: path });
    });

  $("#write-btn").onclick = () => attempt(startWrite);

  $("#rig-name").ondblclick = async () => {
    const values = await prompt("Rename rig", [
      { name: "name", label: "Name", value: current.rig.name },
    ]);
    if (values?.name) {
      await attempt(() =>
        api.updateRig(current.draft_id, { name: values.name }).then(apply)
      );
      await refreshDrafts(current.draft_id);
    }
  };

  return refreshDrafts();
}

export const onShow = () => refreshDrafts(current?.draft_id ?? null);

/** The draft open on the Rig page, if any: ``{draft_id, name}``. */
export const currentDraft = () => (current ? { draft_id: current.draft_id, name: current.rig.name } : null);

// Arriving from a compiled recipe: open the rig it just produced, rather
// than whatever was last selected.
onArrive("rig", ({ draftId, focus }) => {
  if (!draftId) return;
  attempt(async () => {
    await refreshDrafts(draftId);
    // Arriving to finish a tone by ear: straight to the words.
    if (focus === "nudge") spotlight($("#nudge-text"));
  });
});
