/* Thin fetch wrapper.
 *
 * The server returns core errors as {error, hint} with a meaningful status,
 * so this raises a single ApiError carrying both. Every caller then gets the
 * hint for free instead of each one inventing its own failure message.
 */

export class ApiError extends Error {
  constructor(message, { hint = null, status = 0 } = {}) {
    super(message);
    this.name = "ApiError";
    this.hint = hint;
    this.status = status;
  }
}

async function request(method, path, body) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    throw new ApiError("Could not reach the rigsmith server", {
      hint: "Is it still running? Check the terminal you started it from.",
    });
  }

  let payload = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("json")) {
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const message =
      payload?.error ||
      payload?.detail ||
      `${response.status} ${response.statusText}`;
    throw new ApiError(
      typeof message === "string" ? message : JSON.stringify(message),
      { hint: payload?.hint, status: response.status }
    );
  }
  return payload;
}

const query = (params) => {
  const usable = Object.entries(params || {}).filter(
    ([, v]) => v !== undefined && v !== null && v !== ""
  );
  return usable.length
    ? "?" + new URLSearchParams(Object.fromEntries(usable)).toString()
    : "";
};

export const api = {
  get: (path, params) => request("GET", path + query(params)),
  post: (path, body, params) => request("POST", path + query(params), body ?? {}),
  patch: (path, body) => request("PATCH", path, body ?? {}),
  del: (path) => request("DELETE", path),

  health: () => api.get("/api/health"),
  capabilities: () => api.get("/api/capabilities"),

  drafts: () => api.get("/api/drafts"),
  draft: (id) => api.get(`/api/drafts/${id}`),
  createDraft: (body) => api.post("/api/drafts", body),
  deleteDraft: (id) => api.del(`/api/drafts/${id}`),
  duplicateDraft: (id) => api.post(`/api/drafts/${id}/duplicate`),
  updateRig: (id, body) => api.patch(`/api/drafts/${id}`, body),

  addBlock: (id, body) => api.post(`/api/drafts/${id}/blocks`, body),
  removeBlock: (id, slot) => api.del(`/api/drafts/${id}/blocks/${slot}`),
  moveBlock: (id, slot, to) =>
    api.post(`/api/drafts/${id}/blocks/${slot}/move`, { to_slot: to }),
  patchBlock: (id, slot, body) =>
    api.patch(`/api/drafts/${id}/blocks/${slot}`, body),

  setScene: (id, body) => api.post(`/api/drafts/${id}/scenes`, body),
  deleteScene: (id, name) =>
    api.del(`/api/drafts/${id}/scenes/${encodeURIComponent(name)}`),

  preview: (id) => api.get(`/api/drafts/${id}/preview`),
  exportDraft: (id) => api.post(`/api/drafts/${id}/export`),
  writeDraft: (id, confirm) =>
    api.post(`/api/drafts/${id}/write`, {}, { confirm }),

  catalogSearch: (params) => api.get("/api/catalog/search", params),

  discover: (sweep) => api.get("/api/device/discover", { sweep }),
  storage: () => api.get("/api/device/storage"),
  backup: () => api.post("/api/device/backup"),
  midiPorts: () => api.get("/api/device/midi"),
  midiSelect: (body) => api.post("/api/device/midi/select", body),
  midiFootswitch: (body) => api.post("/api/device/midi/footswitch", body),
  midiBlock: (body) => api.post("/api/device/midi/block", body),
  midiTap: (body) => api.post("/api/device/midi/tap", body),
  midiVerify: (body) => api.post("/api/device/midi/verify", body),

  scanCaptures: (directory) => api.get("/api/captures/scan", { directory }),
  tone3000Status: () => api.get("/api/tone3000/status"),
  tone3000Login: () => api.post("/api/tone3000/login"),
  tone3000Search: (params) => api.get("/api/tone3000/search", params),
  tone3000Download: (body) => api.post("/api/tone3000/download", body),

  matchDevices: () => api.get("/api/match/devices"),
  score: (body) => api.post("/api/match/score", body),
  spectrum: (path, region) =>
    api.get("/api/match/spectrum", {
      path,
      start: region ? region[0] : undefined,
      end: region ? region[1] : undefined,
    }),
  openAudio: (path) => api.post("/api/match/open", { path }),
  isolate: (body) => api.post("/api/match/isolate", body),
  renderOffline: (body) => api.post("/api/match/render", body),
  offlineSearch: (body) => api.post("/api/match/search", body),
  captureDi: (body) => api.post("/api/match/capture-di", body),
  reamp: (body) => api.post("/api/match/reamp", body),

  abSessions: () => api.get("/api/ab"),
  createAb: (body) => api.post("/api/ab", body),
  abSession: (id) => api.get(`/api/ab/${id}`),
  abVerdict: (id, body) => api.post(`/api/ab/${id}/verdict`, body),
  abResults: (id) => api.get(`/api/ab/${id}/results`),

  workspace: () => api.get("/api/workspace"),
  prune: (body) => api.post("/api/workspace/prune", body),

  jobs: () => api.get("/api/jobs"),
  job: (id) => api.get(`/api/jobs/${id}`),
  cancelJob: (id) => api.post(`/api/jobs/${id}/cancel`),
};
