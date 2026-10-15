/* Band by band, where a result still differs from the record - shared by
 * autopilot runs and tuning runs.
 *
 * A bar right of centre is too much, left is not enough; amber is audible
 * (1.5 dB or more), and each audible band names the knob to try first.
 */

import { el } from "./ui.js";

export function remainingCard(rows) {
  const span = 12;
  const box = el(
    "div",
    { class: "remaining" },
    el("div", { class: "scenes-head" }, el("h3", { text: "Still different from the record" }), el("span", { class: "hint", text: "right of centre: more than the record; left: less. Differences under 1.5 dB are hard to hear." }))
  );
  for (const row of rows) {
    const width = Math.min(Math.abs(row.delta_db), span) / span * 50;
    const bar = el("div", { class: `rem-bar ${row.audible ? "audible" : ""}`, style: row.delta_db >= 0 ? `left:50%;width:${width}%` : `left:${50 - width}%;width:${width}%` });
    box.append(
      el(
        "div",
        { class: "rem-row", title: row.meaning },
        el("span", { class: "rem-band", text: row.band }),
        el("div", { class: "rem-track" }, el("div", { class: "rem-mid" }), bar),
        el("span", { class: "rem-db mono", text: `${row.delta_db > 0 ? "+" : ""}${row.delta_db.toFixed(1)}` }),
        el("span", { class: "hint rem-try", text: row.audible ? `try ${row.try}` : "" })
      )
    );
  }
  return box;
}

