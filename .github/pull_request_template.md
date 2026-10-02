<!-- Pull requests go to `develop`, which is the default branch. Never to `main`. -->

## What this changes, and why

<!-- One or two sentences. If it fixes an issue, "Fixes #123". -->

## Checks

- [ ] `.venv/bin/python -m pytest -q` passes
- [ ] `.venv/bin/ruff check src tests` passes
- [ ] `.venv/bin/python -m mypy` passes (strict, and it stays clean)
- [ ] New behaviour has a test, or there is a reason here why it cannot have one

## Does this touch the phone?

- [ ] **No** — nothing under `src/ipm/device/` changed
- [ ] **Yes** — and I have said below what I ran against a real iPhone, with which model
      and iOS version

<!--
CI has no iPhone. Anything under src/ipm/device/ is unverified until a human confirms it
with a phone attached, so please say plainly what you did and did not try. "I could not
test this" is a useful answer; a silent assumption is not.

If the change affects deletion, MANUAL_TESTING.md tests 16b, 17 and 18 are the ones that
matter, and they are the only place where a bug cannot be undone.
-->
