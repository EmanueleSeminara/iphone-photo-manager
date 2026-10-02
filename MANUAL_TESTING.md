# Manual testing with a real iPhone

The automated suite (`pytest`, ~300 tests) covers every rule that does not need hardware.
It cannot cover the one thing that matters most: what actually happens between this app
and a real phone -- so this checklist is the part only a human with a cable can do.

**Deletion has been run against real hardware**, on one iPhone 13: on iOS 26.6.1, the
scripted sacrificial test and twice through the app, each time verified afterwards
against the phone's own database; on iOS 26.7.1, this whole checklist (13, 18, 18b,
18c) and then the owner's entire library, about 17 000 items. One phone and one tester is not the same as tested,
which is exactly why this checklist still exists and why the model table in
[`README.md`](README.md) has one row in it.

Run it on the `develop` branch **before** anything is merged into `main`.

## What you need, and how long it takes

* an iPhone, a USB **data** cable, the phone unlocked and trusted;
* free disk space for a full copy of the phone's library;
* about **40 minutes of attention**, plus the transfer itself — around 16 minutes for a
  32 GB library, because the Lightning connector is USB 2 and tops out near 34 MB/s.

There is no way to import a subset yet, so the full import happens **once** (test 7) and
every later test reuses it. Do not restart from zero unless a test says so.

**Tests 1 to 16b remove nothing from the iPhone.** Only **17** and **18** delete, and both
say so in their title. Work through them in order: several tests set up the state the next
one needs.

**16b takes two minutes and makes 17 worth running.** It has you create one throwaway
Live Photo, so the photo you delete in 17 actually has a `.MOV` half rather than being a
lone still that proves nothing. That the half rides along with its photo has been watched
happening, once; that an `.AAE` sidecar does has not, and cannot be arranged on demand --
see 16b for why.

### What the automated suite already does for you

`tests/test_manual_checklist.py` drives the real application -- key presses, dialogs,
workers, the manifest on disk -- against a fake phone, and covers the behaviour behind
checks **1, 2, 3, 4, 6, 10, 11, 13, 14, 16, 19, 20** and **21**. Run it in seconds:

```bash
.venv/bin/python -m pytest tests/test_manual_checklist.py -q
```

There is a second suite that *does* use the phone, and it is read-only:

```bash
cp .ipm-hw.env.example .ipm-hw.env     # fill in IPM_HW_UDID, once
.venv/bin/python -m pytest -m hardware -q
```

It checks the handshake, the device panel, the scan, the item arithmetic, the byte
fidelity of a download, and — the part no click can express — that the phone's library
matches the manifest well enough for a deletion to be planned. It builds that plan in
full and throws it away. It cannot delete: `tests/hardware/README.md` explains the five
gates that stop it, and one of the tests is there to prove the brake works.

Run it before working through checks 3, 4, 7 and 8 by hand; it will have answered most of
them already.

That does not make those checks pointless by hand, but it changes what they are for. The
automated version proves the app behaves as described *given a phone that behaves as we
believe iPhones behave*; running them for real is what tests the belief. So when you are
short on time, run the suite and spend your attention on the checks below that it cannot
reach at all:

* **5** and **7** -- the real transfer, and the phone's own database;
* **8** -- that the bytes on disk are the bytes the phone sent;
* **12** and **16b** -- only a human can take a photo and edit it;
* **13**'s cable, which no script can pull;
* **15**, worth doing once by hand because it is the safety net everything else leans on;
* **17** and **18** -- the deletion itself, which has no automated substitute and never will.

Tick the boxes as you go — a half-run checklist you cannot reconstruct is worse than none.

### If you are short on time

The tests do not all cost the same, and they do not all matter the same. Ranked by what
you lose by skipping one:

* **15 is a prerequisite for 17 and 18.** It is the proof that a damaged local copy cannot
  get its original deleted from the phone. Do not delete anything for real until it has
  passed. It is also cheap — the deep check reads only the files you selected, and you
  select one — so budget about two minutes, not an hour.
* **14** covers the other half of that same gate (a copy that is *missing* rather than
  damaged) and is another two minutes.
