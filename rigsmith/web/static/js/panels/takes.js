/* Your own playing.
 *
 * Every comparison in this tool is against a record made on somebody else's
 * guitar. The one variable you can remove is the instrument, and this is
 * where that happens: play the part yourself, keep it, label it, and render
 * candidates through that instead.
 *
 * The labels are the panel's whole reason for existing. A folder of wav
 * files is not a library; "Les Paul, bridge, drop D" six months later is.
 */

import { api } from "../api.js";
import { $, attempt, clear, el, prompt, toast } from "../ui.js";
import { attachPicker } from "../picker.js";
import { goTo, onArrive } from "../nav.js";

const KIND_TONE = { di: "a2", wet: "", loop: "warn", reference: "" };

let kinds = {};
let origins = {};
let current = [];

/* ----------------------------------------------------------------- render */

function takeItem(take) {
  const gear = [take.guitar, take.pickup].filter(Boolean).join(" ");
  const node = el(
    "div",
    { class: "item", title: take.note || "" },
    el("span", {
      class: `tag ${KIND_TONE[take.kind] ?? ""}`,
      text: take.kind,
      title: kinds[take.kind] || "",
    }),
    el("span", { class: "title", text: take.take_id }),
    gear
      ? el("span", { class: "meta", text: gear })
      : el("span", {
          class: "tag warn",
          text: "no guitar",
          title:
            "Which instrument this was is the whole point of keeping it. " +
            "Click to say.",
        }),
    take.tuning ? el("span", { class: "meta", text: take.tuning }) : null,
    el("span", { class: "spacer" }),
    el("span", { class: "meta", text: `${(take.duration || 0).toFixed(1)}s` }),
    take.lossy
      ? el("span", {
          class: "tag warn",
          text: "lossy",
          title:
            "Fine to chase. Not something to reamp through - the encoder's " +
            "artefacts get printed into every candidate equally.",
        })
      : null,
    !take.exists
      ? el("span", { class: "tag bad", text: "missing", title: take.path })
      : null
  );

  if (take.reamps && take.exists) {
    node.append(
      el("button", {
        class: "btn small",
        text: "use as DI",
        title: "Render candidate captures through this, in the Match panel",
        onclick: () => goTo("match", { diTakeId: take.take_id }),
      })
    );
  } else if (take.exists) {
    node.append(
      el("button", {
        class: "btn small",
        text: "use as target",
        title: "Match candidates against this, in the Match panel",
        onclick: () => goTo("match", { targetTakeId: take.take_id }),
      })
    );
  }

  node.append(
    el("button", {
      class: "btn small ghost",
      text: "label",
      onclick: () => editTake(take),
    })
  );
  return node;
}

function render(payload) {
  current = payload.takes || [];
  kinds = payload.kinds || {};
  origins = payload.origins || {};

  const host = clear($("#takes-list"));
  if (!current.length) {
    host.append(
      el("div", {
        class: "notice",
        text:
          "Nothing here yet. Record the part once - a DI box, the Core's USB " +
          "channel 5, or a loop played into the pedal - and every candidate " +
          "afterwards gets rendered through your guitar rather than a " +
          "stranger's.",
      })
    );
  } else {
    for (const take of current) host.append(takeItem(take));
  }

  const unlabelled = current.filter((take) => !take.guitar);
  const summary = clear($("#takes-summary"));
  if (payload.guitars?.length) {
    summary.append(
      el("span", { class: "hint", text: `guitars: ${payload.guitars.join(", ")}` })
    );
  }
  if (unlabelled.length) {
    summary.append(
      el("div", {
        class: "finding warning",
        text:
          `${unlabelled.length} take(s) have no guitar recorded. That label ` +
          "is the reason this library is worth keeping.",
      })
    );
  }
}

async function refresh() {
  const payload = await attempt(() => api.get("/api/takes"));
  if (payload) render(payload);
}

/* ---------------------------------------------------------------- actions */

const KIND_OPTIONS = () =>
  Object.entries(kinds).map(([value, label]) => ({ value, label: `${value} - ${label}` }));

const ORIGIN_OPTIONS = () =>
  Object.entries(origins).map(([value, label]) => ({ value, label: `${value} - ${label}` }));

