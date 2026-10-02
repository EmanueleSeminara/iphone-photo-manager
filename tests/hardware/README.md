# The hardware suite

Tests that need a real iPhone on the end of a cable. They are **excluded from every
normal `pytest` run** and have to be asked for by name.

```bash
cp .ipm-hw.env.example .ipm-hw.env     # then fill in IPM_HW_UDID
.venv/bin/python -m pytest -m hardware -q
```

Without configuration every test skips with a sentence saying what is missing. Nothing
here writes to the phone.

**`-m hardware` does not include the destructive test.** `check.sh --hardware` passes
`--ignore` for `test_sacrificial_deletion.py`, so the fifteen read-only tests run and the
sixteenth does not; the summary says so in as many words. Only `--delete` includes it, and
only after you have typed `DELETE`.

**The phone does not need to be unlocked.** That was believed for a while, because the
first `requestOpenSession` after the device is announced is refused with `-9943` and
Apple's own text for that code says "Please unlock". It is also what a genuinely locked
phone returns, and the two cannot be told apart — but the session opens on a retry, and a
run with the screen locked passed all fourteen tests in 52 seconds. `ptp.py` retries for
about fifteen seconds before believing the phone.

Deletion has only been run with the phone unlocked, so if you are testing that, leave it
unlocked until someone has checked the other way.

## Why this is not run in CI, ever

GitHub's own documentation says not to use self-hosted runners with public repositories:
anyone can open a pull request from a fork, and the workflow would execute their code on
the machine hosting the runner. That machine is the one with your photos and your phone
attached. So this suite is run by hand, by someone who can see the phone, and CI never
touches it.

## What it covers

Roughly checks 3, 4, 7 and 8 of [`MANUAL_TESTING.md`](../../MANUAL_TESTING.md), plus two
things the checklist cannot express as a click:

* **the join key.** Matching a manifest row to a library item uses
  `(original filename, size)`. The whole delete design assumes that pair is unique on both
  sides; it was measured once by hand against a 16 000-item library, and this keeps the
  measurement honest;
* **the dry-run plan.** The gate and the matcher are run end to end against the real
  manifest and the real library, and the result is inspected and thrown away. It is the
  deletion up to the last instruction — the most that can be automated without removing a
  photo.

What it does **not** cover, and what no script can: taking a photo (12, 16b), pulling the
cable (13), and the deletion itself (17, 18).

## The five gates

Deleting a photo you wanted is the one mistake this project cannot undo, so the guards are
deliberately redundant:

1. **the marker.** `addopts = -m "not hardware"` in `pyproject.toml`. A distracted
   `pytest` never reaches this code;
2. **the read whitelist.** `IPM_HW_UDID` names the single phone the suite may talk to at
   all. No UDID, no run. A *different* phone plugged in is a failure, not a skip — a
   silent skip is how you end up believing you tested something;
3. **the delete whitelist.** `IPM_HW_DESTRUCTIVE_UDID` must name that same phone *again*.
   Reading from a device and removing photos from it are different permissions, and the
   second is worth stating twice rather than inferring from the first;
4. **the blocklist.** `IPM_HW_PROTECTED_UDIDS` overrides everything above, including
   `--allow-delete`. Put other people's phones in it, and your own as soon as you have a
   spare to test deletion on;
5. **the brake.** `conftest.py` replaces `PtpService.delete_assets` with a function that
   raises unless every gate above is open, so no test here can remove anything even if it
   tries — and `test_this_suite_cannot_delete_anything` proves the brake is on.

## `--allow-delete`, and the sacrificial list

One test does remove photos: `test_sacrificial_deletion.py`, which is check 17 of the
checklist, scripted. It needs all of this to be true at once:

```bash
cp tests/hardware/sacrificial.txt.example tests/hardware/sacrificial.txt
# write the file names of photos you are willing to lose, one per line
.venv/bin/python -m pytest -m hardware --allow-delete -q tests/hardware/test_sacrificial_deletion.py
```

**The test never chooses what to delete.** There is no "most recent N" and no default:
the selection is your file, and an item not named in it cannot be reached.

Write **local paths**, as you see them in the Finder: `2026/09/IMG_7045.JPG`, relative to
your destination folder. They are unique inside a destination, and you can look at the
photo before committing it to deletion. Device paths (`/DCIM/117APPLE/IMG_7045.JPG`) work
too, and so does a bare file name — but only when it is unambiguous. File names on an
iPhone are not unique: iOS numbers photos per folder and starts again in the next one, so
the same `IMG_7045.JPG` can be a photo from last year *and* the throwaway you took a
minute ago. A name matching two imported files stops the run and prints both. A bug in
selection logic therefore cannot cost you a photo, because there is no selection logic —
which is the whole reason it is built this way. At most three items per run, and that
ceiling is a constant in the code, not a setting.

Before anything is requested it re-checks, independently of the application's own gate,
that every named file is on the phone, has a manifest row, and has a local copy whose
SHA-256 it recomputes and compares. If the application's gate ever breaks, this still
refuses.

Afterwards it measures rather than assumes: the phone is re-scanned, and the run fails if
anything disappeared that was not offered up. It also fails if the item was deleted but
its Live Photo half or `.AAE` sidecar stayed behind — **that failure is a finding, not a
flake.** It is the one question nobody has been able to answer, so write down exactly
which files were listed before changing anything.

**Photos removed this way do not go to *Recently Deleted*.** Measured on an iPhone 13 /
iOS 26.6: the photo service removes items outright, so the copy in the destination
folder is the only one left. Offer up nothing you would miss.
