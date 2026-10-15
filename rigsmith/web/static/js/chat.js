/* The assistant, beside whatever panel is open.
 *
 * Everything the app can do is a tool; this is a conversation in front of
 * them. The reply streams in over the live socket as it happens - the
 * model's words, each tool it calls and what came back - so a slow answer
 * looks like work, not like a hang. Anything that would change the device or
 * delete something stops and asks here, with the exact call shown.
 */

import { api } from "./api.js";
import { $, attempt, clear, el, toast } from "./ui.js";

let chatId = null;
let roles = [];
let busy = false;
const pendingCards = new Map();

const SUGGESTIONS = [
  ["Map a song", "I want to chase a song's guitar tone. Help me add it and map its parts."],
  ["Teach it my guitar", "Walk me through teaching rigsmith my guitar."],
  ["Set up the loop", "Help me set up the hardware loop between my interface and the Core."],
  ["What next?", "Look at what is in my workspace and tell me the most useful next step."],
];

export function isOpen() {
  return !$("#chat-drawer").hidden;
}

export function open(prefill = null) {
  $("#chat-drawer").hidden = false;
  if (prefill) {
    $("#chat-text").value = prefill;
  }
  $("#chat-text").focus();
}

export function close() {
  $("#chat-drawer").hidden = true;
}

export function toggle() {
  if (isOpen()) close();
  else open();
}

function argumentsText(args) {
  const text = JSON.stringify(args || {});
  return text === "{}" ? "" : text.length > 140 ? text.slice(0, 137) + "..." : text;
}

function log() {
  return $("#chat-log");
}

function scrollDown() {
  const node = log();
  node.scrollTop = node.scrollHeight;
}

function addMessage(kind, text) {
  const node = el("div", { class: `msg ${kind}`, text });
  log().append(node);
  scrollDown();
  return node;
}

function toolCard(entry, { pending = false } = {}) {
  const card = el(
    "details",
    { class: `toolcard ${pending ? "pending" : ""}` },
    el(
      "summary",
      {},
      el("span", { class: "tool-name", text: entry.name }),
      el("span", { class: "tool-args", text: argumentsText(entry.arguments) }),
      el("span", { class: "hint tool-state", text: pending ? "running" : "" })
    )
  );
  log().append(card);
  scrollDown();
  return card;
}

function finishCard(card, entry) {
  card.classList.remove("pending");
  if (!entry.ok) card.classList.add("failed");
  card.querySelector(".tool-state").textContent = entry.ok ? "ok" : entry.declined ? "declined" : "failed";
  card.append(el("pre", { text: entry.text || "" }));
}

function renderEntry(entry) {
  if (entry.role === "user") return addMessage("user", entry.text);
  switch (entry.kind) {
    case "text":
      return addMessage("assistant", entry.text);
    case "final":
      // Claude Code repeats its last text as the final result.
      if (!log().lastElementChild || log().lastElementChild.textContent !== entry.text) addMessage("assistant", entry.text);
      return null;
    case "reasoning":
      return addMessage("reasoning", entry.text.length > 600 ? entry.text.slice(0, 600) + "..." : entry.text);
    case "tool_call": {
      const card = toolCard(entry, { pending: true });
      if (entry.id) pendingCards.set(entry.id, card);
      return card;
    }
    case "tool_result": {
      const card = (entry.id && pendingCards.get(entry.id)) || toolCard(entry);
      pendingCards.delete(entry.id);
      finishCard(card, entry);
      return card;
    }
    case "error":
      return addMessage("error", entry.text);
    case "status":
      return null;
    default:
      return null;
  }
}

function renderEmpty() {
  const box = el(
    "div",
    { class: "chat-empty" },
    el("div", { text: "Ask for anything rigsmith can do. It works with the same tools as every panel." }),
    el("div", {}, ...SUGGESTIONS.map(([label, text]) => el("button", { class: "btn small", text: label, onclick: () => send(text) })))
  );
  log().append(box);
}

async function loadChat(id) {
  chatId = id;
  pendingCards.clear();
  clear(log());
  if (!id) {
    renderEmpty();
    return;
  }
  const chat = await api.get(`/api/chat/${id}`);
  $("#chat-writes").checked = chat.allow_writes;
  $("#chat-role").value = chat.role;
  if (!chat.transcript.length) renderEmpty();
  for (const entry of chat.transcript) renderEntry(entry);
  setBusy(chat.busy);
  await refreshApprovals();
}

async function refreshChats(select = null) {
  const { chats } = await api.get("/api/chat");
  const picker = clear($("#chat-select"));
  picker.append(el("option", { value: "", text: chats.length ? "New conversation" : "No conversations yet" }));
  for (const chat of chats) {
    picker.append(el("option", { value: chat.chat_id, text: `${chat.title} (${chat.role})` }));
  }
  const wanted = select ?? chatId;
  picker.value = wanted && chats.some((c) => c.chat_id === wanted) ? wanted : "";
  return chats;
}

async function refreshProvider() {
  try {
    const [status, settings] = await Promise.all([api.get("/api/chat/providers"), api.get("/api/settings")]);
    const local = settings.mode === "local";
    let text;
    if (local) {
      text = status.local.reachable
        ? `local model at ${status.local.base_url} (${(status.local.models || [])[0] || "auto"})`
        : `local mode - no model server at ${status.local.base_url}. Start LM Studio or Ollama.`;
    } else if (settings.llm.provider === "openai-compatible") {
      text = `model at ${settings.llm.base_url}`;
    } else if (status.claude_code && settings.llm.provider !== "anthropic") {
      text = "Claude, through Claude Code";
    } else if (status.anthropic_sdk && status.anthropic_key) {
      text = "Claude, through the Anthropic API";
    } else {
      text = status.local.reachable ? "a local model (Claude is not set up)" : "no model available - see Settings";
    }
    $("#chat-provider").textContent = text;
  } catch {
    /* the pill is a nicety */
  }
}

