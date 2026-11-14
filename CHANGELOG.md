# Changelog

## 0.1.0 (2026-11-10)

- Google sign-in (OpenID Connect with state, nonce and PKCE, via Authlib) with an email allow-list.
- Local dev login, refused outside localhost and in production.
- Ask page: questions run in the background through cost-capped-agent's `Agent.ask()`, with a cap per
  question, one active question per user and a concurrency limit; status, answer, citations, steps and cost.
- History of each user's questions, paginated.
- Knowledge pages: indexed files with summaries, full-text search with highlights, file view with line links.
- Health and cost page: spend per day and model, today against the daily cap, cache hit rate, last runs,
  readiness.
- `/healthz`, `/readyz`, security headers, CSRF, rate limits, JSON logs without personal data.
- Offline demo client for local runs without a key.
- Dockerfile, Render blueprint, CI with an auto-deploy job after all checks pass.
