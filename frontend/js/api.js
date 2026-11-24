// Thin client for the same-origin /api routes. Every call carries the Cognito
// ID token; API Gateway's Cognito authorizer validates it before any Lambda runs.
import { idToken } from "./auth.js";

let base = "/api";
export function init(config) { base = config.apiBase || "/api"; }

async function authHeaders(extra = {}) {
  const token = await idToken();
  if (!token) throw new Error("not signed in");
  return { Authorization: token, ...extra };
}

export async function request(method, path, body) {
  const res = await fetch(`${base}${path}`, {
    method,
    headers: await authHeaders(body ? { "Content-Type": "application/json" } : {}),
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw Object.assign(new Error(data.message || `HTTP ${res.status}`), { status: res.status, data });
  return data;
}

// POST /api/chat and parse the Server-Sent Events stream as it arrives.
export async function streamChat(payload, onEvent, signal) {
  const res = await fetch(`${base}/chat`, {
    method: "POST",
    headers: await authHeaders({ "Content-Type": "application/json", Accept: "text/event-stream" }),
    body: JSON.stringify(payload),
    signal,
  });
  if (!res.ok || !res.body) {
    let message = `HTTP ${res.status}`;
    try { message = (await res.json()).message || message; } catch { /* not JSON */ }
    if (res.status === 429) message = "Too many requests (API throttling). Wait a moment and retry.";
    throw new Error(message);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let type = "message", data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) type = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) onEvent(type, JSON.parse(data));
    }
  }
}
