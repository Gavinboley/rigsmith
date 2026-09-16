/* Recipes: the research, before there is a rig.
 *
 * The confidence marker is the thing this panel exists to make visible. A
 * list of gear tells you what someone decided; a list of gear with its
 * sourcing tells you which parts of that decision to trust, and which to
 * attack first when the rig sounds wrong.
 */

import { api } from "../api.js";
import { $, attempt, clear, el, prompt } from "../ui.js";
import { crossLink, goTo } from "../nav.js";

const CONFIDENCE = [
  { value: "stated", label: "stated - someone who was there said so" },
  { value: "reported", label: "reported - rig rundown, photo, credible catalogue" },
  { value: "inferred", label: "inferred - reasoned from the era or the sound" },
  { value: "guessed", label: "guessed - a plausible starting point" },
];

const MARK = { stated: "***", reported: "** ", inferred: "*  ", guessed: "?  " };
const TONE = { stated: "a2", reported: "a2", inferred: "", guessed: "warn" };

let current = null;

/** What a measurement amounts to, in one line. */
function summarise(item) {
  if (item.kind === "listen") {
    const outcome = item.won ? "chosen" : "not chosen";
    return item.trials ? `${outcome} over ${item.trials} trial(s)` : outcome;
  }
  const parts = [];
  if (item.composite !== null && item.composite !== undefined) {
    parts.push(`composite ${item.composite.toFixed(4)}`);
  }
  if (item.mode) parts.push(item.mode);
  if (item.region) parts.push(item.region);
  if (item.won) parts.push("ranked first");
  return parts.join("  ");
}

/* ----------------------------------------------------------------- render */

function gearItem(choice) {
  const gear = [choice.make, choice.model].filter(Boolean).join(" ");
  const settings = Object.entries(choice.settings || {})
    .map(([key, value]) => `${key} ${value}`)
    .join("  ");

  const node = el(
    "div",
    { class: "item", title: choice.note || "" },
    el("span", {
      class: `tag ${TONE[choice.confidence] ?? ""}`,
      text: choice.confidence,
      title: CONFIDENCE.find((c) => c.value === choice.confidence)?.label || "",
    }),
    el("span", { class: "meta", text: choice.role }),
    el("span", { class: "title", text: choice.name }),
    gear ? el("span", { class: "meta", text: gear }) : null,
    settings ? el("span", { class: "meta", text: settings }) : null,
    el("span", { class: "spacer" })
  );

  if (choice.resolved_model_id) {
    node.append(
      el("span", {
        class: "tag a2",
        text: "resolved",
        title: `-> ${choice.resolved_model_id}`,
      })
    );
  } else {
    node.append(
      el("button", {
        class: "btn small",
        text: "resolve",
        title: "Bind this to a real device model or capture",
        onclick: () => resolveGear(choice),
      })
    );
    // An unresolved amp is usually a capture you have not found yet, so the
    // panel offers to go and look rather than leaving you to remember which
    // tab that was.
    if (["amp", "nam", "clone"].includes(choice.category)) {
      node.append(
        crossLink("find a capture", "captures", { query: gear || choice.name }, {
          title: "Search TONE3000 for this, in the Captures panel",
        })
      );
    }
  }

  if (!choice.sources?.length) {
    node.append(
      el("span", {
        class: "tag warn",
        text: "unsourced",
        title: "Nothing backs this up but a hunch.",
      })
    );
  }
  return node;
}

