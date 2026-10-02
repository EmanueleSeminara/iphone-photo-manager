#!/usr/bin/env bash
#
# Everything this project can check, in one command.
#
#   scripts/check.sh                  lint, types, and every test that needs no iPhone
#   scripts/check.sh --hardware       ... plus the read-only tests against a real phone
#   scripts/check.sh --hardware --delete
#                                     ... plus the one test that removes a photo
#
# The three layers cost different things. The default is free and runs in under a
# minute. --hardware needs a cable and a couple of minutes. --delete removes a photo
# from a real iPhone, so it asks first, and it can only ever touch the files named by
# hand in tests/hardware/sacrificial.txt.
#
# Every step runs even if an earlier one failed: a run that stops at the first error
# tells you less than a run that tells you everything that is wrong.
#
# Every run is written to .check-logs/ (git-ignored): the full output, a one-screen
# summary, and pytest's JUnit XML for anything that wants to read the results rather
# than look at them. The most recent run is always .check-logs/latest.log,
# latest-summary.txt and latest-pytest.xml.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PYTHON="${IPM_PYTHON:-.venv/bin/python}"
RUFF="${IPM_RUFF:-.venv/bin/ruff}"
LOG_DIR="${IPM_LOG_DIR:-.check-logs}"
KEEP_RUNS=20

if [[ ! -x "$PYTHON" ]]; then
    echo "No interpreter at $PYTHON."
    echo "Create one with:"
    echo "    uv venv --python 3.13 .venv"
    echo "    uv pip install --python .venv/bin/python -e \".[dev]\""
    exit 1
fi

usage() {
    cat <<'HELP'
scripts/check.sh — every check this project has, in one command.

USAGE
  scripts/check.sh                     lint, types, and every test that needs no iPhone
  scripts/check.sh --hardware          ... plus the read-only tests against a real phone
  scripts/check.sh --hardware --delete ... plus the one test that removes a photo
  scripts/check.sh --help              this text

WHAT EACH LEVEL COSTS
  (default)   ~70 s, no hardware, safe anywhere. ruff + mypy + the whole suite.
  --hardware  ~2-3 min. Needs the iPhone plugged in and unlocked, and .ipm-hw.env
              filled in. Reads only: nothing is written to the phone.
              Turn Auto-Lock off first (Settings > Display & Brightness):
              scanning a big library takes long enough that the phone locks
              itself, and the photo-library tests then skip.
  --delete    Removes photos from a real iPhone. Asks for the word DELETE first,
              and can only touch the files named in tests/hardware/sacrificial.txt.
              Requires --hardware.

FILES YOU EDIT
  .ipm-hw.env                       which phone the hardware tests may use.
                                    Copy from .ipm-hw.env.example. Git-ignored.
  tests/hardware/sacrificial.txt    the photos --delete is allowed to remove, one
                                    file name per line (IMG_6601.HEIC). Copy from
                                    tests/hardware/sacrificial.txt.example.
                                    Git-ignored. Missing or empty = nothing runs.

SETTINGS INSIDE .ipm-hw.env  (environment variables of the same name win)
  IPM_HW_UDID               required. The one phone the suite may talk to.
  IPM_HW_DESTINATION        the folder holding the verified copies. Without it,
                            the tests that plan a deletion skip.
  IPM_HW_DESTRUCTIVE_UDID   the phone that may lose photos. Must repeat
                            IPM_HW_UDID. Leave empty except for a --delete run.
  IPM_HW_PROTECTED_UDIDS    comma-separated phones that may never be deleted from.
                            Overrides everything, including --delete.

WHERE THE RESULTS GO  (.check-logs/, git-ignored, last 20 runs kept)
  .check-logs/latest-summary.txt    one screen: mode, commit, one line per step
  .check-logs/latest.log            the full output of the last run
  .check-logs/latest-pytest.xml     JUnit XML, for reading results rather than
                                    scrolling them

OTHER SCRIPTS
  scripts/mutation-check.sh   breaks one safety rule at a time and fails if the
                              suite does not notice. Run on a clean tree, ~1 min.
  scripts/reset-test-state.sh puts a machine back to "never ran this app".

THE RAW COMMANDS, if you want one at a time
  .venv/bin/ruff check src tests
  .venv/bin/python -m mypy
  .venv/bin/python -m pytest -q                      # hardware excluded by default
  .venv/bin/python -m pytest -m hardware -v -rs      # read-only, needs a phone
  .venv/bin/python -m pytest -m hardware --allow-delete -q \
      tests/hardware/test_sacrificial_deletion.py    # deletes
  .venv/bin/python -m pytest -q tests/test_manual_checklist.py   # one file
  .venv/bin/python -m pytest -q -k "delete"                      # by name
  .venv/bin/python -m pytest -q -x --lf                          # last failures first
HELP
}

WITH_HARDWARE=0
WITH_DELETE=0
for arg in "$@"; do
    case "$arg" in
        --hardware) WITH_HARDWARE=1 ;;
        --delete)   WITH_DELETE=1 ;;
        -h|--help)  usage; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)"; exit 2 ;;
    esac