function setBusy(value) {
  busy = value;
  $("#chat-stop").hidden = !value;
  $("#chat-send").disabled = value;
}

async function ensureChat() {
  if (chatId) return chatId;
  const chat = await api.post("/api/chat", { role: $("#chat-role").value || "assistant", allow_writes: $("#chat-writes").checked });
  chatId = chat.chat_id;
  await refreshChats(chatId);
  return chatId;
}

export async function send(text) {
  text = (text ?? $("#chat-text").value).trim();
  if (!text || busy) return;
  open();
  await attempt(async () => {
    const id = await ensureChat();
    if (log().querySelector(".chat-empty")) clear(log());
    addMessage("user", text);
    $("#chat-text").value = "";
    setBusy(true);
    await api.post(`/api/chat/${id}/messages`, { text });
  });
}

async function refreshApprovals() {
  const host = clear($("#chat-approvals"));
  if (!chatId) return;
  const { approvals } = await api.get("/api/chat/approvals", { chat_id: chatId });
  for (const approval of approvals) host.append(approvalCard(approval));
}

function approvalCard(approval) {
  const node = el(
    "div",
    { class: "approval", "data-id": approval.approval_id },
    el("div", {}, el("strong", { text: "Allow " }), el("span", { class: "mono", text: approval.tool }), el("span", { text: "?" })),
    el("div", { class: "mono hint", text: argumentsText(approval.arguments) }),
    el(
      "div",
      { class: "row" },
      el("button", { class: "btn primary small", text: "Allow", onclick: () => decide(approval.approval_id, true, node) }),
      el("button", { class: "btn small", text: "Decline", onclick: () => decide(approval.approval_id, false, node) })
    )
  );
  return node;
}

async function decide(id, allow, node) {
  await attempt(() => api.post(`/api/chat/approvals/${id}`, { allow }));
  node.remove();
}

/** Live events from app.js's socket. */
export function onEvent(message) {
  if (message.type === "agent") {
    if (message.kind === "started" && message.parent === chatId) {
      addMessage("status", `${message.role} started: ${message.task.slice(0, 120)}`);
    } else if (message.kind === "finished" && message.parent === chatId) {
      addMessage("status", `${message.role} ${message.state}`);
    }
    return;
  }
  if (message.type !== "chat" || message.chat_id !== chatId) return;
  if (message.kind === "approval_request") {
    open();
    $("#chat-approvals").append(approvalCard(message));
    toast(`The assistant wants to run ${message.tool} - approve it in the chat`, { kind: "warn" });
    return;
  }
  if (message.kind === "approval_decided") {
    $(`#chat-approvals .approval[data-id='${message.approval_id}']`)?.remove();
    return;
  }
  if (message.kind === "done") {
    setBusy(false);
    refreshChats();
    return;
  }
  if (message.kind === "status") {
    $("#chat-provider").dataset.status = message.text;
    return;
  }
  renderEntry(message);
  if (message.kind === "error") setBusy(false);
}

export async function init() {
  const roleData = await api.get("/api/chat/roles").catch(() => ({ roles: [] }));
  roles = roleData.roles;
  const roleSelect = clear($("#chat-role"));
  for (const role of roles) {
    roleSelect.append(el("option", { value: role.name, text: role.name, title: role.description }));
  }

  $("#chat-toggle").onclick = () => {
    toggle();
    if (isOpen()) refreshProvider();
  };
  $("#chat-close").onclick = close;
  $("#chat-new").onclick = () => {
    chatId = null;
    $("#chat-select").value = "";
    loadChat(null);
  };
  $("#chat-select").onchange = (event) => loadChat(event.target.value || null);
  $("#chat-role").onchange = () => {
    // A role is fixed for a conversation; changing it starts a new one.
    if (chatId) {
      chatId = null;
      $("#chat-select").value = "";
      loadChat(null);
    }
  };
  $("#chat-writes").onchange = (event) => {
    if (chatId) attempt(() => api.patch(`/api/chat/${chatId}`, { allow_writes: event.target.checked }));
  };
  $("#chat-stop").onclick = () => chatId && attempt(() => api.post(`/api/chat/${chatId}/stop`));
  $("#chat-form").onsubmit = (event) => {
    event.preventDefault();
    send();
  };
  $("#chat-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      toggle();
      if (isOpen()) refreshProvider();
    } else if (event.key === "Escape" && isOpen() && document.activeElement?.closest("#chat-drawer")) {
      close();
    }
  });
  // Any panel can hand the assistant a question with context.
  document.addEventListener("rigsmith:ask", (event) => {
    open(event.detail?.text || "");
    if (event.detail?.send) send(event.detail.text);
  });

  const chats = await refreshChats();
  await loadChat(chats[0]?.chat_id || null);
}

/** Ask the assistant from anywhere: `ask("tune this against the solo", {send: true})`. */
export function ask(text, { send: now = false } = {}) {
  document.dispatchEvent(new CustomEvent("rigsmith:ask", { detail: { text, send: now } }));
}
