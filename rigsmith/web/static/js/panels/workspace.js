/* Workspace panel: what is on disk, and the jobs that put it there. */

import { api } from "../api.js";
import { $, attempt, bytes, clear, el } from "../ui.js";

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
      : el("span", {
          class: "tag warn",
          text: "protected",
          title: "Never pruned unless named explicitly.",
        }),
    el("span", { class: "meta", text: area.human_size })
  );
}

function jobItem(job) {
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
  if (!job.finished) {
    node.append(
      el("button", {
        class: "btn small",
        text: "cancel",
        onclick: () => attempt(() => api.cancelJob(job.id).then(refresh)),
      })
    );
  }
  return node;
}

export async function refreshJobs(jobs = null) {
  const list = jobs ?? (await api.jobs().then((r) => r.jobs).catch(() => []));
  const host = clear($("#jobs-list"));
  if (!list.length) {
    host.append(el("div", { class: "hint", text: "No background jobs." }));
    return;
  }
  for (const job of list) host.append(jobItem(job));
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
