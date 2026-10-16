/* Workspace panel: what is on disk, and the jobs that put it there. */

import { api } from "../api.js";
import { $, attempt, bytes, clear, el, jobRow } from "../ui.js";

function areaItem(area) {
  return el(
    "div",
    { class: "item", title: area.description },
    el("span", { class: "title", text: area.name }),
    el("span", {
      class: "meta",
      text: area.exists ? `${area.file_count} files` : "not created yet",
    }),
    el("span", { class: "spacer" }),
    area.prunable
      ? null
      : area.kept
        ? el("span", {
            class: "tag a2",
            text: "never pruned",
            title: "What is here cannot be made again - it is deleted one item at a time, never swept.",
          })
        : el("span", {
            class: "tag warn",
            text: "protected",
            title: "Left out of a prune unless named explicitly.",
          }),
    el("span", { class: "meta", text: area.human_size })
  );
}

export async function refreshJobs(jobs = null) {
  const list = jobs ?? (await api.jobs().then((r) => r.jobs).catch(() => []));
  const host = clear($("#jobs-list"));
  if (!list.length) {
    host.append(el("div", { class: "hint", text: "No background jobs." }));
    return;
  }
  for (const job of list) {
    host.append(
      jobRow(job, {
        onCancel: () => attempt(() => api.cancelJob(job.id).then(() => refreshJobs())),
      })
    );
  }
}

async function refresh() {
  const report = await attempt(() => api.workspace());
  if (!report) return;

  const host = clear($("#workspace-areas"));
  host.append(
    el("div", {
      class: "hint",
      text: `${report.root} - ${report.file_count} files, ${report.human_size}`,
    })
  );
  for (const area of report.areas) host.append(areaItem(area));
  await refreshJobs();
}

export function init() {
  $("#prune-dry").onclick = () =>
    attempt(async () => {
      const result = await api.prune({ apply: false });
      $("#prune-out").textContent =
        result.count === 0
          ? "Nothing to prune."
          : `Would remove ${result.count} item(s), ${bytes(result.freed_bytes)}:\n` +
            result.removed
              .map((r) => `  ${r.area}  ${r.path}  (${bytes(r.bytes)})`)
              .join("\n");
    });

  $("#prune-apply").onclick = () =>
    attempt(async () => {
      const dry = await api.prune({ apply: false });
      if (!dry.count) {
        $("#prune-out").textContent = "Nothing to prune.";
        return;
      }
      if (
        !window.confirm(
          `Delete ${dry.count} item(s), freeing ${bytes(dry.freed_bytes)}? ` +
            "Backups and drafts are not included."
        )
      ) {
        return;
      }
      const result = await api.prune({ apply: true });
      $("#prune-out").textContent = `Removed ${result.count} item(s), ${bytes(
        result.freed_bytes
      )}.`;
      await refresh();
    });

  return refresh();
}

export const onShow = refresh;