function render(payload) {
  current = payload;
  $("#recipe-empty").hidden = true;
  $("#recipe-body").hidden = false;

  $("#recipe-title").textContent = payload.title;
  const credit = [payload.artist, payload.album, payload.year]
    .filter(Boolean)
    .join(" - ");
  $("#recipe-credit").textContent = credit;
  $("#recipe-summary").textContent = payload.summary || "";
  $("#recipe-summary").hidden = !payload.summary;

  const confidence = payload.confidence || 0;
  $("#recipe-confidence").textContent = `${Math.round(confidence * 100)}%`;
  $("#recipe-confidence").className =
    "tag " + (confidence > 0.7 ? "a2" : confidence > 0.4 ? "" : "warn");

  const gear = clear($("#recipe-gear"));
  if (!payload.gear?.length) {
    gear.append(el("div", { class: "hint", text: "No gear recorded yet." }));
  } else {
    payload.gear.forEach((choice) => gear.append(gearItem(choice)));
  }

  const scenes = clear($("#recipe-scenes"));
  for (const scene of payload.scenes || []) {
    scenes.append(
      el(
        "div",
        { class: "item" },
        scene.footswitch
          ? el("span", { class: "tag", text: `FS${scene.footswitch}` })
          : null,
        el("span", { class: "title", text: scene.name }),
        el("span", { class: "spacer" }),
        scene.roles_on?.length
          ? el("span", { class: "meta", text: `on ${scene.roles_on.join(", ")}` })
          : null,
        scene.roles_off?.length
          ? el("span", { class: "meta", text: `off ${scene.roles_off.join(", ")}` })
          : null
      )
    );
  }

  const measured = clear($("#recipe-measured"));
  if (!payload.measurements?.length) {
    measured.append(
      el("div", {
        class: "hint",
        text:
          "Nothing measured yet. This recipe has been reasoned about, not " +
          "checked - score it against the record in the Match panel.",
      })
    );
  } else {
    for (const item of payload.measurements) {
      measured.append(
        el(
          "div",
          { class: "item", title: item.at },
          el("span", {
            class: `tag ${item.won ? "a2" : ""}`,
            text: item.kind === "listen" ? "ears" : "metric",
            title:
              item.kind === "listen"
                ? "A blind listening verdict. This is what settles it."
                : "A metric result. A ranking, not a decision.",
          }),
          el("span", { class: "title", text: item.label }),
          el("span", { class: "spacer" }),
          el("span", { class: "meta", text: item.describe || summarise(item) })
        )
      );
    }
  }
  $("#recipe-verified").textContent = payload.verified ? "verified by ear" : "unverified";
  $("#recipe-verified").className = "tag " + (payload.verified ? "a2" : "warn");

  const questions = clear($("#recipe-questions"));
  for (const question of payload.open_questions || []) {
    questions.append(
      el("div", { class: "finding info", text: question })
    );
  }
  if (payload.unsupported?.length) {
    questions.append(
      el("div", {
        class: "finding warning",
        text:
          `Unsourced: ${payload.unsupported.join(", ")} - a guess, not a finding.`,
      })
    );
  }
}

/* ---------------------------------------------------------------- actions */

async function refresh(selectId = null) {
  const { recipes } = await api.get("/api/recipes");
  const select = clear($("#recipe-select"));
  for (const recipe of recipes) {
    select.append(
      el("option", {
        value: recipe.recipe_id,
        text: `${recipe.title} (${recipe.gear} gear, ${Math.round(
          recipe.confidence * 100
        )}%)`,
      })
    );
  }
  if (!recipes.length) {
    $("#recipe-body").hidden = true;
    $("#recipe-empty").hidden = false;
    current = null;
    return;
  }
  const wanted =
    selectId && recipes.some((r) => r.recipe_id === selectId)
      ? selectId
      : recipes[0].recipe_id;
  select.value = wanted;
  render(await api.get(`/api/recipes/${wanted}`));
}

async function newRecipe() {
  const values = await prompt("New tone recipe", [
    { name: "title", label: "Title", placeholder: "Cherub Rock" },
    { name: "artist", label: "Artist", placeholder: "optional" },
    { name: "album", label: "Album", placeholder: "optional" },
    {
      name: "summary",
      label: "What does the tone sound like?",
      placeholder: "The thing no measurement captures",
    },
  ]);
  if (!values?.title) return;
  const created = await attempt(() =>
    api.post("/api/recipes", {
      title: values.title,
      artist: values.artist || null,
      album: values.album || null,
      summary: values.summary || null,
    })
  );
  if (created) await refresh(created.recipe_id);
}

