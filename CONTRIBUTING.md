# Contributing

Thanks for your interest in improving Job Alert Agent. This project is a
local-first, single-user job-search agent; contributions that keep it
privacy-respecting, deterministic where possible, and honest about AI output
are very welcome.

## Development setup

```bash
git clone <repo-url> && cd job-alert-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

No build step is required for the SPA or the Chrome extension.

## Before opening a PR

1. Add or update tests covering your change (happy path + a boundary case).
2. Run the suite and make sure it is green:

   ```bash
   ./.venv/bin/pytest -q tests/
   ```

3. If you touched JS, run the syntax checks:

   ```bash
   node -c frontend/app.js
   node -c extension/js/runtime-adapter.js
   node -c extension/content/ats-autofill.js
   node -c extension/sidepanel/sidepanel.js
   ```

4. If you changed a SQLAlchemy model, add a lightweight `ALTER TABLE` step in
   `init_db()` (`backend/database.py`) so existing databases migrate without data loss.

## Ground rules

- **No fabricated AI output.** If an LLM/analysis step fails, surface an explicit
  error or `None` — never a plausible-looking default score, letter, or answer.
- **Privacy first.** Do not commit personal target-company lists, resumes, API keys,
  `.env`/`.key`/`.api_key`, or databases. User-specific data belongs in gitignored
  `data/` or `backend/companies_config.json`; ship only the `.example` files.
- **Opt-in for anything unofficial.** New scraping/automation channels must sit behind
  a feature flag that defaults to off.
- Keep changes focused and consistent with the existing module boundaries
  (see `PROJECT_INDEX.md`).

## Reporting bugs / requesting features

Use the issue templates in `.github/ISSUE_TEMPLATE/`. For security issues, please
avoid opening a public issue — see `SECURITY.md` if present, or contact the maintainer.
