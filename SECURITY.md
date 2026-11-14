# Security policy

This is a personal demonstration project. Please report a security problem privately:
**Security** tab → **Report a vulnerability**. Please don't open a public issue.
I aim to reply within 7 days.

## What's in place

- **Sign-in:** Google OpenID Connect, authorization-code flow with `state`, `nonce` and PKCE (S256), through
  Authlib. The ID token's signature is checked against Google's published keys; issuer, audience, expiry and
  nonce are validated, and the key claims are checked a second time by the console. `email_verified` must be
  true and the address must be on `ALLOWED_EMAILS`.
- **Sessions:** a signed cookie, `HttpOnly`, `SameSite=Lax`, `Secure` and `__Host-` prefixed by default,
  with an absolute 8-hour limit. Removing an address from the allow-list ends its sessions.
- **CSRF:** a per-session token on every form, compared in constant time.
- **Dev login** exists only for local development: startup refuses it unless `BASE_URL` is localhost and
  `ENVIRONMENT` isn't `production`; the runner refuses a non-loopback bind; the route answers 404 to any
  request that doesn't come from the same machine.
- **Browser hardening:** a strict Content Security Policy with no scripts at all and no inline styles,
  `X-Frame-Options: DENY`, `nosniff`, HSTS when served over https, `Cache-Control: no-store` on pages.
- **Abuse limits:** rate limits on sign-in, on asking and per client address; request bodies capped;
  one running question per user; per-question and per-day spend caps enforced by the agent before each call.
- **Privacy:** each user sees only their own questions. Logs are JSON lines with a request id; they never hold
  emails (a short hash instead), question or answer text, cookies, secrets or query strings.
- **Errors:** generic pages with a request id; no stack traces or settings ever reach the browser, and a
  refused configuration doesn't echo its values.
- **Supply chain:** pinned dependencies, pip-audit, Dependabot, CodeQL, gitleaks; a multi-stage image that
  runs as a non-root user.
