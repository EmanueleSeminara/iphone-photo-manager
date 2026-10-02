#!/usr/bin/env bash
#
# Put a test machine back to "never ran this app before".
#
# Removes, after showing exactly what it found and asking:
#   <destination>/YYYY/          the year folders the app created
#   <destination>/*.ipm-part     leftovers from an interrupted transfer
#   <destination>/.ipm/          the import manifest (history + checksums)
#   ~/.config/iphone-photo-manager/config.json
#   ~/.local/state/iphone-photo-manager/ipm.log
#
# It never touches the iPhone, and inside the destination it only ever removes
# four-digit year folders, .ipm-part files and .ipm/ -- exactly what the app
# creates. Anything else you keep in that folder is left alone, the same promise
# the app itself makes.
#
# Usage:
#   scripts/reset-test-state.sh [--dest PATH] [--keep-photos] [--dry-run] [--yes]
#
#   --dest PATH     folder to clean; default: the one in the app's config file
#   --keep-photos   remove the manifest, config and log, but keep the copied
#                   photos -- the state that makes the next import re-adopt them
#   --dry-run       show what would be removed, remove nothing
#   --yes           skip the typed confirmation (for scripts and tests)

set -euo pipefail

CONFIG_DIR="${IPM_CONFIG_DIR:-$HOME/.config/iphone-photo-manager}"
CONFIG_FILE="$CONFIG_DIR/config.json"
LOG_FILE="${IPM_LOG_FILE:-$HOME/.local/state/iphone-photo-manager/ipm.log}"
CONFIRM_WORD="RESET"

DEST=""
KEEP_PHOTOS=0
DRY_RUN=0
ASSUME_YES=0

die() {
    printf 'error: %s\n' "$1" >&2
    exit 1
}

usage() {
    sed -n '3,26p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dest) [ $# -ge 2 ] || die "--dest needs a path"; DEST="$2"; shift 2 ;;
        --dest=*) DEST="${1#--dest=}"; shift ;;
        --keep-photos) KEEP_PHOTOS=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --log-file) [ $# -ge 2 ] || die "--log-file needs a path"; LOG_FILE="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) printf 'error: unknown option %s\n\n' "$1" >&2; usage 1 ;;
    esac
done

# -- work out which folder we are talking about -------------------------------

if [ -z "$DEST" ] && [ -f "$CONFIG_FILE" ]; then
    # The config is written by json.dumps(indent=2), so the value sits alone on
    # its line. Anything more clever would need a JSON parser for one field.
    DEST=$(sed -n 's/^[[:space:]]*"destination"[[:space:]]*:[[:space:]]*"\(.*\)".*$/\1/p' \
        "$CONFIG_FILE" | head -n 1)
fi

if [ -z "$DEST" ]; then
    die "no destination folder given, and none found in $CONFIG_FILE
       pass one explicitly:  scripts/reset-test-state.sh --dest ~/Desktop/ipm-test"
fi

case "$DEST" in
    "~"|"~/"*) DEST="$HOME${DEST#\~}" ;;
esac

# -- refuse to be pointed at something that is not a destination folder --------

if [ -e "$DEST" ] && [ ! -d "$DEST" ]; then
    die "$DEST is not a folder"
fi
case "$DEST" in
    /|"$HOME"|"$HOME"/) die "refusing to clean $DEST" ;;