* **13** is the expensive one: it needs a real transfer running, so it wants a few dozen
  files to copy and your attention while they do. It is also the least urgent — every
  transfer is written to a temporary file and renamed into place, so the worst an
  interruption leaves behind is a `.ipm-part` that the next run deletes. Postpone it
  without guilt, but do run it before the tool goes to anyone else.
* **16b** is two minutes and is what makes 17 an experiment rather than a formality. Skip
  it and you will delete some ordinary photo that may have no Live Photo half at all, which
  answers nothing.
* **19, 20, 21** are quick and independent; do them whenever.

Anything you skip, leave unticked and say so when you report — an untested path that
everyone assumes was tested is how a delete bug reaches someone's photos.

## Setup

```bash
cd ~/projects/iphone_photo_manager
git checkout develop
git log --oneline -1                  # what you are about to test
.venv/bin/python -m pytest -q         # must be green before you start
.venv/bin/ruff check src tests
.venv/bin/python -m mypy

.venv/bin/ipm --log --verbose         # the log makes any bug report much easier
```

`ipm` needs a real terminal, so run it yourself — not through an editor pane. Everything
below assumes it is running in the foreground of one terminal.

The log lands in `~/.local/state/iphone-photo-manager/ipm.log`.

Use a **scratch folder** for the whole checklist — `~/Desktop/ipm-test` is the one the
examples name — rather than your real photo archive. Any folder does, as long as you keep
using the same one; the text says `~/Desktop/ipm-test` wherever a concrete path is needed.

### `$DEST` — set this once, in your second terminal

Keep a second terminal open for the shell commands. They all talk to the folder you told
the app to use and call it `$DEST`, so define it once and everything below can be pasted
as it is:

```bash
DEST=$(sed -n 's/.*"destination": *"\(.*\)".*/\1/p' \
       ~/.config/iphone-photo-manager/config.json)
echo "$DEST"                 # the folder the app is actually using
ls -l "$DEST/.ipm/manifest.db"
```

Or set it by hand: `DEST=~/Pictures/iPhone13` — **without quotes around the tilde**. In
`"~/Pictures/…"` the shell does not expand `~`, and every command then fails with
`unable to open database file` while the file is plainly there. Once `$DEST` holds a real
path, quoting it everywhere else (`"$DEST/.ipm/manifest.db"`) is right and expected.

**Where things live:** the import history, checksums included, is the SQLite file
`$DEST/.ipm/manifest.db` — inside your destination folder, never in the repository. The
only two things outside it are `~/.config/iphone-photo-manager/config.json` and, if you
asked for it, the log.

**Leave `ipm` running.** The manifest is a WAL database, so reading it from another
terminal while the app is open is safe and needs no coordination — every `sqlite3 … select
…` in this checklist can be run with the app on screen. Two courtesies for the one test
that *writes* to it (19): do it while no import or deletion is running, and press `r`
afterwards so the app re-reads. If a command ever answers `database is locked`, the app was
mid-write; wait a second and run it again. Never delete `manifest.db-wal` or
`manifest.db-shm` by hand.

### Starting from zero, at any point

```bash
scripts/reset-test-state.sh --dry-run          # see what it would remove
scripts/reset-test-state.sh                    # then type RESET to confirm
```

With no arguments it cleans the folder currently configured in the app; pass
`--dest ~/Desktop/ipm-test` to be explicit. It removes the app's entire footprint — the
`YYYY/` folders it created, any `*.ipm-part` leftovers, `.ipm/` (manifest and checksums),
the config file and the log — so the next `ipm` behaves exactly like a first run.

It **never touches the iPhone**, and inside the destination it only ever removes those
three things: anything else you keep in that folder is listed as "would keep" and left
alone. It refuses outright to clean a folder that has files but no `.ipm/manifest.db`,
so it cannot be pointed at your real archive by mistake.

`--keep-photos` removes the manifest, config and log but leaves the copied photos. The
next import then **adopts** them: it recognises every file that is already there (same
name, same size, same timestamp), hashes it from disk, writes its history row back, and
transfers nothing. That is the cheap way to redo the whole checklist without waiting for
16 minutes of USB again — and a test of the adoption rule in its own right.

---

## 1. Empty state — no phone connected

- [ ] Run `scripts/reset-test-state.sh --dest ~/Desktop/ipm-test` so this really is a
      first run (it will say "already a clean slate" the very first time).
