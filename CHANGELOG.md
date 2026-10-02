# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-10-02

First public release.

The application was built and rewritten several times in private before this point;
those versions were never published and are not listed separately. What follows is
what 1.0.0 does, and the few things worth knowing about how it got here.

### The tool

* **Live USB detection.** The app stays open and notices a phone plugged in or pulled
  out. Polling asks the local `usbmuxd` daemon what is attached and never touches the
  phone itself, so nothing is woken up 1.5 seconds at a time.
* **Lossless import** of everything under `/DCIM` and `/PhotoData/CPLAssets` into
  `DESTINATION/YYYY/MM/`, byte for byte, with the device timestamp restored. Nothing
  is ever re-encoded. HEIC stays HEIC; a Live Photo's `.MOV` half and an `.AAE`
  sidecar are copied like any other file and land beside their photo.
* **Resumable, and safe to interrupt.** Every transfer is written to `<name>.ipm-part`
  and promoted with an atomic rename, so a crash, an unplugged cable or a sleeping Mac
  leaves complete files and at worst one leftover part file, which the next run
  removes. Press Import again to carry on.
* **Deletion is a separate action, and only ever removes what has just been
  verified.** There is no "delete after import" and there will not be one. Before
  anything goes, the local copy is stat-ed, its SHA-256 is recomputed and compared
  with what was recorded during the transfer, and the file on the phone is checked
  against the manifest row for both size and timestamp. A partial deletion is counted
  in *items*, never in files, so a Live Photo cannot be split, and "the most recent
  items" are the ones *taken* most recently: a file is dated by the earlier of its
  creation and modification times, because the latter can be wrong — even in the
  future.
* **A photo edited on the phone is imported but never deleted.** iOS keeps an edit
  apart from the original, and this version imports the original only, so deleting
  the item would lose the edit. Every photo with an edit is found by the folder iOS
  keeps it in, which is named after the original, and stays on the phone; the delete
  dialog lists them separately. If the edits cannot be read, nothing is deleted at all.
* **The summary after a deletion says what the phone really did.** A photo the iPhone
  ignored is reported as still on the phone, untouched, with the advice to press `d`
  again; a Live Photo half left behind by a deleted still has a sentence of its own.
* **Deletion goes through the phone's own photo library**, the way Image Capture does,
  not by unlinking files. Unlinking leaves the Photos app showing an item whose file
  is gone, and no restart clears it. The AFC channel is read-only with no exception,
  and a test parses the source to prove it.
* **What actually happened is measured, not assumed.** After every deletion the phone
  is re-scanned and only files that have really disappeared are recorded as deleted.
* **Library check** (`v`) copies the phone's own photo database, read-only, and
  reconciles it against the file scan: how many items the Photos app shows, how many
  are in Recently Deleted, and whether any library item has no file behind it.
* **A manifest per destination folder** (`.ipm/manifest.db`, SQLite), so the folder is
  self-describing and history moves with it. Losing it costs a re-verification, not
  the photos: files carry the device timestamp and are recognised again.
* **The app is additive to its destination.** It touches only the `YYYY/MM` folders it
  creates, `.ipm/`, and `*.ipm-part`. Anything else in that folder is invisible to it.
* **A run log** at `.ipm/last-run.log`, five runs deep — the activity panel in plain
  text, safe to attach to a bug report.

### Verified on hardware

Confirmed on an iPhone 13 running iOS 26.6.1, against a 16 749-item library:

* a full 17 000-file, 33 GB import, twice, with no leftover part files;
* the library check reconciling to the item the Photos app shows;
* deletion, watched removing photos and their Live Photo halves, verified afterwards
  against the phone's own database;
* `(original filename, size)` unique across the whole library on both sides — the key
  the delete matcher joins on;
* **the phone sometimes ignores a delete request**: it accepts it, reports no error,
  and does nothing. The application detects this, because it believes the re-scan
  rather than the acknowledgement, and asks once more;
* a whole phone emptied on iOS 26.7.1 (2 October 2026): about 17 000 items deleted, each
  re-verified first. Live Photos were ignored by every first request and removed by the
  second; about 45 ambiguous items and every edited photo were kept, as designed.

### Known behaviour worth reading before deleting

* **Deleted photos do not go to Recently Deleted.** The photo service removes them
  outright. The application says so once, in its own dialog, before your first
  deletion, and asks you to type `DELETE` to confirm the run.
* **Your edits stay on the phone.** Only originals are imported; iOS keeps edited
  versions elsewhere. Delete the phone and the edits go with it.
* **A few items cannot be matched to the library and are kept** rather than guessed
  at — 0.3% of a real library.

### Testing

* `scripts/check.sh` — lint, types and the whole suite in one command, ~300 tests, no
  phone needed. `--hardware` adds a read-only suite that talks to a real iPhone;
  `--hardware --delete` adds the single test that removes a photo, which can only
  reach items named by hand in a file you write yourself.
* `scripts/mutation-check.sh` — breaks one safety rule at a time and fails if the
  suite does not notice.
* `tests/test_manual_checklist.py` drives the whole application through its interface,
  named after the numbered checks in `MANUAL_TESTING.md` so the two read side by side.
* CI runs on macOS for Python 3.11-3.13, plus one Linux job whose only purpose is to
  catch the day the business logic grows a platform dependency. Hardware tests never
  run in CI and never will: a forked pull request would execute a stranger's code on
  the machine with the photos and the phone attached.
