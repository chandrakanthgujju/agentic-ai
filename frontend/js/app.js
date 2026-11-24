import * as auth from "./auth.js";
import * as api from "./api.js";
import { refreshDocs, refreshJobs, wireAdmin } from "./admin.js";

const $ = (id) => document.getElementById(id);
const state = { conversationId: null, busy: false };

function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 4000);
}

const VIEWS = ["signedOut", "chatView", "adminView"];

// Exactly one <main> view is visible at a time; the model picker only belongs to chat.
function show(view) {
  for (const id of VIEWS) $(id).hidden = id !== view;
  for (const b of document.querySelectorAll(".tab")) b.classList.toggle("active", `${b.dataset.view}View` === view);
  $("modelPicker").hidden = view !== "chatView";
}

// Single place that renders signed-in vs signed-out chrome, used on first load,
// after the OAuth redirect callback, after a reload, and on bfcache restores.
function renderAuth(claims) {
  const signedIn = Boolean(claims);
  document.body.dataset.auth = signedIn ? "in" : "out";
  $("userEmail").textContent = signedIn ? claims.email || "" : "";
  $("logoutBtn").hidden = !signedIn;
  $("tabs").hidden = !signedIn;
  $("adminTab").hidden = !(signedIn && auth.isAdmin());
  if (!signedIn) show("signedOut");
}

function addMessage(role, text = "") {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  const tools = document.createElement("div"); tools.className = "tools"; tools.hidden = true;
  const body = document.createElement("div"); body.className = "body"; body.textContent = text;
  el.append(tools, body);
  $("messages").append(el);
  $("messages").scrollTop = $("messages").scrollHeight;
  return { el, tools, body };
}

async function loadModels() {
  const { models } = await api.request("GET", "/models");
  const sel = $("modelSelect");
  sel.replaceChildren(...models.map((m) => {
    const o = document.createElement("option"); o.value = m.id; o.textContent = m.label; o.selected = m.default; return o;
  }));
  const saved = localStorage.getItem("agentic.model");
  if (saved && models.some((m) => m.id === saved)) sel.value = saved;
  sel.onchange = () => localStorage.setItem("agentic.model", sel.value);
}

async function loadConversations() {
  const { conversations } = await api.request("GET", "/conversations");
  const list = $("conversationList");
  list.replaceChildren(...conversations.map((c) => {
    const li = document.createElement("li");
    li.classList.toggle("active", c.conversationId === state.conversationId);
    const span = document.createElement("span"); span.textContent = c.title || "Untitled";
    const del = document.createElement("button"); del.className = "ghost"; del.textContent = "✕"; del.type = "button"; del.title = "Delete";
    del.onclick = async (e) => {
      e.stopPropagation();
      await api.request("DELETE", `/conversations/${c.conversationId}`);
      if (state.conversationId === c.conversationId) newChat();
      loadConversations();
    };
    li.onclick = () => openConversation(c.conversationId);
    li.append(span, del);
    return li;
  }));
}

async function openConversation(id) {
  const conv = await api.request("GET", `/conversations/${id}`);
  state.conversationId = id;
  $("messages").replaceChildren();
  for (const m of conv.messages) {
    const { tools } = addMessage(m.role, m.text);
    for (const t of m.tools || []) { tools.hidden = false; const c = document.createElement("span"); c.className = "chip"; c.textContent = `🛠 ${t}`; tools.append(c); }
  }
  loadConversations();
}

function newChat() {
  state.conversationId = null;
  $("messages").replaceChildren();
  loadConversations().catch(() => {});
}

async function send(text) {
  if (state.busy) return;
  state.busy = true; $("sendBtn").disabled = true;
  const wasNewConversation = !state.conversationId;
  addMessage("user", text);
  const out = addMessage("assistant");
  out.body.classList.add("cursor");
  const chips = new Map();
  const citations = [];
  let failed = false;
  try {
    await api.streamChat(
      { message: text, modelId: $("modelSelect").value, ...(state.conversationId ? { conversationId: state.conversationId } : {}) },
      (type, data) => {
        if (type === "meta") state.conversationId = data.conversationId;
        else if (type === "text") { out.body.textContent += data.delta; $("messages").scrollTop = $("messages").scrollHeight; }
        else if (type === "tool_use") {
          out.tools.hidden = false;
          const c = document.createElement("span"); c.className = "chip"; c.textContent = `🛠 ${data.name}…`;
          c.title = JSON.stringify(data.input); chips.set(data.id, c); out.tools.append(c);
        } else if (type === "tool_result") {
          const c = chips.get(data.id);
          if (c) { c.className = `chip ${data.ok ? "ok" : "err"}`; c.textContent = `🛠 ${data.name} · ${data.ms} ms`; }
          citations.push(...(data.citations || []));
        } else if (type === "warning") addMessage("warning", data.message);
        else if (type === "error") {
          failed = true;
          addMessage("error", data.code ? `${data.message} (${data.code})` : data.message);
          // A failed first turn is not saved server-side; don't point the next
          // message at a conversation that does not exist.
          if (wasNewConversation) state.conversationId = null;
        }
        else if (type === "done") {
          const meta = document.createElement("div"); meta.className = "meta";
          meta.textContent = `${data.usage.inputTokens} in / ${data.usage.outputTokens} out tokens · ${data.iterations} model call(s) · ${data.toolCalls} tool call(s)`;
          out.el.append(meta);
        }
      },
    );
    if (citations.length) {
      const c = document.createElement("div"); c.className = "citations";
      c.textContent = "Sources: " + citations.map((x) => `[${x.id}] ${x.source}`).join("  ·  ");
      out.el.append(c);
    }
  } catch (e) {
    failed = true;
    addMessage("error", e.message);
    if (wasNewConversation) state.conversationId = null;
  } finally {
    out.body.classList.remove("cursor");
    if (!out.body.textContent) {
      // Never show a placeholder next to an error: drop the empty bubble unless
      // it carries tool chips worth keeping.
      if (failed) { if (out.tools.hidden) out.el.remove(); }
      else out.body.textContent = "(no text returned)";
    }
    state.busy = false; $("sendBtn").disabled = false;
    loadConversations().catch(() => {});
  }
}

async function main() {
  const config = await (await fetch("/config.json", { cache: "no-store" })).json();
  auth.init(config); api.init(config);
  $("loginBtn").onclick = () => auth.login();
  $("logoutBtn").onclick = () => auth.logout();

  try { await auth.handleRedirect(); } catch (e) { toast(e.message); }
  // Back/forward cache can restore a stale signed-out/in page; re-check on restore.
  window.addEventListener("pageshow", async (e) => {
    if (e.persisted) renderAuth((await auth.idToken()) ? auth.claims() : null);
  });
  if (!(await auth.idToken())) { renderAuth(null); return; }

  renderAuth(auth.claims());
  if (auth.isAdmin()) wireAdmin(toast);
  for (const b of document.querySelectorAll(".tab")) {
    b.onclick = () => {
      show(`${b.dataset.view}View`);
      if (b.dataset.view === "admin") { refreshDocs(toast); refreshJobs(toast); }
    };
  }
  show("chatView");
  $("newChatBtn").onclick = newChat;
  $("chatForm").onsubmit = (e) => {
    e.preventDefault();
    const text = $("messageInput").value.trim();
    if (!text) return;
    $("messageInput").value = "";
    send(text);
  };
  $("messageInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("chatForm").requestSubmit(); }
  });
  await Promise.all([loadModels(), loadConversations()]).catch((e) => toast(e.message));
}

main().catch((e) => { console.error(e); toast(e.message); });