- [ ] Start `ipm` with **no** iPhone plugged in.
- [ ] The iPhone panel says *"Waiting for an iPhone…"*.
- [ ] **Import** and **Delete** are visibly greyed out; **Change folder** is not.
- [ ] The app does not exit, does not print a traceback, and the clock in the header ticks.

## 2. First run asks for a folder

- [ ] On a truly clean start (see *Starting from zero*) the folder dialog opens by itself.
- [ ] Type `~/Desktop/ipm-test` and confirm. The folder is created.
- [ ] The "On this Mac" panel shows that folder and three zero counters.
- [ ] Quit with `q`, start `ipm` again: the folder is remembered, no dialog.
- [ ] Press `f`, type a nonsense path like `/nope/nope`, confirm: a clear error line
      appears inside the dialog, no crash. Press Escape and check the folder is unchanged.

## 3. Live detection

- [ ] With the app running, plug the iPhone in and unlock it. Tap **Trust** if asked.
- [ ] Within a couple of seconds the panel fills in by itself: name, model as a marketing
      name (*iPhone 13*, not `iPhone14,5`), iOS version, battery %, storage.
- [ ] The activity log says **"First time this iPhone is used with this folder"** — this is
      shown exactly once, on this first connection.
- [ ] Then it scans: a `Scanning… N files` line that keeps moving, and at the end
      *"Found N item(s) — M file(s), X GB"*.
- [ ] Unplug the cable: the panel empties, both buttons grey out, no error.
- [ ] Plug it back in: it repopulates, and this time the log says **"Known iPhone — last
      used with this folder on …"** with today's date and time.
- [ ] Repeat once more. Nothing accumulates, nothing slows down.

## 4. The numbers are the right numbers

- [ ] On the phone: Photos → Library, read the *"N Items"* line under the title.
- [ ] Compare with **Library N items** in the app panel. They must match, allowing for
      whatever you shot in between (a screenshot of that screen is itself a new item).
- [ ] **Files** is legitimately higher: a Live Photo is two files. Older edits may also
      have left an `.AAE` sidecar beside their photo, though recent iOS does not write
      one any more. The panel says how many extra parts that is.
- [ ] If the item count is **higher** than Photos: check *Album → Recently Deleted*. A
      deleted photo keeps its file for 30 days and AFC sees files, not the database.
- [ ] If the item count is **lower**: some originals live only in iCloud. Turn off
      *Optimise iPhone Storage*, let them download, press `r`. Anything else is a bug.
- [ ] Press `r` (rescan): the previous numbers stay on screen with a `Scanning…` line, and
      the UI stays responsive throughout.

## 5. Library check (`v`)

Copies the phone's own photo database (~2 GB, about a minute) and compares it with the
scan. Read-only.

- [ ] Press `v`. A progress bar shows the copy, with size and ETA.
- [ ] At the end: *"the Photos app shows N items; K more are in Recently Deleted, H
      hidden"*.
- [ ] **N matches what the Photos app shows.**
- [ ] *"Every item in the library has its file on the device"* — this is the number that
      matters before deleting anything. If instead it lists items with no file, **stop**
      and report it.
- [ ] Delete one photo on the phone (Photos → select → bin), press `r`, then `v` again:
      the "in Recently Deleted" count goes up by one, and the item count in the panel does
      **not** go down. That is expected and documented behaviour.
- [ ] Check nothing is left behind: `ls /var/folders/*/T/ipm-photosdb-* 2>/dev/null` prints
      nothing (the temporary copy is deleted after the check).

## 6. The import dialog does not import

- [ ] Press `i`. A dialog appears **before anything is transferred**: number of files,
      total size, destination folder.
- [ ] Press Escape.
- [ ] The activity log says the import was cancelled, and the folder is still empty:
      `find "$DEST" -type f | head` prints nothing (or only `.ipm/manifest.db`).

## 7. The first import — the long one

- [ ] Press `i` again and confirm.
- [ ] While it runs, watch: current file name, files done/total, bytes, speed, ETA. The
      device panel stays visible and the app stays responsive.
- [ ] At the end: a summary line (copied / skipped / errors / transferred) and **Finder
      opens** at the destination.