done

if (( WITH_DELETE && ! WITH_HARDWARE )); then
    echo "--delete only makes sense with --hardware." >&2
    exit 2
fi

MODE="local"
(( WITH_HARDWARE )) && MODE="hardware"
(( WITH_DELETE )) && MODE="hardware-delete"

mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
RUN_LOG="$LOG_DIR/$STAMP-$MODE.log"
JUNIT="$LOG_DIR/$STAMP-pytest.xml"

# Fixed names for the most recent run, so anything reading the results does not have to
# work out which timestamp is newest. Symlinks rather than copies: the log is still being
# written while the run goes on, and a copy taken at the end would race the flush.
ln -sf "$(basename "$RUN_LOG")" "$LOG_DIR/latest.log"
ln -sf "$(basename "$JUNIT")" "$LOG_DIR/latest-pytest.xml"

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'
    DIM=$'\033[2m'; OFF=$'\033[0m'
else
    BOLD=""; GREEN=""; RED=""; YELLOW=""; DIM=""; OFF=""
fi

FAILED=()
PASSED=()
PARTIAL=()
SKIPPED=()
FAIL_COUNT=0

# Everything printed goes to the terminal and to the run log at the same time, so the
# log is exactly what you saw. `exec` here rather than piping each command keeps the
# steps' own exit codes intact.
exec > >(tee "$RUN_LOG") 2>&1

printf '%s\n\n' "${DIM}$(date '+%Y-%m-%d %H:%M:%S') · mode: $MODE · $($PYTHON -V 2>&1)${OFF}"

run_step() {
    local name="$1"; shift
    printf '%s\n' "${BOLD}── ${name}${OFF}"
    local started=$SECONDS
    if "$@"; then
        PASSED+=("$name")
        printf '%s\n\n' "${GREEN}ok${OFF} ${DIM}(${name}, $((SECONDS - started))s)${OFF}"
    else
        FAILED+=("$name")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        printf '%s\n\n' "${RED}FAILED${OFF} ${DIM}(${name}, $((SECONDS - started))s)${OFF}"
    fi
}

# pytest exits 0 when every test skipped, which for the hardware suites is the most
# misleading answer available: "Everything passed" while the phone was never touched.
# So the output is read, and a run where nothing actually ran says so.
run_pytest_step() {
    local name="$1"; shift
    printf '%s\n' "${BOLD}── ${name}${OFF}"
    local started=$SECONDS
    local log
    log="$(mktemp -t ipm-check)"
    local status=0
    "$@" > >(tee "$log") 2>&1 || status=1
    wait
    if grep -qE '[0-9]+ (failed|error)' "$log"; then
        status=1
    fi
    local skipped ran
    skipped="$(grep -oE '[0-9]+ skipped' "$log" | tail -1 | cut -d' ' -f1)"
    skipped="${skipped:-0}"
    # pytest prints "collected N / M deselected / K selected" at the top and only a
    # count at the bottom, so a reader sees "deselected" and reasonably wonders what
    # was left out. Report what *ran* instead; the other half runs in the other step.
    ran="$(grep -oE '[0-9]+ passed' "$log" | tail -1 | cut -d' ' -f1)"
    ran="${ran:-0}"
    name="$name — ${ran} passed"
    if (( skipped )); then
        name="$name, ${skipped} skipped"
    fi
    if (( status )); then
        FAILED+=("$name")
        FAIL_COUNT=$((FAIL_COUNT + 1))
        printf '%s\n\n' "${RED}FAILED${OFF} ${DIM}(${name}, $((SECONDS - started))s)${OFF}"
    elif ! grep -q '[0-9] passed' "$log"; then
        SKIPPED+=("$name")
        printf '%s\n\n' "${DIM}nothing ran — see the skip reasons above${OFF}"
    elif (( skipped )); then
        # Some ran and some did not. Reported apart from a clean pass on purpose: a
        # hardware run where one test passed and thirteen skipped is not a pass, and
        # calling it one is how you come to believe the phone was tested.
        PARTIAL+=("$name")
        printf '%s\n\n' "${YELLOW}partial${OFF} ${DIM}(${name}, $skipped skipped, $((SECONDS - started))s)${OFF}"
    else
        PASSED+=("$name")
        printf '%s\n\n' "${GREEN}ok${OFF} ${DIM}(${name}, $((SECONDS - started))s)${OFF}"
    fi
    rm -f "$log"
}

run_step "lint (ruff)"  "$RUFF" check src tests
run_step "types (mypy)" "$PYTHON" -m mypy

run_pytest_step "tests (no iPhone)" \
    "$PYTHON" -m pytest -q -rs --junitxml="$JUNIT"

