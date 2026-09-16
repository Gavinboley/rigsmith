/* The rig builder.
 *
 * Fourteen slots as a pedalboard, drag to reorder, scenes along the bottom
 * mapped to the five footswitches. Every mutation round-trips through the
 * server and re-renders from the response, so what you see is the validated
 * state rather than an optimistic guess the server might disagree with.
 */

import { api } from "../api.js";
import { $, $$, attempt, clear, el, pct, prompt, toast } from "../ui.js";

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

function paramSummary(block) {
  const entries = Object.entries(block.params || {});
  if (!entries.length) return null;
  return entries
    .slice(0, 3)
    .map(([key, value]) => `${key} ${value}`)
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
      ? el("span", { class: "slot-params", text: paramSummary(block) })
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

async function refreshDrafts(selectId = null) {
  const { drafts } = await api.drafts();
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
  apply(await api.draft(wanted));
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

/* -------------------------------------------------------------------- init */

export function init() {
  $("#new-draft").onclick = newDraft;
  $("#new-draft-empty").onclick = newDraft;

  $("#draft-select").onchange = async (event) => {
    await attempt(() => api.draft(event.target.value).then(apply));
  };

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

  $("#new-scene").onclick = () => editScene(null);

  $("#preview-btn").onclick = () =>
    attempt(async () => {
      const preview = await api.preview(current.draft_id);
      toast(preview.summary, { kind: preview.ok ? "ok" : "error", timeout: 9000 });
      $("#findings").prepend(
        el("div", { class: "finding info", text: preview.rendered })
      );
    });

  $("#export-btn").onclick = () =>
    attempt(async () => {
      const { path } = await api.exportDraft(current.draft_id);
      toast("Exported", { kind: "ok", hint: path });
    });

  $("#write-btn").onclick = () =>
    attempt(async () => {
      const dry = await api.writeDraft(current.draft_id, false);
      toast(dry.message, { kind: dry.ok ? "" : "error", timeout: 11000 });
    });

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