- [ ] `copied` matches the file count from the scan, `errors` is **0**.

## 8. What landed on disk

```bash
find "$DEST" -name '*.ipm-part'          # must print nothing
find "$DEST" -type d -maxdepth 2 | head  # YYYY/MM folders
```

- [ ] Folders are `YYYY/MM`, by shot date.
- [ ] A Live Photo is **two files with the same base name in the same folder**
      (`IMG_xxxx.HEIC` + `IMG_xxxx.MOV`).
- [ ] Any RAW/DNG came along, and any `.AAE` sidecars the phone still holds (recent
      iOS writes no new ones, so a library with none is normal).
- [ ] Open a handful of HEIC files in Preview: they open, and look right.
- [ ] File dates in Finder match the shot dates, not today.
- [ ] No `*.ipm-part` anywhere.

**Prove a file is byte-for-byte identical** (this is what the whole tool promises):

```bash
sqlite3 "$DEST/.ipm/manifest.db" \
  "select local_path, size, sha256 from files limit 3"
shasum -a 256 "$DEST/<one of those local_path values>"
```

- [ ] The digest printed by `shasum` equals the `sha256` column for that row.
- [ ] `stat -f%z "$DEST/<that file>"` equals the `size` column.

That digest was computed from the bytes as they arrived from the phone, so a match means
the copy is exactly what the device sent.

## 9. The reset script keeps its promise

Before trusting it with the real folder, watch it work on this one:

- [ ] `scripts/reset-test-state.sh --dry-run`. It lists the `YYYY/` folders, `.ipm/`, the
      config and the log — with file counts and sizes — and removes nothing.
- [ ] Drop a file it should not care about: `echo mine > "$DEST/notes.txt"`.
- [ ] Run `--dry-run` again: `notes.txt` appears under **"Would keep (not created by the
      app)"**.
- [ ] Run it for real without `--yes`, type something other than `RESET`: it cancels and
      removes nothing.
- [ ] Point it at a folder that is not a destination — `scripts/reset-test-state.sh --dest
      ~/Pictures` — and it refuses, because there is no `.ipm/manifest.db` there.

Do **not** actually reset now; you need this import for the tests below. Come back to it
whenever you want a clean slate.

## 10. A lost history rebuilds itself, without downloading anything

The reason `--keep-photos` is safe to use between test rounds:

- [ ] `scripts/reset-test-state.sh --keep-photos`, confirm with `RESET`.
- [ ] Start `ipm`. It asks for the destination folder again (the config is gone) — point
      it back at `~/Desktop/ipm-test`.
- [ ] The "On this Mac" counters are all **zero**: the history really is gone.
- [ ] Plug the phone in, let it scan, press `i`.
- [ ] The dialog says **0 files, 0 B will be copied**, and — the line that matters —
      *"N file(s) are already in that folder. They are checked and added back to the
      history — not downloaded again."* The button reads **Check N file(s)**.
- [ ] Confirm. It finishes in a couple of minutes (it is hashing 32 GB from disk, not
      downloading it): watch the activity, `copied` is **0**, `skipped` is everything.
- [ ] `find "$DEST" -name '*_1.*'` prints nothing — no duplicates.
- [ ] The counters are back where they were, and **Delete** is available again.
- [ ] Spot-check one checksum as in test 8: it matches what it was before the reset.

## 11. Importing again changes nothing

- [ ] Press `i` immediately. The dialog says **"Nothing to import"** (or opens with a
      count of 0) — everything is already here.
- [ ] No duplicates on disk: `find "$DEST" -name '*_1.*' | head` prints nothing.
- [ ] The run finishes in seconds, not minutes.

## 12. New photos arrive — the everyday case

- [ ] Take 2 or 3 new photos with the phone, including **one Live Photo** and **one
      video**.
- [ ] Press `r` (rescan). The item count goes up by exactly the number of new items.
- [ ] Press `i`. The dialog offers **only the new files** — a handful of files and a few
      MB, not the whole library.
- [ ] Confirm. Only those are transferred; the summary reports the rest as skipped.
- [ ] The new photos are in the right `YYYY/MM` folder, the Live Photo as two files.

## 13. Interruption and resume

