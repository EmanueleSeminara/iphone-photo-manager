#!/usr/bin/env bash
#
# Does the test suite actually notice when a safety rule breaks?
#
# A green suite proves nothing on its own: tests that assert the wrong thing, or that
# never reach the code they claim to cover, are green too. So this breaks one rule at a
# time, on purpose, and checks that the suite goes red. A rule reported as MISSED is a
# hole: something the application promises and nothing verifies.
#
#   scripts/mutation-check.sh
#
# Each mutation is applied, the suite is run with -x (stop at the first failure, so a
# caught mutation costs seconds rather than a full run), and the file is restored. The
# restore also happens on Ctrl-C, so an interrupted run does not leave broken source
# behind -- but if the machine dies mid-run, check `git status` before doing anything
# else.
#
# This is not run in CI. It takes a minute and it is a periodic sanity check on the
# tests, not a gate on a change.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PYTHON="${IPM_PYTHON:-.venv/bin/python}"

if [[ ! -x "$PYTHON" ]]; then
    echo "No interpreter at $PYTHON." >&2
    exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "The working tree has uncommitted changes." >&2
    echo "This script edits source files in place and restores them; run it on a clean tree" >&2
    echo "so that a crash cannot be confused with your own work." >&2
    exit 1
fi

if [[ -t 1 ]]; then
    GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; OFF=$'\033[0m'
else
    GREEN=""; RED=""; DIM=""; OFF=""
fi

# name | file | text to replace | replacement
# Each one is a rule the application promises, phrased as the bug that would break it.
#
# The last one is deliberately written on a *local* variable rather than on
# `self._afc`: the guard in tests/test_readonly.py used to recognise only the second
# shape, so a deletion inside connect() -- the one method that necessarily holds the
# service in a local -- passed every test in the file. Keeping the mutation in that
# shape is what stops the guard being narrowed back.
MUTATIONS=(
"a missing or damaged local copy no longer stops a deletion|src/ipm/core/deleter.py|            if not await asyncio.to_thread(local_copy_is_intact, record, self.destination):|            if False:"
"the deep check accepts any digest|src/ipm/core/deleter.py|        elif digest == record.sha256:|        elif True:"
"a device file that changed is still considered the imported one|src/ipm/models.py|    def describes(self|    def describes_DISABLED(self"
"adoption matches on size alone, ignoring the modification time|src/ipm/core/organizer.py|    return abs(existing.st_mtime - remote.modified.timestamp()) <= MTIME_TOLERANCE|    return True"
"a Live Photo's halves are no longer grouped into one item|src/ipm/core/library.py|def item_key(|def item_key_DISABLED("
"a write to the phone slips into the AFC layer through a local|src/ipm/device/afc.py|        afc = AfcService(lockdown=lockdown)|        afc = AfcService(lockdown=lockdown); await afc.rm(DCIM_ROOT)"
"a photo edited on the phone is offered for deletion|src/ipm/core/deleter.py|        if edited_items is not None and item_key(record.device_path) in edited_items:|        if False:"
"the last gate lets an edited photo through|src/ipm/core/deleter.py|            if self.edited_items is not None and item_key(record.device_path) in self.edited_items:|            if False:"
"the walk that finds the edits reports none|src/ipm/device/afc.py|        await walk(MUTATIONS_ROOT)|        pass"
"an unreadable edits folder passes for an empty one|src/ipm/device/afc.py|                if _is_not_found(exc):|                if True:"
"the most recent items are chosen by modification time again|src/ipm/core/items.py|            times[item.path] = min(item.created.timestamp(), item.modified.timestamp())|            times[item.path] = item.modified.timestamp()"
)

# Some mutations rename a function; the caller then needs something to call, and an
# always-permissive stub is exactly the bug being modelled.
stub_for() {
    case "$1" in
        *models.py)    printf '\n    def describes(self, remote: object) -> bool:\n        return True\n' ;;
        *library.py)   printf '\n\ndef item_key(path: str) -> str:\n    return path\n' ;;
        *)             printf '' ;;
    esac
}

CAUGHT=0
MISSED=0
MISSED_NAMES=()
RESTORE_FILE=""
BACKUP=""

# The original is kept as a file, not in a variable: $(cat file) drops trailing
# newlines, and restoring a file one byte short of what it was is exactly the kind of
# silent damage this script must not do.
restore() {
    if [[ -n "$RESTORE_FILE" && -f "$BACKUP" ]]; then
        cp "$BACKUP" "$RESTORE_FILE"
        RESTORE_FILE=""
    fi
    [[ -n "$BACKUP" ]] && rm -f "$BACKUP"
    BACKUP=""
}
trap 'restore; echo; echo "interrupted — source restored"; exit 130' INT TERM

for entry in "${MUTATIONS[@]}"; do
    IFS='|' read -r name file old new <<< "$entry"
    if [[ ! -f "$file" ]]; then
        echo "${RED}SKIP  ${OFF} $name ${DIM}($file is gone)${OFF}"
        continue
    fi
    BACKUP="$(mktemp -t ipm-mutation)"
    cp "$file" "$BACKUP"
    RESTORE_FILE="$file"

    if ! grep -qF -- "$old" "$file"; then
        echo "${RED}STALE ${OFF} $name ${DIM}(the code it mutates has changed; fix this script)${OFF}"
        restore
        continue
    fi

    # Replace only the first occurrence, then append a stub if the mutation renamed
    # something the rest of the code calls.
    "$PYTHON" - "$file" "$old" "$new" <<'PY'
import sys
from pathlib import Path
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
p = Path(path)
p.write_text(p.read_text().replace(old, new, 1))
PY
    stub_for "$file" >> "$file"

    if "$PYTHON" -m pytest -x -q --no-header -p no:cacheprovider > /dev/null 2>&1; then
        MISSED=$((MISSED + 1))
        MISSED_NAMES+=("$name")
        echo "${RED}MISSED${OFF} $name"
    else
        CAUGHT=$((CAUGHT + 1))
        echo "${GREEN}caught${OFF} $name"
    fi
    restore
done

echo
echo "${CAUGHT} caught, ${MISSED} missed."
if (( MISSED )); then
    echo
    echo "${RED}Nothing in the suite notices these:${OFF}"
    for name in "${MISSED_NAMES[@]}"; do echo "  · $name"; done
    echo
    echo "Each one is a promise the application makes and no test checks."
    exit 1
fi
echo "${GREEN}Every rule that was broken on purpose was noticed.${OFF}"