esac
if [ -d "$DEST" ] && [ ! -e "$DEST/.ipm/manifest.db" ] && [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
    die "$DEST has files in it but no .ipm/manifest.db, so it was not made by this app.
       Refusing to touch it. Pass --dest explicitly if this really is the test folder."
fi

# -- collect what would go -----------------------------------------------------

YEAR_DIRS=""
PART_FILES=""
if [ -d "$DEST" ] && [ "$KEEP_PHOTOS" -eq 0 ]; then
    YEAR_DIRS=$(find "$DEST" -maxdepth 1 -type d -regex '.*/[12][0-9][0-9][0-9]$' 2>/dev/null | sort)
    PART_FILES=$(find "$DEST" -name '*.ipm-part' -type f 2>/dev/null | sort)
fi

printf '\nReset iphone-photo-manager test state\n\n'
printf '  destination : %s\n' "$DEST"

removals=0

# -- the photos, kept deliberately separate from everything else ---------------
#
# This is the part that costs 16 minutes and a cable to undo, so it gets its own
# heading, its own total and its own warning -- reading this block as "only the
# app's own files" is the one misreading that would hurt.

if [ "$KEEP_PHOTOS" -eq 1 ]; then
    printf '\nPhotos and videos: KEPT (--keep-photos)\n'
elif [ -n "$YEAR_DIRS" ]; then
    total_files=0
    total_size=$(echo "$YEAR_DIRS" | tr '\n' '\0' | xargs -0 du -ch 2>/dev/null \
        | tail -n 1 | cut -f1)
    printf '\n>>> PHOTOS AND VIDEOS THAT WOULD BE DELETED  (%s)\n\n' "${total_size:-?}"
    while IFS= read -r dir; do
        [ -n "$dir" ] || continue
        # Partials are not photos, and they are already listed under app state.
        count=$(find "$dir" -type f ! -name '*.ipm-part' 2>/dev/null | wc -l | tr -d ' ')
        size=$(du -sh "$dir" 2>/dev/null | cut -f1)
        total_files=$((total_files + count))
        printf '  %-46s %6s files, %s\n' "${dir#"$DEST"/}/" "$count" "$size"
    done <<EOF
$YEAR_DIRS
EOF
    printf '  %-46s %6s files\n' "in total" "$total_files"
    printf '\n  These are the copies on this Mac, not the app'\''s own files.\n'
    printf '  They do NOT go to the Trash and cannot be recovered.\n'
    printf '  Use --keep-photos to reset the app and keep every one of them.\n'
    removals=$((removals + 1))
else
    printf '\nPhotos and videos: none found in %s\n' "$DEST"
fi

# -- the app's own state -------------------------------------------------------

printf '\nApp state that would be removed:\n'
state_lines=0

report_dir() {
    # $1 = path, $2 = label
    if [ -d "$1" ]; then
        size=$(du -sh "$1" 2>/dev/null | cut -f1)
        printf '  %-56s %s\n' "$2" "$size"
        state_lines=$((state_lines + 1))
        removals=$((removals + 1))
    fi
}

report_file() {
    if [ -f "$1" ]; then
        printf '  %s\n' "$2"
        state_lines=$((state_lines + 1))
        removals=$((removals + 1))
    fi
}

if [ -n "$PART_FILES" ]; then
    count=$(echo "$PART_FILES" | wc -l | tr -d ' ')
    printf '  %-56s %s files\n' "*.ipm-part (interrupted transfers)" "$count"
    state_lines=$((state_lines + 1))
    removals=$((removals + 1))
fi
report_dir "$DEST/.ipm" ".ipm/ (import history, checksums)"
report_file "$CONFIG_FILE" "$CONFIG_FILE  (the folder you chose in the app)"
report_file "$LOG_FILE" "$LOG_FILE"
[ "$state_lines" -eq 0 ] && printf '  none\n'

if [ "$removals" -eq 0 ]; then
    printf '\nNothing to do — already a clean slate.\n\n'
    exit 0
fi

# What stays, so it is never mistaken for a folder-wide wipe.
if [ -d "$DEST" ]; then
    others=$(find "$DEST" -maxdepth 1 -mindepth 1 \
        ! -regex '.*/[12][0-9][0-9][0-9]$' ! -name '.ipm' 2>/dev/null | sort)
    if [ -n "$others" ]; then
        printf '\nKept, because this app did not create them:\n'
        echo "$others" | sed "s|^|  |"
    fi
fi

printf '\nThe iPhone is not touched.\n'

if [ "$DRY_RUN" -eq 1 ]; then
    printf '\n--dry-run: nothing was removed.\n\n'
    exit 0
fi

if [ "$ASSUME_YES" -eq 0 ]; then
    # The prompt repeats what is at stake: by the time someone is typing the word,
    # the list above has usually scrolled out of sight.
    if [ "$KEEP_PHOTOS" -eq 0 ] && [ -n "$YEAR_DIRS" ]; then
        printf '\nType %s to DELETE %s photos and videos and reset the app: ' \
            "$CONFIRM_WORD" "${total_files:-?}"
    else
        printf '\nType %s to reset the app (photos are kept): ' "$CONFIRM_WORD"
    fi
    read -r answer
    if [ "$answer" != "$CONFIRM_WORD" ]; then
        printf 'Cancelled. Nothing was removed.\n\n'
        exit 1
    fi
fi

# -- do it ---------------------------------------------------------------------

if [ -n "$YEAR_DIRS" ]; then
    echo "$YEAR_DIRS" | while IFS= read -r dir; do
        [ -n "$dir" ] || continue
        rm -rf "$dir"
    done
fi
if [ -n "$PART_FILES" ]; then
    echo "$PART_FILES" | while IFS= read -r file; do
        [ -n "$file" ] || continue
        rm -f "$file"
    done
fi
rm -rf "$DEST/.ipm"
rm -f "$CONFIG_FILE"
rm -f "$LOG_FILE"

printf '\nDone. Start `ipm` and it will behave exactly like a first run.\n\n'