if (( WITH_HARDWARE )); then
    # -rs prints the reason for every skip: with hardware tests "skipped" almost always
    # means something is not plugged in or not configured, and that is the information
    # you actually wanted.
    # -v rather than -q: a scan of a big library takes twenty seconds and the
    # catalogue another twenty, and a row of dots that has not moved for a minute
    # looks exactly like a hang. With -v the name of the test being run is on screen.
    run_pytest_step "tests (iPhone, read-only)" \
        "$PYTHON" -m pytest -m hardware -v -rs \
        --junitxml="$LOG_DIR/$STAMP-pytest-hardware.xml" \
        --ignore=tests/hardware/test_sacrificial_deletion.py
fi

if (( WITH_DELETE )); then
    printf '%s\n' "${BOLD}── tests (iPhone, DELETES A PHOTO)${OFF}"
    cat <<'WARN'
This removes photos from a real iPhone. Only the items named in
tests/hardware/sacrificial.txt can go, at most three of them, and each one is
checked against its verified local copy first. They do NOT land in Recently
Deleted: the photo service removes items outright, so the copy in your
destination folder is the only one that will be left. Offer up nothing you
would miss.

The phone must be unlocked and on the home screen.
WARN
    printf '%s' "Type DELETE to run it, anything else to skip: "
    read -r answer
    if [[ "$answer" == "DELETE" ]]; then
        run_pytest_step "tests (iPhone, destructive)" \
            "$PYTHON" -m pytest -m hardware --allow-delete -v -rs \
            --junitxml="$LOG_DIR/$STAMP-pytest-destructive.xml" \
            tests/hardware/test_sacrificial_deletion.py
    else
        SKIPPED+=("tests (iPhone, destructive)")
        printf '%s\n\n' "${DIM}skipped — nothing was deleted${OFF}"
    fi
fi

# The ${a[@]+"${a[@]}"} dance is for the bash 3.2 that ships with macOS: under `set -u`
# it treats an empty array as unset and aborts.
SUMMARY="$LOG_DIR/$STAMP-summary.txt"
{
    echo "run:    $STAMP"
    echo "mode:   $MODE"
    echo "python: $($PYTHON -V 2>&1)"
    echo "commit: $(git rev-parse --short HEAD 2>/dev/null || echo unknown) on $(git branch --show-current 2>/dev/null || echo unknown)"
    echo
    for name in ${PASSED[@]+"${PASSED[@]}"};  do echo "ok       $name"; done
    for name in ${PARTIAL[@]+"${PARTIAL[@]}"}; do echo "partial  $name"; done
    for name in ${SKIPPED[@]+"${SKIPPED[@]}"}; do echo "none     $name"; done
    for name in ${FAILED[@]+"${FAILED[@]}"};  do echo "FAILED   $name"; done
    if (( WITH_HARDWARE && ! WITH_DELETE )); then
        echo "not run  tests (iPhone, destructive) — needs --delete"
    fi
    echo
    if (( FAIL_COUNT )); then
        echo "result:  $FAIL_COUNT step(s) failed"
    elif [[ -n "${SKIPPED[*]:-}" || -n "${PARTIAL[*]:-}" ]]; then
        echo "result:  nothing failed, but not everything ran"
    else
        echo "result:  everything passed"
    fi
} > "$SUMMARY"

printf '%s\n' "${BOLD}── summary${OFF}"
for name in ${PASSED[@]+"${PASSED[@]}"};   do printf '  %s %s\n' "${GREEN}ok     ${OFF}" "$name"; done
for name in ${PARTIAL[@]+"${PARTIAL[@]}"}; do printf '  %s %s\n' "${YELLOW}partial${OFF}" "$name"; done
for name in ${SKIPPED[@]+"${SKIPPED[@]}"}; do printf '  %s %s\n' "${DIM}none   ${OFF}" "$name"; done
for name in ${FAILED[@]+"${FAILED[@]}"};   do printf '  %s %s\n' "${RED}FAILED ${OFF}" "$name"; done
if (( WITH_HARDWARE && ! WITH_DELETE )); then
    printf '  %s %s\n' "${DIM}not run${OFF}" \
        "tests (iPhone, destructive) ${DIM}— needs --delete${OFF}"
fi

cp "$SUMMARY" "$LOG_DIR/latest-summary.txt" 2>/dev/null

# Keep the last few runs and no more; these are working notes, not an archive.
ls -1t "$LOG_DIR"/*-summary.txt 2>/dev/null | tail -n +$((KEEP_RUNS + 1)) | while read -r old; do
    rm -f "${old%-summary.txt}"*
done

printf '\n%s\n' "${DIM}log: $LOG_DIR/latest.log · summary: $LOG_DIR/latest-summary.txt${OFF}"

if (( FAIL_COUNT )); then
    printf '%s\n' "${RED}${FAIL_COUNT} step(s) failed.${OFF}"
    exit 1
fi
if [[ -n "${SKIPPED[*]:-}" || -n "${PARTIAL[*]:-}" ]]; then
    printf '%s\n' "${YELLOW}Nothing failed, but not everything ran.${OFF} Check the skip reasons above."
else
    printf '%s\n' "${GREEN}Everything passed.${OFF}"
fi
