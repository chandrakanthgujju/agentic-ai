# ADR-0007: Cognito user pool, hosted UI + PKCE, `admin` group for KB management

- Status: Accepted
- Date: 2026-11-24

## Decision

- Cognito user pool with **self sign-up disabled** (admins invite users), 12+ character password
  policy, optional TOTP MFA, email-only recovery.
- A public app client (no secret) using the **authorization code flow with PKCE**, hosted UI on a
  Cognito domain, callback/logout URL = the CloudFront app URL only. The client is created in the
  Frontend stack because it needs the CloudFront domain (avoids a circular dependency).
- API Gateway Cognito authorizer validates the **ID token** on every `/api/*` route.
- `/api/kb/*` additionally requires the `admin` group, checked inside the KB admin Lambda from the
  verified claims (defence in depth: the UI hides the admin tab, but the server enforces it).
- Conversations are partitioned by the token's `sub`; a user can only read/delete their own.

## Trade-offs

- Tokens are kept in `sessionStorage` (cleared when the tab closes). This is exposed to XSS, which is
  mitigated by a strict CSP (`script-src 'self'`, no inline script), `textContent`-only rendering and
  no third-party scripts. A backend-for-frontend with HttpOnly cookies is the stronger alternative
  and is listed in the roadmap.
- ID tokens (not access tokens) are used for simplicity with the REST API Cognito authorizer.
