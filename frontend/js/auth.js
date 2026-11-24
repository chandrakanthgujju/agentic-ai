// OAuth 2.0 authorization-code flow with PKCE against the Cognito hosted UI.
// No client secret exists in the browser; the PKCE verifier proves that the
// party exchanging the code is the one that started the login.
// Tokens live in sessionStorage (cleared when the tab closes). See docs/security.md
// for the trade-off versus a backend-for-frontend cookie session.

const KEY = "agentic.tokens";
let cfg;

export function init(config) { cfg = config; }

function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function randomString(n = 48) { return b64url(crypto.getRandomValues(new Uint8Array(n))); }
async function sha256(text) { return crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)); }

export function decodeJwt(token) {
  const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  return JSON.parse(decodeURIComponent(escape(atob(payload))));
}

function save(tokens) {
  const prev = load() || {};
  const merged = { ...prev, ...tokens };
  merged.expiresAt = Date.now() + (tokens.expires_in || 3600) * 1000 - 60_000;
  sessionStorage.setItem(KEY, JSON.stringify(merged));
  return merged;
}
function load() { try { return JSON.parse(sessionStorage.getItem(KEY)); } catch { return null; } }

async function tokenRequest(params) {
  const res = await fetch(`${cfg.cognitoDomain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: cfg.clientId, ...params }),
  });
  if (!res.ok) throw new Error(`token endpoint returned ${res.status}`);
  return res.json();
}

export async function login() {
  const verifier = randomString(64);
  const state = randomString(16);
  sessionStorage.setItem("agentic.pkce", JSON.stringify({ verifier, state }));
  const url = new URL(`${cfg.cognitoDomain}/oauth2/authorize`);
  url.search = new URLSearchParams({
    response_type: "code",
    client_id: cfg.clientId,
    redirect_uri: cfg.redirectUri,
    scope: "openid email profile",
    state,
    code_challenge_method: "S256",
    code_challenge: b64url(await sha256(verifier)),
  }).toString();
  location.assign(url.toString());
}

export async function handleRedirect() {
  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  if (!code) return false;
  const pkce = JSON.parse(sessionStorage.getItem("agentic.pkce") || "{}");
  sessionStorage.removeItem("agentic.pkce");
  history.replaceState({}, "", location.pathname);
  if (!pkce.verifier || params.get("state") !== pkce.state) throw new Error("login state mismatch; please sign in again");
  save(await tokenRequest({ grant_type: "authorization_code", code, redirect_uri: cfg.redirectUri, code_verifier: pkce.verifier }));
  return true;
}

export async function idToken() {
  const t = load();
  if (!t) return null;
  if (Date.now() < t.expiresAt) return t.id_token;
  if (!t.refresh_token) { logoutLocal(); return null; }
  try {
    return save(await tokenRequest({ grant_type: "refresh_token", refresh_token: t.refresh_token })).id_token;
  } catch { logoutLocal(); return null; }
}

export function claims() {
  const t = load();
  return t?.id_token ? decodeJwt(t.id_token) : null;
}

export function isAdmin() {
  const groups = claims()?.["cognito:groups"] || [];
  return Array.isArray(groups) ? groups.includes("admin") : String(groups).includes("admin");
}

function logoutLocal() { sessionStorage.removeItem(KEY); }

export function logout() {
  logoutLocal();
  const url = new URL(`${cfg.cognitoDomain}/logout`);
  url.search = new URLSearchParams({ client_id: cfg.clientId, logout_uri: cfg.redirectUri }).toString();
  location.assign(url.toString());
}