Do these **during** a transfer. To have time, delete a few dozen local files first so the
next import has real work to do:

```bash
find "$DEST/20*" -type f | head -40 | xargs rm      # local copies only
```

- [ ] Press `i`, confirm, and **unplug the cable** mid-transfer. Expected: a clear
      message, no traceback, the panel empties.
- [ ] Plug it back in, press `i`, confirm: it copies only what was still missing.
- [ ] `find "$DEST" -name '*.ipm-part'` prints nothing.
- [ ] Repeat, this time quitting the app with `q` mid-transfer, then restart and resume.
- [ ] If you can, repeat with the Mac going to sleep (`pmset sleepnow`) mid-transfer.
- [ ] Open the last few files written each time: none is truncated or unreadable.

## 14. A missing local copy is never deletable

- [ ] Note the *Imported & verified* count in the "On this Mac" panel.
- [ ] Delete one imported file in Finder, then press `r`.
- [ ] That file moves from *verified* to *Local copy missing*.
- [ ] Press `d`: the dialog lists it under *"will be kept because their local copy is
      missing or incomplete"*. Press Escape.
- [ ] Press `i`: that one file is re-downloaded, and the counters return to normal.

## 15. The deep check catches a damaged copy — do not skip this

This proves the safety net that everything else depends on, and it deletes nothing: the
scope is one single item, and that item is the one you are about to damage.

The manifest that answers "which file is the newest" is `$DEST/.ipm/manifest.db` — inside
your destination folder, not in the repo. `sqlite3` ships with macOS.

- [ ] Pick the newest imported file. It is the one *Only the most recent → 1* selects, and
      the query only reads — nothing is damaged yet:

```bash
sqlite3 "$DEST/.ipm/manifest.db" \
  "select local_path from files where deleted_from_device_at is null
   order by mtime desc limit 3"
```

- [ ] Now damage that copy, keeping its size byte for byte identical so that only the
      checksum can tell. This block picks the same newest file on its own, so there is no
      path to paste by hand and no chance of hitting the wrong one:

```bash
F="$DEST/$(sqlite3 "$DEST/.ipm/manifest.db" \
  "select local_path from files where deleted_from_device_at is null
   order by mtime desc limit 1")"

echo "$F"                # the file about to be damaged
shasum -a 256 "$F"       # its digest before
python3 -c "import sys,os;p=sys.argv[1];n=os.path.getsize(p);open(p,'wb').write(b'X'*n)" "$F"
shasum -a 256 "$F"       # different digest, same size
ls -l "$F"
```

That file is now wrong on the Mac. The phone still has the original, and the last step of
this test downloads it again — that is the whole point: the app must notice before the
original is at risk.

- [ ] Press `d`, choose **Only the most recent…**, type `1`, leave **"Read every local copy
      back and compare it"** ticked, type `DELETE`, confirm.
- [ ] A progress bar appears for the check itself (files, bytes, speed, ETA).
- [ ] The log reports that **1 local copy did not match**, names it, and says it is kept on
      the iPhone. `Deleted 0 file(s)`.
- [ ] **That photo is still on the phone.** If it was deleted, stop immediately and report:
      that is the exact failure this release exists to prevent.
Then put the damaged copy right again. Pressing `i` on its own will **not** do it: the file
still has exactly the recorded size, so the import plan sees it as already imported and
skips it — the size check is precisely what the deep check exists to backstop. Delete the
bad copy first, so there is something to re-import:

```bash
rm "$F"
```

- [ ] Press `r`, then `i`, and confirm: that one file is transferred again.
- [ ] `shasum -a 256 "$F"` now prints the digest it had before you damaged it — the same
      one stored in the manifest:

```bash
sqlite3 "$DEST/.ipm/manifest.db" \
  "select sha256 from files where local_path = '${F#"$DEST"/}'"
```

> If the corrupted file was one half of a Live Photo, the scope of one item covers both
> halves — the intact half is checked and accepted, the damaged one is refused, and
> neither is deleted, because an item is never deleted by halves.

## 16. The delete dialog, without deleting

- [ ] Press `d`. The dialog shows: item count, file count, total size, and *Everything*
      selected.
