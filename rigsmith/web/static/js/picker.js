/* A file picker, because a browser will not give the page a real path.
 *
 * `<input type=file>` deliberately withholds the absolute path, and an
 * absolute path is exactly what every input in this tool needs - the server
 * is the thing that opens the file. So the picker asks the server what is in
 * a directory and hands back the path the user chose.
 *
 * It lists names and types only. Reading a file still goes through the audio
 * route and its allowance, so browsing to something is not the same as
 * having opened it.
 */

import { api } from "./api.js";
import { $, clear, el, toast } from "./ui.js";

/** Remember where each kind of pick was last taken from. */
const lastDirectory = {};

/**
 * @param {object} options
 * @param {string} options.kind    audio | capture | ir | any
 * @param {boolean} options.folder pick the directory itself, not a file in it
 * @param {string} options.start   where to open
 * @returns {Promise<string|null>} the chosen path, or null if cancelled
 */
export function pickPath({
  kind = "audio",
  folder = false,
  start = null,
  title = null,
} = {}) {
  return new Promise((resolve) => {
    const dialog = el("dialog", { class: "modal picker" });
    const heading = el("h3", { text: title || (folder ? "Choose a folder" : "Choose a file") });
    const here = el("div", { class: "picker-path mono" });
    const list = el("div", { class: "picker-list" });
    const roots = el("div", { class: "picker-roots" });

    let current = null;
    let chosen = null;

    const finish = (value) => {
      dialog.close();
      dialog.remove();
      resolve(value);
    };

    async function show(path) {
      let payload;
      try {
        payload = await api.get("/api/browse", { path, kind });
      } catch (error) {
        toast(error.message, { kind: "error", hint: error.hint });
        return;
      }

      current = payload.path;
      lastDirectory[kind] = current;
      here.textContent = current;
      chosen = folder ? current : null;
      ok.disabled = !chosen;

      clear(roots);
      for (const root of payload.roots) {
        roots.append(
          el("button", {
            class: "btn small ghost picker-root",
            text: root.name,
            title: root.path,
            onclick: () => show(root.path),
          })
        );
      }

      clear(list);
      if (payload.parent) {
        list.append(
          el("div", {
            class: "picker-row up",
            text: "..",
            onclick: () => show(payload.parent),
          })
        );
      }
      for (const directory of payload.directories) {
        list.append(
          el("div", {
            class: "picker-row dir",
            text: directory.name,
            onclick: () => show(directory.path),
          })
        );
      }
      if (!folder) {
        for (const file of payload.files) {
          const row = el(
            "div",
            { class: "picker-row file" },
            el("span", { class: "title", text: file.name }),
            el("span", { class: "spacer" }),
            el("span", { class: "meta", text: `${Math.round(file.size / 1024)} KB` })
          );
          row.onclick = () => {
            chosen = file.path;
            ok.disabled = false;
            for (const other of list.querySelectorAll(".picker-row")) {
              other.classList.toggle("chosen", other === row);
            }
          };
          row.ondblclick = () => finish(file.path);
          list.append(row);
        }
      }
      if (!payload.directories.length && !payload.files.length) {
        list.append(
          el("div", {
            class: "hint",
            text: folder ? "No subfolders here." : `Nothing of that kind here.`,
          })
        );
      }
    }

    const ok = el("button", {
      class: "btn primary picker-ok",
      text: folder ? "Use this folder" : "Choose",
      disabled: true,
      onclick: (event) => {
        event.preventDefault();
        if (chosen) finish(chosen);
      },
    });

    dialog.append(
      heading,
      roots,
      here,
      list,
      el(
        "menu",
        { class: "modal-actions" },
        el("button", {
          class: "btn ghost picker-cancel",
          text: "Cancel",
          onclick: (event) => {
            event.preventDefault();
            finish(null);
          },
        }),
        ok
      )
    );

    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      finish(null);
    });

    document.body.append(dialog);
    dialog.showModal();
    show(start || lastDirectory[kind] || null);
  });
}

/**
 * Put a Browse button next to a text input, so a path can be picked or typed.
 * Typing still works - an agent or a script pasting a path should not have to
 * go through a dialog.
 */
export function attachPicker(inputSelector, options = {}) {
  const input = $(inputSelector);
  if (!input || input.dataset.picker) return;
  input.dataset.picker = "1";

  const button = el("button", {
    class: "btn small ghost",
    text: "Browse",
    title: options.folder ? "Choose a folder" : "Choose a file",
    onclick: async () => {
      const picked = await pickPath({ ...options, start: input.value.trim() || null });
      if (picked === null) return;
      input.value = picked;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      options.onPick?.(picked);
    },
  });
  input.after(button);
  return button;
}
