/* Small DOM helpers. No framework, on purpose - five panels do not need a
 * component model, and a build step would put a Node toolchain between
 * `pip install rigsmith` and changing a button.
 */

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

/** Create an element. Children may be nodes or strings. */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.firstChild.remove();
  return node;
}

/* ------------------------------------------------------------------ toasts */

export function toast(message, { kind = "", hint = null, timeout = 6000 } = {}) {
  const host = $("#toasts");
  const node = el(
    "div",
    { class: `toast ${kind}` },
    el("div", { text: message }),
    hint ? el("span", { class: "hint", text: hint }) : null
  );
  host.append(node);
  setTimeout(() => {
    node.style.opacity = "0";
    setTimeout(() => node.remove(), 200);
  }, timeout);
  return node;
}

/** Run an async action, reporting failure as a toast rather than a console trace. */
export async function attempt(action, { success = null } = {}) {
  try {
    const result = await action();
    if (success) toast(success, { kind: "ok", timeout: 3000 });
    return result;
  } catch (error) {
    toast(error.message || String(error), {
      kind: "error",
      hint: error.hint,
      timeout: 9000,
    });
    return undefined;
  }
}

/* ------------------------------------------------------------------- modal */

/** Show a small form. `fields` is [{name, label, type, value, options}]. */
export function prompt(title, fields, { okLabel = "OK" } = {}) {
  const dialog = $("#modal");
  const body = clear($("#modal-body"));
  $("#modal-title").textContent = title;
  $("#modal-ok").textContent = okLabel;

  for (const field of fields) {
    // A note is not an input - it is the context somebody needs to answer
    // the question, and a dialog that asks without showing it is a trap.
    if (field.type === "note") {
      body.append(el("div", { class: field.class || "hint", text: field.text }));
      continue;
    }
    if (field.type === "pre") {
      body.append(el("pre", { class: "out", text: field.text }));
      continue;
    }

    body.append(el("label", { text: field.label, for: `f-${field.name}` }));
    if (field.type === "checkbox") {
      body.append(
        el("input", {
          id: `f-${field.name}`,
          name: field.name,
          type: "checkbox",
          checked: Boolean(field.value),
        })
      );
    } else if (field.type === "select") {
      body.append(
        el(
          "select",
          { id: `f-${field.name}`, name: field.name },
          ...field.options.map((option) =>
            el("option", {
              value: option.value,
              selected: option.value === field.value,
              text: option.label,
            })
          )
        )
      );
    } else {
      body.append(
        el("input", {
          id: `f-${field.name}`,
          name: field.name,
          type: field.type || "text",
          value: field.value ?? "",
          placeholder: field.placeholder ?? "",
        })
      );
    }
  }

  dialog.showModal();
  const first = body.querySelector("input, select");
  if (first) first.focus();

  return new Promise((resolve) => {
    dialog.addEventListener(
      "close",
      () => {
        if (dialog.returnValue !== "ok") return resolve(null);
        const values = {};
        for (const field of fields) {
          if (field.type === "note" || field.type === "pre") continue;
          const node = body.querySelector(`#f-${field.name}`);
          values[field.name] =
            field.type === "checkbox" ? node.checked : node.value;
        }
        resolve(values);
      },
      { once: true }
    );
  });
}

export async function confirmAction(message) {
  const answer = await prompt(message, [], { okLabel: "Yes" });
  return answer !== null;
}

/* ----------------------------------------------------------------- format */

export const pct = (value) =>
  value === null || value === undefined ? "--" : `${Math.round(value * 100)}%`;

export function bytes(count) {
  if (!count) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let size = count;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${index === 0 ? size : size.toFixed(1)} ${units[index]}`;
}

export function setPill(id, text, kind = "") {
  const pill = document.getElementById(id);
  if (!pill) return;
  pill.textContent = text;
  pill.className = `pill ${kind}`;
  pill.hidden = false;
}

/* One row for one background job.
 *
 * Shared so the Workspace list and an in-panel progress strip cannot drift
 * into describing the same job two different ways - which is how a user ends
 * up unsure whether they are looking at one job or two.
 */
export function jobRow(job, { onCancel = null } = {}) {
  const node = el(
    "div",
    { class: "item" },
    el("span", {
      class: `tag ${
        job.state === "succeeded" ? "a2" : job.state === "failed" ? "bad" : "warn"
      }`,
      text: job.state,
    }),
    el("span", { class: "title", text: job.name })
  );

  if (!job.finished && job.progress !== null && job.progress !== undefined) {
    node.append(
      el("span", { class: "bar" }, el("span", { style: `width:${job.progress * 100}%` }))
    );
  }

  node.append(
    el("span", { class: "spacer" }),
    el("span", { class: "meta", text: job.message || "" }),
    el("span", { class: "meta", text: `${job.elapsed}s` })
  );

  if (job.error) {
    node.append(el("span", { class: "tag bad", text: job.error, title: job.error }));
  }
  if (!job.finished && onCancel) {
    node.append(
      el("button", { class: "btn small", text: "cancel", onclick: onCancel })
    );
  }
  return node;
}
