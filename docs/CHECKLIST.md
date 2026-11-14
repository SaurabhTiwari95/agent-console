# Release checklist

The same checklist every one of my projects meets before publishing. Ticked only when verified;
anything checked only by CI (not yet run) says so.

Last checked: 2026-11-10

## A. Installation and usage

- [x] What it is, with a diagram
- [x] Prerequisites with versions and links
- [x] Install steps for Windows (PowerShell) and macOS/Linux, with both ways to install the agent (GitHub tag or local clone)
- [x] Usage with real output, captured as text from a local run (dev login, demo client): sign-in, ask, request status, history, knowledge, search, health and cost, health endpoints
- [ ] Image screenshots: the README shows the pages as text captured from the running app; add PNGs after the first deploy if wanted
- [ ] Real Claude answers and real Google sign-in: **not run** (paid API and a Google client are needed; tests use fakes). The first live check is after the deploy in docs/deploy.md
- [x] One command for tests
- [x] Configuration table for every setting
- [x] Troubleshooting
- [x] Security model, how it scales, performance, design decisions, what's next
- [x] AI-assisted line
- [x] Relative links and anchors checked locally (0 broken); external links checked in CI by lychee
- [ ] Every command from a fresh clone: the local commands were run in this folder; `pip install -r requirements-agent.txt` and `docker build` need cost-capped-agent pushed first

## B. No private data

- [x] Made-up "Acme Traders" sample documents (shared with cost-capped-agent); `example.com` addresses only
- [x] No employer, product or people names; no real hosts, IPs, account ids or keys
- [x] No work code, names, counts or costs
- [x] Pre-publish check: 0 hits locally against the private block-list; also runs in CI
- [ ] gitleaks: CI only

## C. Secure by default

- [x] Secrets only from the environment; `.env` git-ignored; `.env.example` has placeholders; `make_env.py` generates the session secret
- [x] Google OIDC through Authlib: state, nonce, PKCE S256; ID token signature (JWKS), issuer, audience, expiry, nonce verified; `email_verified` and the allow-list enforced (tested against a fake provider with real keys)
- [x] Session cookie `HttpOnly`, `SameSite=Lax`, `Secure` + `__Host-` by default; absolute expiry; non-Secure only on localhost (tested)
- [x] CSRF token on every POST (tested)
- [x] Dev login refused in production, off localhost, on a public bind and from another machine (tested)
- [x] Input validated (Pydantic, size limits, body cap); parameterised SQL only
- [x] Rate limits on sign-in, ask and per client address (tested); `/healthz` and `/readyz` excluded
- [x] Security headers: strict CSP with no scripts and no inline styles, frame denial, nosniff, HSTS on https (tested)
- [x] Errors generic with a request id; a refused config doesn't echo secrets (tested)
- [x] Logs: JSON, no emails, question text, cookies, secrets or query strings (tested)
- [x] Users see only their own questions (tested)
- [x] Dependencies pinned; **pip-audit: no known vulnerabilities** (2026-11-10)
- [x] Dependabot (pip, Actions, Docker); CodeQL workflow; gitleaks in CI
- [ ] Docker: multi-stage, non-root uid 10001, read-only root filesystem: written, **checked in CI only** (Docker isn't installed here)
- [x] `SECURITY.md`
- [ ] CodeQL results: first run on GitHub after the push
- [ ] Actions pinned to commit SHAs: major versions for now; Dependabot keeps them current
- [ ] Repo settings on GitHub after the push: secret scanning + push protection, private vulnerability reporting, branch protection on `main`

## D. Scalable

- [x] Pages stateless (session in a signed cookie); data in SQLite files on one volume
- [x] 12-factor config; the same image for CI and Render
- [x] Migrations (numbered, `PRAGMA user_version`); indexes on every lookup; a partial unique index for one active question per user
- [x] Pagination on history and knowledge lists; bounded file views and search results
- [x] Long work off the request: background runs with a concurrency limit and a timeout; restarts recover
- [x] `/healthz` and `/readyz`
- [x] "How it scales" section, with the move to PostgreSQL, a queue and Redis for more than one instance
- [ ] Multi-instance: deliberately not built (one free instance); the README says what changes
- [ ] Persistent spend ledger on Render free: the disk is ephemeral, so the day cap resets on redeploy or spin-down. Decided 10 Nov 2026: a monthly spend limit on a dedicated Claude Console workspace and key is required before the first deploy (docs/deploy.md step 0.3); no paid disk

## E. Optimised

- [x] Async I/O; blocking SQLite work in worker threads, one hand-off per page where it mattered
- [x] Caching: the cost summary for 5 seconds; static CSS for an hour
- [x] No N+1: each page is a fixed handful of queries
- [x] Profiled before optimising: `/health` 278 → about 500 requests/s; first-question delay of 4 s removed by importing the SDK at start-up
- [x] LLM: model routing, prompt caching and caps come from cost-capped-agent; per-question cap chosen by the user, never above the agent's
- [ ] Multi-stage Docker image: written, built in CI only
- [x] Performance numbers in the README from a local load test

## F. Repo basics

- [x] Tests: 156 passed, 99% coverage (gate 80% in CI); no network, no key, no cost
- [x] Lint: ruff check and format clean
- [x] YAML files parse (CI, CodeQL, Dependabot, Render)
- [x] Markdown: core rules checked locally; full markdownlint in CI
- [x] `.gitignore`, `LICENSE` (MIT), `CHANGELOG.md`
- [x] CI: security (prepublish, gitleaks, pip-audit), test (ruff, pytest with coverage gate), docker, docs (markdownlint, lychee), deploy (main only, after all jobs, skips without the secret)
