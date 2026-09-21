## Summary

<!-- What does this change and why? Link related issues. -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor / cleanup
- [ ] Documentation
- [ ] Tests

## Checklist

- [ ] Tests added/updated and `./.venv/bin/pytest -q tests/` passes
- [ ] JS syntax checks pass (`node -c` on touched files)
- [ ] DB model changes include an `init_db()` auto-migration step
- [ ] No secrets, resumes, personal company lists, `.env`/`.key`/`.api_key`, or DB files committed
- [ ] AI-failure paths surface an explicit error/`None` (no fabricated output)
- [ ] New unofficial channels are behind an opt-in flag defaulting to off