- [ ] It says plainly that the photos do **not** go to *Recently Deleted*.
- [ ] The confirm button is **disabled**.
- [ ] Type `delete` in lowercase: still disabled.
- [ ] Type `DELETE`: it becomes clickable and names the number of **items**.
- [ ] Choose **Only the most recent…**, type `1`: the summary line and the button both drop
      to one item — two files if it is a Live Photo — and show its date.
- [ ] Type `0`: *"Nothing selected"*, button disabled again.
- [ ] Type `99999`: the summary says only N are available, and stays at the real total.
- [ ] Press Escape. The log says the deletion was cancelled. **Nothing has been removed.**

## 16b. Build the test photo — do this before test 17

Test 17 asks whether the iPhone, deleting a photo, also takes the parts that have no
library item of their own and cannot be deleted directly — the whole design assumes they
ride along with the photo they belong to.

There are two such parts, and they are in different states:

* the **video half of a Live Photo**: watched leaving with its still, once, on an
  iPhone 13. Worth watching again on your model, and easy to arrange — that is what this
  check builds;
* the **`.AAE` sidecar**: still unwatched, and **not arrangeable**. On recent iOS,
  editing a photo no longer writes a sidecar next to the original at all; the edit goes
  to `PhotoData/Mutations/`, which this app does not import. So there is no way to make a
  fresh `.AAE` to sacrifice. If your phone still holds old ones from before that change,
  they belong to real photos — do not delete one to find out. The question answers itself
  the first time a deletion happens to include one, because anything left behind is
  reported in the summary and named.

So this check builds a Live Photo, and nothing else. Make one on purpose:

1. **On the iPhone, open the Camera app.** At the top, the Live Photo button (concentric
   circles) must be **on** — not crossed out with a slash.
2. **Take one photo.** Something disposable: a wall, your desk. This gives you
   `IMG_xxxx.HEIC` plus `IMG_xxxx.MOV`.
3. **Note the file name.** In Photos, swipe up on the photo (or tap the ⓘ button) — the
   name `IMG_xxxx` is shown in the info panel.

   *(Do not bother editing it to produce an `.AAE`. On this iOS an edit writes nothing
   beside the original — see above.)*

Now bring it over so the app has a verified copy of both files:

```bash
# In the app: press r to rescan, then i to import.
# Then, in your second terminal, confirm both halves arrived:
ls -l "$DEST"/*/*/IMG_xxxx.*        # replace IMG_xxxx with the real name
```

- [ ] Two files are listed: `.HEIC` and `.MOV`, both in the same `YYYY/MM` folder.
- [ ] The app's item count went up by exactly **one**, not two — two files, one item.

*(If there is no `.MOV`, Live Photos was off when you took the shot. Check the icon at
the top of the Camera app and take another.)*

## 17. The one-item trial — the first real deletion

**This deletes from the phone. Use the photo you made in 16b, which you do not care about.**

There is a scripted version of this check now. It does the same thing, measures the
result instead of asking you to read it off the screen, and can only ever touch photos
you have named by hand in `tests/hardware/sacrificial.txt`:

```bash
cp tests/hardware/sacrificial.txt.example tests/hardware/sacrificial.txt
# put its local path in it -- the one you see in the Finder, e.g. 2026/09/IMG_xxxx.HEIC
.venv/bin/python -m pytest -m hardware --allow-delete -q tests/hardware/test_sacrificial_deletion.py
```

Run whichever you prefer — but do the checks on the phone below either way, because the
Photos app being left clean is the one thing no script can see.

**Prerequisites, not suggestions:** test **8** (the copies are byte-identical), test **14**
(a missing copy is refused) and test **15** (a damaged copy is refused). Those three are
what makes a deletion here an informed act rather than a hope. If any of them is still
unticked, go back and run it — between them they cost under ten minutes.

- [ ] The first time you press `d`, the confirmation comes first; after you have typed
      `DELETE` there, a second dialog warns that deleted photos do **not** go to Recently
      Deleted. It does **not** ask for the word again — one `DELETE` per deletion — and
      focus starts on **Cancel**. Tick **Do not show this again**, then check it really
      stays away: quit with `q`, start `ipm`, press `d`, and after the confirmation it
      should go straight to the deletion.
- [ ] The photo from 16b is the most recent thing on the phone. If you have taken others
      since, it is no longer the newest — take the count you need, or just retake it.
