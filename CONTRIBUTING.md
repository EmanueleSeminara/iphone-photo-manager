# Contributing

Thank you for looking. This is a small project with one unusual property: **it can delete
your photos**. Almost every rule below comes from that.

## The short version

* Branch off `develop`, never off `main`.
* `pytest`, `ruff check src tests` and `mypy` must all pass.
* If you touch anything under `src/ipm/device/`, say so in the pull request — that code
  cannot be tested by CI, and someone with a real iPhone has to try it.

## Setting up

```bash
git clone https://github.com/EmanueleSeminara/iphone-photo-manager.git
cd iphone-photo-manager
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
```

Then, before you change anything:

```bash
scripts/check.sh                   # lint, types and ~300 tests. No iPhone needed, ~70 s.
```

That is the one command; it is what a pull request has to pass. Each run is written to
`.check-logs/` (git-ignored): the full output, a one-screen `latest-summary.txt`, and
pytest's JUnit XML, so results can be read rather than scrolled back to.

Once in a while, on a clean tree:

```bash
scripts/mutation-check.sh          # breaks one safety rule at a time, ~1 min
```

It fails if the suite does not notice. A green suite proves nothing on its own — tests
that assert the wrong thing are green too — and a rule reported as MISSED is a promise
the application makes that nothing verifies. It runs every step
even when an earlier one fails, so you see everything that is wrong in one go. The
pieces separately, if you prefer:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/python -m mypy           # strict, and it stays clean
```

The first launch after a fresh install is slow (10-30 s) because one of
`pymobiledevice3`'s imports has to be byte-compiled. It is not a hang.

## Branches

```
feat/<one-thing>  ──┐
fix/<one-thing>   ──┴──►  develop  ──(only after a manual test with a phone)──►  main
```

* **One branch per change**, off `develop`. Name it `feat/`, `fix/`, `chore/` or `docs/`.
* **`develop` is the integration branch.** Pull requests go there. It is the default
  branch, so you do not have to think about it.
* **`main` only ever receives `develop`**, and only after the checklist in
  [`MANUAL_TESTING.md`](MANUAL_TESTING.md) has been run against a real iPhone. Nothing
  reaches `main` untested, because the delete path cannot be covered by CI.
* Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/),
  in English.

## The architecture, in one rule

```
ipm/core/     business logic     — may NOT import pymobiledevice3 or ImageCaptureCore
ipm/device/   the only code that talks to hardware
ipm/tui/      presentation only
```

`ipm.core` depends on a `Protocol` (`ipm/device/base.py`), which is why every rule that
matters is unit-tested without hardware. `tests/test_readonly.py` enforces the boundary
statically and will fail your pull request if you cross it.

If a rule can live in `ipm/core`, put it there, where a test can reach it.

## Testing

| What | Where | Needs a phone |
|---|---|---|
| Business rules | `tests/test_*.py` | no |
| The whole app, driven through its UI | `tests/test_manual_checklist.py` | no |
| The layer boundary and the read-only guarantee | `tests/test_readonly.py` | no |
| Everything else | [`MANUAL_TESTING.md`](MANUAL_TESTING.md) | **yes** |

`tests/test_manual_checklist.py` is named after the numbered checks in
`MANUAL_TESTING.md`, so the two can be read side by side. It proves the app behaves as the
checklist describes *given a phone that behaves as we believe iPhones behave*. It cannot
prove the belief — which is why the checklist still exists.

Tests use `pytest-asyncio` in `auto` mode, so async test functions need no decorator.
Anything that would touch a device goes through `tests/fakes.py`.

## Things that are deliberate, and not up for optimisation

Please do not "streamline" these. Each one is there because of a specific way photos get
lost:

* **Import and delete are two separate actions.** There is no "delete after import"
  option and there will not be one. The value of the tool is that you look at the photos
  on your Mac first.
* **Both buttons open a dialog before doing anything.** Deleting also needs the word
  `DELETE` typed in full.
* **Deletion is checked three times**: when the dialog is built, again by the deep check
  that re-reads every local copy, and again immediately before each item goes.
* **Deletion goes through the phone's photo library, never the file system.** Unlinking a
  file over AFC leaves its row behind in the Photos app, as a broken item. The AFC channel
  is read-only and a test enforces it.
* **A partial deletion is counted in items, never in files.** Deleting "5 files" could
  strand the video half of a Live Photo; deleting "5 items" cannot.
* **Nothing is ever re-encoded.**

## Reporting a bug

Include the iPhone model and iOS version, your macOS version, and
**`<destination>/.ipm/last-run.log`** — every line the activity panel showed, kept for
the last five runs. If it involves deletion, say exactly what you expected to be removed
and what actually was.

Never paste the contents of a manifest or a `Photos.sqlite`: they describe every photo
you own. The run log does not contain either.

For anything that looks like a security problem, read [SECURITY.md](.github/SECURITY.md)
first: do not open a public issue.
