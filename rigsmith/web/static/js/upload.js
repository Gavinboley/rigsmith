/* Getting a file into the workspace: click to pick, or drop it.
 *
 * The server's file picker (picker.js) browses what is already on disk; this
 * is for a file that lives on the machine running the browser, which for a
 * local tool is usually the same machine - but dropping a song from a music
 * folder is still quicker than navigating to it.
 */

import { ApiError } from "./api.js";
import { el } from "./ui.js";

export async function uploadFile(file) {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch("/api/upload", { method: "POST", body: form });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(payload.error || `upload failed (${response.status})`, { hint: payload.hint });
  }
  return payload;
}

/**
 * A drop zone that uploads what it is given and reports the server path.
 * `pick` is an optional async function returning a path already on disk.
 */
export function dropZone({ label, accept = "", onPath, pick = null }) {
  const input = el("input", { type: "file", accept, hidden: true });
  const shown = el("div", { class: "picked" });
  const zone = el("div", { class: "dropzone", tabindex: "0" }, el("div", { text: label }), shown, input);

  const take = async (file) => {
    if (!file) return;
    shown.textContent = `uploading ${file.name}...`;
    try {
      const result = await uploadFile(file);
      shown.textContent = result.name;
      zone.dataset.path = result.path;
      onPath?.(result.path, result);
    } catch (error) {
      shown.textContent = error.message;
    }
  };

  zone.addEventListener("click", async (event) => {
    if (event.shiftKey && pick) {
      const path = await pick();
      if (path) {
        shown.textContent = path;
        zone.dataset.path = path;
        onPath?.(path, { path });
      }
      return;
    }
    input.click();
  });
  input.addEventListener("change", () => take(input.files[0]));
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("over");
    take(event.dataTransfer.files[0]);
  });
  zone.setPath = (path) => {
    shown.textContent = path ? path.split(/[\\/]/).pop() : "";
    if (path) zone.dataset.path = path;
    else delete zone.dataset.path;
  };
  zone.path = () => zone.dataset.path || null;
  return zone;
}
