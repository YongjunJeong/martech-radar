# Repository instructions

- Inspect existing code and preserve unrelated work before editing.
- Keep collection independent of optional KB/AI services.
- Do not infer installation or removal from a single observation. Keep blocked,
  incomplete, policy-reference and browser evidence distinct.
- Use temporary workspaces, synthetic company fixtures and local test pages.
  Do not scan live targets or invoke paid models as part of automated tests.
- Never read or expose credentials, production radar.toml, private target lists,
  database contents, customer documents or CLI login files for publication.
- Do not restart production, install timers, expand the live watchlist, publish,
  push or send external messages without explicit authorization.
- Validate with `PYTHONPATH=. .venv/bin/pytest -q`,
  `.venv/bin/ruff check radar web tests deploy`, and relevant shell syntax checks.
  Report checks actually executed; separate mocked and live-model validation.
- Keep public docs portable. Operational reports and host-specific scheduled
  units stay outside the public source set. Do not delete files used by timers.
