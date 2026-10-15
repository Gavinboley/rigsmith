/* Waiting for a background job without writing the same loop six times.
 *
 * Slow work comes back as a job id. `track(id)` returns a promise for the
 * job's result and calls `onUpdate` with each state the live socket reports,
 * falling back to polling if the socket is not delivering - a panel that
 * hangs forever because one message was missed is worse than one extra
 * request a second.
 */

import { api } from "./api.js";

const waiting = new Map();

/** Called by app.js with every jobs snapshot from the socket. */
export function jobsTick(jobs) {
  for (const job of jobs) {
    const entry = waiting.get(job.id);
    if (!entry) continue;
    entry.onUpdate?.(job);
    if (job.finished) settle(job.id);
  }
}

async function settle(id) {
  const entry = waiting.get(id);
  if (!entry || entry.settling) return;
  entry.settling = true;
  clearInterval(entry.poll);
  waiting.delete(id);
  try {
    const job = await api.job(id);
    entry.onUpdate?.(job);
    if (job.state === "succeeded") entry.resolve(job.result);
    else {
      const error = new Error(job.error || `job ${job.state}`);
      error.hint = job.state === "cancelled" ? "It was cancelled." : null;
      entry.reject(error);
    }
  } catch (error) {
    entry.reject(error);
  }
}

export function track(id, { onUpdate } = {}) {
  return new Promise((resolve, reject) => {
    const entry = { resolve, reject, onUpdate, settling: false };
    entry.poll = setInterval(async () => {
      try {
        const job = await api.job(id);
        onUpdate?.(job);
        if (job.finished) settle(id);
      } catch {
        /* the next tick, or the socket, will catch up */
      }
    }, 1500);
    waiting.set(id, entry);
  });
}

/** Start a job with a POST, then wait for it. */
export async function runJob(path, body, options = {}) {
  const started = await api.post(path, body);
  return track(started.job_id, options);
}

/** A one-line progress readout for a job, as text. */
export function describeJob(job) {
  if (!job) return "";
  const percent = job.progress === null || job.progress === undefined ? "" : ` ${Math.round(job.progress * 100)}%`;
  return `${job.state}${percent}${job.message ? " - " + job.message : ""}`;
}