- [ ] Press `d`, choose **Only the most recent**, `1`, leave the deep check ticked, type
      `DELETE`, confirm.
- [ ] The summary line says it is opening the iPhone's photo library, and reports items as
      they are catalogued. On a big library this takes ten to twenty seconds.
- [ ] The deep check runs, then the deletion.
- [ ] The summary reports **1 item(s)**, the freed bytes, and **kept 0**.
- [ ] **Nothing is listed as "still on the iPhone although their photo was deleted".** If
      something is, that is the answer to the open question — write down exactly which
      files, and stop here rather than running test 18.
- [ ] The app rescans by itself, and the item count drops by one.
- [ ] **On the Mac: the local copies are untouched.** Both files are still in `$DEST`,
      and the photo still opens.

Now look at the phone — this is the behaviour the photo-service route exists to fix:

- [ ] The photo is **gone from the library**. No placeholder, no broken thumbnail, no
      warning badge.
- [ ] It is **not** in *Album → Recently Deleted* either. Measured on an iPhone 13 /
      iOS 26.6: the photo service removes items outright, unlike deleting in the Photos
      app. If yours *does* appear there, say so — it would mean the behaviour differs by
      iOS version, and the warning dialog would need rewording.
- [ ] Press `v` in the app: it does **not** report any library item without a file.

```bash
# And on the Mac, confirm the sidecar and the half really went with it:
# (the app's own rescan already checked this, but seeing it is worth a moment)
```

- [ ] Press `i`: the deleted files are **not** re-downloaded (their rows say they were
      removed on purpose).
- [ ] The "On this Mac" panel shows them under *Already deleted*.
- [ ] **Did the log say `The iPhone ignored the first request, so it was sent again.`?**
      Write it down either way — yes or no. It means the phone accepted the deletion,
      reported no error, did nothing, and the app noticed and asked once more. It is
      expected to be rare and it is not a failure, but it is the behaviour least
      understood on this side, so every observation of it is worth having.

## 18. A larger batch — only if 17 went perfectly

**Still deleting for real.** If 17 left anything behind, do not run this.

- [ ] Press `d`, choose **Only the most recent**, `10`, deep check on, confirm.
- [ ] 10 items go, with their Live Photo halves and sidecars. Kept is 0, nothing is
      reported as left behind.
- [ ] The Photos app shows 10 fewer items, none of them lingering as placeholders, and
      none of them in *Recently Deleted* — the photo service removes them outright.
- [ ] Press `r` and `v`: the counts agree with what the Photos app now shows.
- [ ] **Unplug the cable and press `d`.** The Delete button greys out and nothing
      happens — no traceback, and nothing is deleted.
- [ ] If the summary says some items **were not removed** (the iPhone ignored them —
      Live Photos, on iOS 26.7.1), press `d` again: they go on the second request.

### 18b. The retry, on a run that also keeps things back

**Read this before running 18 on a large selection.** Until now the "ask once more"
behaviour could only trigger on a selection where *nothing at all* was held back. On a
real library that never happens: a handful of items always have no library entry, or an
ambiguous one, and are kept and explained. The rule was corrected so that only an
objection *from the phone* stops the second attempt -- items we ourselves decided to keep
no longer do.

That correction met a real iPhone on 2026-10-02 (iOS 26.7.1): the second request went
out on a run that also kept three items back, as intended. It cannot delete anything the
first request could not: the identifiers are the same, the files were seen still present by the scan in
between, and the phone is scanned again afterwards. But it changes when a destructive
request is repeated, so it is worth watching once.

- [ ] Run a deletion whose summary reports **both** some items deleted **and** some kept
      (`kept` greater than 0). A selection of 10 on a real library usually does this by
      itself; if not, `Everything` will.
- [ ] The kept items are listed with a reason, and are **still on the phone** afterwards.
- [ ] If the retry line appears, the items that finally went are exactly the ones that
      were requested — nothing that was listed as kept was removed. Check two of them by
      name in the Photos app.
- [ ] Nothing is reported under *"still on the iPhone although their photo was deleted"*.

### 18c. An edited photo is never deleted

**Removes nothing.** Edits are not imported yet, so a photo edited on the phone must
stay there: deleting it would lose the edit.

