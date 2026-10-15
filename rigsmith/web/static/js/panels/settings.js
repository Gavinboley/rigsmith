/* Settings: which mode, which model, which defaults. */

import { api } from "../api.js";
import { $, attempt, clear, el, toast } from "../ui.js";

const PRESETS = [
  ["LM Studio", "http://127.0.0.1:1234/v1"],
  ["Ollama", "http://127.0.0.1:11434/v1"],
  ["llama.cpp", "http://127.0.0.1:8080/v1"],
];

function field(label, input, hint = null) {
  return el("label", { class: "field" }, el("span", { text: label }), input, hint ? el("span", { class: "hint", text: hint }) : null);
}

function modeCard(mode, title, text, current, onPick) {
  return el(
    "div",
    { class: `route-card ${current === mode ? "default" : ""}`, style: "cursor:pointer;flex:1", onclick: () => onPick(mode) },
    el("div", { class: "r-title" }, el("span", { text: title }), current === mode ? el("span", { class: "tag a2", text: "on" }) : null),
    el("div", { class: "hint", text })
  );
}

async function render() {
  const host = clear($("#settings-body"));
  const [settings, status, profiles, routes] = await Promise.all([
    api.get("/api/settings"),
    api.get("/api/chat/providers").catch(() => null),
    api.get("/api/learn/profiles").catch(() => ({ profiles: [] })),
    api.get("/api/loop/routes").catch(() => ({ routes: [] })),
  ]);

  const pickMode = (mode) => attempt(async () => {
    await api.patch("/api/settings", { mode });
    toast(`${mode} mode`, { kind: "ok" });
    document.dispatchEvent(new CustomEvent("rigsmith:settings"));
    render();
  });
  host.append(
    el(
      "div",
      { class: "card" },
      el("h3", { text: "Mode" }),
      el(
        "div",
        { class: "row", style: "align-items:stretch" },
        modeCard("accuracy", "Accuracy", "The best answer available: Claude (through Claude Code or the API) for research and agents, the network for song lookups and TONE3000.", settings.mode, pickMode),
        modeCard("local", "Local", "Nothing leaves this machine. Agents run on a local model; every online lookup refuses. Your Core on the LAN is still reachable.", settings.mode, pickMode)
      )
    )
  );

  // ----- models
  const acc = settings.llm;
  const loc = settings.local_llm;
  const accProvider = el("select", {}, ...["auto", "claude-code", "anthropic", "openai-compatible"].map((p) => el("option", { value: p, text: p, selected: acc.provider === p })));
  const claudeModel = el("input", { type: "text", value: acc.claude_model || "", placeholder: "Claude Code's default" });
  const accUrl = el("input", { type: "text", value: acc.base_url });
  const locUrl = el("input", { type: "text", value: loc.base_url });
  const locModel = el("select", {}, el("option", { value: "", text: "auto - the best loaded model" }));
  if (loc.model) locModel.append(el("option", { value: loc.model, text: loc.model, selected: true }));
  const thinking = el("input", { type: "checkbox", checked: loc.thinking });
  const detect = async () => {
    const result = await api.get("/api/settings/models", { base_url: locUrl.value });
    const keep = locModel.value;
    clear(locModel).append(el("option", { value: "", text: result.reachable ? "auto - the best loaded model" : `not answering: ${result.error || ""}` }));
    for (const model of result.models) locModel.append(el("option", { value: model, text: model }));
    locModel.value = keep;
  };

  host.append(
    el(
      "div",
      { class: "card" },
      el("h3", { text: "Who answers" }),
      el("div", { class: "form-grid" }, field("Accuracy mode provider", accProvider, "auto: Claude Code if installed, then the API, then a local server"), field("Claude model", claudeModel), field("Or an OpenAI-compatible server", accUrl)),
      el("h3", { text: "Local model", style: "margin-top:14px" }),
      el("div", { class: "row" }, ...PRESETS.map(([name, url]) => el("button", { class: "btn small", text: name, onclick: () => { locUrl.value = url; detect(); } }))),
      el("div", { class: "form-grid" }, field("Server", locUrl), field("Model", locModel), el("label", { class: "check hint", style: "margin-top:18px" }, thinking, "let it think first (slower, sometimes better)")),
      el("div", { class: "row" }, el("button", { class: "btn small", text: "Detect loaded models", onclick: () => attempt(detect) }), el("span", { class: "hint", text: "Recommended: Qwen3-30B-A3B-Instruct-2507 or Qwen3.8-27B on 24 GB, Qwen3-8B on 12-16 GB. See docs/LOCAL_MODELS.md." }))
    )
  );
  if (status) {
    host.append(
      el(
        "div",
        { class: "card" },
        el("h3", { text: "Available right now" }),
        el(
          "div",
          { class: "list" },
          el("div", { class: "item" }, el("span", { class: `tag ${status.claude_code && status.claude_code_signed_in !== false ? "a2" : status.claude_code ? "warn" : "bad"}`, text: !status.claude_code ? "no" : status.claude_code_signed_in === false ? "signed out" : "yes" }), el("span", { class: "title", text: "Claude Code" }), el("span", { class: "meta", text: !status.claude_code ? "not on PATH" : status.claude_code_signed_in === false ? "installed, not signed in - run `claude` in a terminal and use /login; until then accuracy mode uses the API or a local model" : "installed - agents get rigsmith's tools and web search" })),
          el("div", { class: "item" }, el("span", { class: `tag ${status.anthropic_sdk && status.anthropic_key ? "a2" : "warn"}`, text: status.anthropic_sdk && status.anthropic_key ? "yes" : "no" }), el("span", { class: "title", text: "Anthropic API" }), el("span", { class: "meta", text: `SDK ${status.anthropic_sdk ? "installed" : "not installed ('rigsmith[claude]')"}, key ${status.anthropic_key ? "set" : "not set"}` })),
          el("div", { class: "item" }, el("span", { class: `tag ${status.local.reachable ? "a2" : "warn"}`, text: status.local.reachable ? "yes" : "no" }), el("span", { class: "title", text: "Local server" }), el("span", { class: "meta", text: status.local.reachable ? (status.local.models || []).join(", ") : status.local.error || "not answering" }))
        )
      )
    );
  }

  // ----- defaults
  const profile = el("select", {}, el("option", { value: "", text: "none" }), ...profiles.profiles.map((p) => el("option", { value: p.profile_id, text: p.label || p.profile_id, selected: settings.default_profile === p.profile_id })));
  const route = el("select", {}, el("option", { value: "", text: "the loop's own default" }), ...routes.routes.map((r) => el("option", { value: r.route_id, text: r.route_id, selected: settings.default_route === r.route_id })));
  const backend = el("select", {}, ...["sounddevice", "virtual"].map((b) => el("option", { value: b, text: b, selected: settings.audio_backend === b })));
  const previews = el("input", { type: "checkbox", checked: settings.allow_preview_downloads });
  const musicDirs = el("textarea", { rows: 2, placeholder: "your home folder's Music, when empty", text: (settings.music_dirs || []).join("\n") });
  host.append(
    el(
      "div",
      { class: "card" },
      el("h3", { text: "Defaults" }),
      el("div", { class: "form-grid" }, field("Your guitar", profile, "what parts are rendered on when nothing else is said"), field("Loop route", route), field("Audio", backend, "virtual = the simulated bench")),
      field("Music library folders, one per line", musicDirs, "where autopilot looks for your copy of a song by name - nothing leaves the machine"),
      el("label", { class: "check hint" }, previews, "fetch a store's 30-second preview when a song has no file (accuracy mode)"),
      el(
        "div",
        { class: "row" },
        el("span", { class: "grow" }),
        el("button", {
          class: "btn primary",
          text: "Save",
          onclick: () => attempt(async () => {
            await api.patch("/api/settings", {
              llm: { provider: accProvider.value, claude_model: claudeModel.value.trim(), base_url: accUrl.value.trim() },
              local_llm: { provider: "openai-compatible", base_url: locUrl.value.trim(), model: locModel.value, thinking: thinking.checked },
              default_profile: profile.value,
              default_route: route.value,
              audio_backend: backend.value,
              allow_preview_downloads: previews.checked,
              music_dirs: musicDirs.value.split(/\n+/).map((line) => line.trim()).filter(Boolean),
            });
            toast("Saved", { kind: "ok" });
            document.dispatchEvent(new CustomEvent("rigsmith:settings"));
          }),
        })
      )
    )
  );
}

export async function init() {
  await render();
}

export function onShow() {
  render();
}