async function addGear() {
  const values = await prompt("Add gear", [
    { name: "role", label: "Role (scenes reference this)", placeholder: "fuzz, amp, cab, delay" },
    { name: "name", label: "What is it?", placeholder: "Big Muff Pi" },
    {
      name: "category",
      label: "Block kind",
      type: "select",
      value: "effect",
      options: [
        { value: "effect", label: "Effect" },
        { value: "amp", label: "Amp" },
        { value: "nam", label: "NAM capture" },
        { value: "cab", label: "Cab" },
        { value: "ir", label: "IR" },
        { value: "clone", label: "Amp clone" },
      ],
    },
    {
      name: "confidence",
      label: "How well supported is this?",
      type: "select",
      value: "guessed",
      options: CONFIDENCE.map((c) => ({ value: c.value, label: c.label })),
    },
    { name: "source_title", label: "Source (required for stated/reported)", placeholder: "Guitar World interview" },
    { name: "source_url", label: "Source URL", placeholder: "optional" },
    { name: "source_note", label: "What does it actually say?", placeholder: "optional but worth it" },
  ]);
  if (!values?.role || !values?.name) return;

  const updated = await attempt(() =>
    api.post(`/api/recipes/${current.recipe_id}/gear`, {
      role: values.role,
      name: values.name,
      category: values.category,
      confidence: values.confidence,
      source_title: values.source_title || null,
      source_url: values.source_url || null,
      source_note: values.source_note || null,
    })
  );
  if (updated) render(updated);
}

async function resolveGear(choice) {
  const values = await prompt(`Resolve "${choice.name}"`, [
    {
      name: "model_id",
      label: "Device model id or capture name",
      placeholder: "82-lead-800-100w",
    },
  ]);
  if (!values?.model_id) return;
  const updated = await attempt(() =>
    api.post(`/api/recipes/${current.recipe_id}/resolve`, {
      role: choice.role,
      model_id: values.model_id,
    })
  );
  if (updated) render(updated);
}

async function addScene() {
  const values = await prompt("Add a part of the song", [
    { name: "name", label: "Name", placeholder: "Clean verse" },
    { name: "on", label: "Gear roles active (comma separated)", placeholder: "fuzz" },
    { name: "off", label: "Gear roles off", placeholder: "delay" },
    { name: "footswitch", label: "Footswitch 1-5", placeholder: "optional" },
  ]);
  if (!values?.name) return;

  const roles = (text) =>
    text.split(",").map((s) => s.trim()).filter(Boolean);

  const updated = await attempt(() =>
    api.post(`/api/recipes/${current.recipe_id}/scenes`, {
      name: values.name,
      roles_on: roles(values.on),
      roles_off: roles(values.off),
      footswitch: values.footswitch ? Number(values.footswitch) : null,
    })
  );
  if (updated) render(updated);
}

async function addQuestion() {
  const values = await prompt("What could you not establish?", [
    {
      name: "question",
      label: "Open question",
      placeholder: "Which Big Muff revision?",
    },
  ]);
  if (!values?.question) return;
  const updated = await attempt(() =>
    api.post(`/api/recipes/${current.recipe_id}/questions`, {
      question: values.question,
    })
  );
  if (updated) render(updated);
}

/* ------------------------------------------------------------------- init */

export function init() {
  $("#new-recipe").onclick = newRecipe;
  $("#new-recipe-empty").onclick = newRecipe;
  $("#add-gear").onclick = addGear;
  $("#add-recipe-scene").onclick = addScene;
  $("#add-question").onclick = addQuestion;

  $("#recipe-select").onchange = (event) =>
    attempt(() => api.get(`/api/recipes/${event.target.value}`).then(render));

  $("#compile-recipe").onclick = () =>
    attempt(async () => {
      const result = await api.post(
        `/api/recipes/${current.recipe_id}/compile`
      );
      const out = clear($("#compile-out"));
      out.append(el("pre", { class: "out", text: result.rendered }));
      if (result.draft_id) {
        out.append(
          el("button", {
            class: "btn primary",
            text: "Open in the rig builder",
            onclick: () => goTo("rig", { draftId: result.draft_id }),
          })
        );
      }
    });

  $("#delete-recipe").onclick = async () => {
    if (!current) return;
    if (!window.confirm(`Delete recipe "${current.title}"?`)) return;
    await attempt(async () => {
      await api.del(`/api/recipes/${current.recipe_id}`);
      await refresh();
    });
  };

  return refresh();
}

export const onShow = () => refresh(current?.recipe_id ?? null);