- [ ] Take a throwaway photo, edit it in the Photos app (a crop is enough), then press
      `r` and `i` to import it.
- [ ] The log says **"1 of them were edited on the iPhone"** — or the number of photos
      you have edited in total, if there were some already.
- [ ] Press `d`. The dialog lists the photo under **"were edited on the iPhone"**, with
      its file name, and the selection summary does not count it. Press Escape.
- [ ] If you run a deletion in the same session (18 or 18b), the edited photo is
      **still on the phone afterwards**, with its edit.

## 19. The app notices a library that changed underneath

You do not need to erase your phone to test this. Make the manifest disagree with the
phone on purpose — this edits only the app's own database, never a photo:

```bash
sqlite3 "$DEST/.ipm/manifest.db" \
  "update files set mtime = mtime - 86400 where device_path in
   (select device_path from files where deleted_from_device_at is null limit 3)"
```

- [ ] Press `r`. A dialog appears: *"This iPhone is not the one this folder remembers"*,
      listing 3 files, explaining the erase-and-restore case.
- [ ] Choose **Leave it as it is**. Press `d`: those 3 files are listed as *"no longer the
      ones that were imported"* and are **excluded** from the deletion. Escape.
- [ ] Press `r` again, and this time choose **Tidy up the history**.
- [ ] The log reports the rows forgotten, and says no file was touched.
- [ ] Confirm that for real: the 3 photos are still on the phone, and their copies are
      still in the destination folder.
- [ ] Press `i`: those 3 are imported again as new files (`_1` suffix, since the originals
      are still there under the same names).

## 20. Your own archive is safe

The destination will eventually be your real photo folder, so prove the app leaves
strangers alone:

```bash
mkdir -p "$DEST/2019/07"
echo "not from the phone" > "$DEST/2019/07/scan_holidays.jpg"
echo "mine" > "$DEST/notes.txt"
```

- [ ] Press `i`, then `r`. Both files are still there, unchanged.
- [ ] `sqlite3 "$DEST/.ipm/manifest.db" "select count(*) from files where local_path like
      '%scan_holidays%' or local_path like '%notes%'"` prints **0** — the app does not
      track them, so the delete button can never see them.

## 21. Two folders, one phone

- [ ] Press `f` and point the app at a brand new folder, e.g. `~/Desktop/ipm-test-2`.
- [ ] The log says **"First time this iPhone is used with this folder"** — the folder, not
      the phone, is what has no history.
- [ ] The counters reset to zero and **Delete** greys out: with no import history there is
      nothing it could offer to delete.
- [ ] Point it back at `~/Desktop/ipm-test`: the old counters come back exactly as they
      were.
- [ ] `rm -rf ~/Desktop/ipm-test-2`.

---

## When you are done

- [ ] `scripts/reset-test-state.sh --dest ~/Desktop/ipm-test` to clear the scratch folder,
      then `rmdir ~/Desktop/ipm-test` if you want it gone entirely.
- [ ] No `*.ipm-part` files anywhere: `find "$DEST" -name '*.ipm-part'`.
- [ ] The log has no tracebacks: `grep -c Traceback ~/.local/state/iphone-photo-manager/ipm.log`
      prints 0.
- [ ] Quit with `q`. The app exits cleanly, the terminal is usable.

Then say which tests passed, and for anything that did not:

* what you did, and what happened instead;
* the summary line from the app;
* the tail of `~/.local/state/iphone-photo-manager/ipm.log`;
* the iPhone model and iOS version, and your macOS version;
* whether any `*.ipm-part` file was left behind.

Test **17**'s observations are the ones to write down even when everything works: whether
the `.MOV` half left with the photo, whether anything was reported as left behind, and
whether the Photos app was clean afterwards. That is the behaviour this rests on and the
only part of it that cannot be tested without a phone, so the answer belongs in the README
either way. If a deletion ever does include an old `.AAE`, say so — that is the one
question still open, and it can only be answered by accident.

Say as well whether `The iPhone ignored the first request, so it was sent again.` appeared,
and on what kind of run. That path has been reasoned about and unit-tested, and no phone
has ever met the current version of it.

Only after this checklist is `develop` merged into `main`.