async function addTake() {
  const path = $("#take-path").value.trim();
  if (!path) throw new Error("Choose a recording first");

  const values = await prompt("Add a recording", [
    {
      type: "note",
      text:
        "A DI is dry, straight from the guitar, and is the only kind that " +
        "can be rendered through a candidate rig. Anything already through " +
        "an amp - including a loop off the pedal - is a reference.",
    },
    {
      name: "kind",
      label: "What is it?",
      type: "select",
      value: "di",
      options: KIND_OPTIONS(),
    },
    {
      name: "origin",
      label: "Where from?",
      type: "select",
      value: "file",
      options: ORIGIN_OPTIONS(),
    },
    { name: "guitar", label: "Which guitar?", placeholder: "Les Paul Standard" },
    { name: "pickup", label: "Pickup", placeholder: "bridge" },
    { name: "tuning", label: "Tuning", placeholder: "E standard" },
    { name: "note", label: "What did you play?", placeholder: "the riff, twice" },
    { name: "take_id", label: "Name it", placeholder: "defaults to the filename" },
  ]);
  if (!values) return;

  const added = await api.post("/api/takes", {
    path,
    take_id: values.take_id || null,
    kind: values.kind,
    origin: values.origin,
    guitar: values.guitar || null,
    pickup: values.pickup || null,
    tuning: values.tuning || null,
    note: values.note || null,
  });
  $("#take-path").value = "";
  for (const concern of added.concerns || []) {
    toast(concern, { kind: "error", timeout: 14000 });
  }
  await refresh();
}

async function editTake(take) {
  const values = await prompt(`Label "${take.take_id}"`, [
    {
      name: "kind",
      label: "What is it?",
      type: "select",
      value: take.kind,
      options: KIND_OPTIONS(),
    },
    { name: "guitar", label: "Which guitar?", value: take.guitar || "" },
    { name: "pickup", label: "Pickup", value: take.pickup || "" },
    { name: "tuning", label: "Tuning", value: take.tuning || "" },
    { name: "note", label: "What did you play?", value: take.note || "" },
  ]);
  if (!values) return;

  await attempt(async () => {
    await api.patch(`/api/takes/${take.take_id}`, {
      kind: values.kind,
      guitar: values.guitar || null,
      pickup: values.pickup || null,
      tuning: values.tuning || null,
      note: values.note || null,
    });
    await refresh();
  });
}

/* ------------------------------------------------------- off the pedal */

async function lookOnDevice() {
  const payload = await api.get("/api/device/audio");
  const host = clear($("#device-audio"));

  if (payload.error) {
    host.append(
      el("div", { class: "notice warn" }, el("div", { text: payload.error }),
        el("div", { class: "hint", text: payload.hint || "" }))
    );
    return;
  }
  if (!payload.files.length) {
    host.append(
      el("div", {
        class: "notice",
        text:
          "No audio on the device's storage. HeadRush has not documented " +
          "where the looper writes, so it may keep loops somewhere this " +
          "cannot see - recording the same part over USB audio always works.",
      })
    );
    return;
  }

  for (const file of payload.files) {
    host.append(
      el(
        "div",
        { class: "item", title: file.path },
        el("span", { class: "title", text: file.name }),
        el("span", { class: "spacer" }),
        el("span", { class: "meta", text: `${Math.round(file.size / 1024)} KB` }),
        el("button", {
          class: "btn small",
          text: "import",
          onclick: () => attempt(() => importOne(file)),
        })
      )
    );
  }
  host.append(
    el("div", {
      class: "hint",
      text:
        "Impulse responses live on here too, so not everything listed is a " +
        "recording of you playing.",
    })
  );
}

async function importOne(file) {
  const values = await prompt(`Import "${file.name}"`, [
    {
      type: "note",
      text:
        "Whatever the pedal recorded has already been through the pedal, so " +
        "this arrives as a loop rather than a DI - reamping through one " +
        "would stack two amps.",
    },
    {
      name: "kind",
      label: "What is it?",
      type: "select",
      value: "loop",
      options: KIND_OPTIONS(),
    },
    { name: "guitar", label: "Which guitar?", placeholder: "Les Paul Standard" },
    { name: "pickup", label: "Pickup", placeholder: "bridge" },
    { name: "note", label: "What did you play?" },
  ]);
  if (!values) return;

  const result = await api.post("/api/device/audio/import", {
    kind: values.kind,
    guitar: values.guitar || null,
    pickup: values.pickup || null,
    note: values.note || null,
    only: [file.name],
  });
  toast(result.rendered.split("\n")[0], { kind: "ok" });
  await refresh();
}

/* ------------------------------------------------------------------- init */

export function init() {
  attachPicker("#take-path", { kind: "audio", title: "Choose a recording" });

  $("#take-add").onclick = () => attempt(addTake);
  $("#device-audio-scan").onclick = () => attempt(lookOnDevice);

  return refresh();
}

export const onShow = refresh;

// Arriving from somewhere that wants a take added - the Match panel, say.
onArrive("takes", ({ path }) => {
  if (path) $("#take-path").value = path;
});
