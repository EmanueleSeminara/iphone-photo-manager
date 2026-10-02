"""Modal screens: destination picker, import and delete confirmations, and the
warning shown when the phone stops matching what the folder remembers.

Neither of the two buttons that touch the iPhone acts on its click. Both open a
dialog first: the import one so the user sees how much is about to be copied and
where, the delete one because it is the single irreversible action in the
application -- and there it also chooses *how much* to delete, and how thoroughly
to check it first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, RadioButton, RadioSet, Static

from ipm.core.deleter import group_by_item, select_newest_items
from ipm.core.formatting import human_bytes
from ipm.core.history import LibraryChange
from ipm.core.items import taken_at
from ipm.models import DeleteCandidates, DeleteRequest, ImportPlan, ImportRecord

__all__ = [
    "CONFIRM_WORD",
    "ConfirmDeleteScreen",
    "ConfirmImportScreen",
    "DestinationScreen",
    "LibraryChangedScreen",
    "PermanentDeleteScreen",
]

CONFIRM_WORD = "DELETE"
"""Word the user must type before the destructive button becomes clickable."""

EVERYTHING = 0
"""Index of the "delete everything" radio button."""
ONLY_RECENT = 1
"""Index of the "delete only the N most recent items" radio button."""


class DestinationScreen(ModalScreen[Path | None]):
    """Ask for the folder where imported media should be stored."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, current: Path | None = None) -> None:
        """
        :param current: Folder currently configured, pre-filled in the input.
        """
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="destination-dialog"):
            yield Label("Where should your photos and videos be saved?", classes="dialog-title")
            yield Static(
                "The folder is created if it does not exist. Inside it you will find "
                "one folder per year, and one per month.",
                classes="dialog-help",
            )
            yield Input(
                value=str(self._current) if self._current else str(Path.home() / "Pictures" / "iPhone"),
                placeholder="/Users/you/Pictures/iPhone",
                id="destination-input",
            )
            yield Static("", id="destination-error", classes="dialog-error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="destination-cancel")
                yield Button("Use this folder", variant="primary", id="destination-ok")

    def on_mount(self) -> None:
        """Focus the input so the user can type straight away."""
        self.query_one("#destination-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "destination-ok":
            self._accept()
        elif event.button.id == "destination-cancel":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the input behaves like pressing the primary button."""
        del event
        self._accept()

    def action_cancel(self) -> None:
        """Escape closes the dialog without changing anything."""
        self.dismiss(None)

    def _accept(self) -> None:
        """Validate the typed path and return it, or explain what went wrong."""
        raw = self.query_one("#destination-input", Input).value.strip()
        error = self.query_one("#destination-error", Static)
        if not raw:
            error.update("Please type a folder path.")
            return
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            error.update(f"Cannot use that folder: {exc.strerror or exc}.")
            return
        self.dismiss(candidate)


class ConfirmImportScreen(ModalScreen[bool]):
    """Summary of what an import would copy, shown before anything is transferred.

    Importing is not destructive, so there is no word to type here: the dialog
    exists so a click never silently starts a long transfer, and so the user can
    check the destination folder before tens of gigabytes land in it.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, plan: ImportPlan, destination: Path) -> None:
        """
        :param plan: What the importer decided still needs copying.
        :param destination: Folder the files would be written to.
        """
        super().__init__()
        self._plan = plan
        self._destination = destination

    def compose(self) -> ComposeResult:
        plan = self._plan
        with Vertical(id="import-dialog"):
            yield Label("Import from the iPhone", classes="dialog-title")
            yield Static(
                f"[b]{plan.to_transfer}[/b] file(s), [b]{human_bytes(plan.total_bytes)}[/b], "
                "will be copied to\n"
                f"[b]{escape(str(self._destination))}[/b]\n"
                "sorted into one folder per year and month.",
                id="import-summary",
            )
            if plan.adopted:
                # The manifest was lost or reset, but the photos are still here. Say so:
                # otherwise a run that transfers nothing looks like it is about to
                # re-download the whole library.
                yield Static(
                    f"[b]{plan.adopted}[/b] file(s) are already in that folder. They are "
                    "checked and added back to the history — not downloaded again.",
                    classes="dialog-help",
                )
            if plan.already_imported:
                yield Static(
                    f"[b]{plan.already_imported}[/b] file(s) are already on this Mac "
                    "and will be skipped.",
                    classes="dialog-help",
                )
            yield Static(
                "Nothing is removed from the iPhone by this step, and nothing already "
                "in the folder is overwritten. You can stop it at any time by unplugging "
                "the cable; the next run resumes where it left off.",
                classes="dialog-help",
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="import-cancel")
                yield Button(
                    f"Import {plan.to_transfer} file(s)"
                    if plan.to_transfer
                    else f"Check {plan.adopted} file(s)",
                    variant="primary",
                    id="import-ok",
                )

    def on_mount(self) -> None:
        """Focus the confirming button; Escape (or Cancel) is the other way out."""
        self.query_one("#import-ok", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "import-ok")

    def action_cancel(self) -> None:
        """Escape means "do not import"."""
        self.dismiss(False)


class ConfirmDeleteScreen(ModalScreen[DeleteRequest | None]):
    """Typed confirmation, scope choice and depth of checking, before anything goes.

    Returns a :class:`~ipm.models.DeleteRequest`, or ``None`` when the user backs
    out. The scope is expressed in *items* (what the Photos app counts), never in
    files, so a Live Photo cannot be half-deleted:
    :func:`~ipm.core.deleter.select_newest_items` expands the chosen items back into
    every file behind them.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_RECENT = 5
    """Pre-filled item count -- small on purpose, so the obvious first run is a trial."""

    def __init__(self, candidates: DeleteCandidates) -> None:
        """
        :param candidates: Result of the verification pass, shown to the user.
        """
        super().__init__()
        self._candidates = candidates
        self._items = len(group_by_item(candidates.deletable))
        self._selection: list[ImportRecord] = list(candidates.deletable)

    def compose(self) -> ComposeResult:
        deletable = len(self._candidates.deletable)
        unverified = len(self._candidates.unverified)
        with Vertical(id="confirm-dialog"):
            yield Label("Delete photos from the iPhone", classes="dialog-title dialog-danger")
            yield Static(
                f"[b]{self._items}[/b] item(s) — [b]{deletable}[/b] file(s), "
                f"[b]{human_bytes(self._candidates.total_bytes)}[/b] — can be removed "
                "from the iPhone.\n"
                "Every one of them was checked again just now: the copy on this Mac exists "
                "with the right size, and the file on the iPhone is still the one that was "
                "imported.\n"
                "The iPhone deletes them itself, as whole photos, so they leave the Photos "
                "app properly.\n"
                "[b]They do not go to Recently Deleted.[/b] They are removed outright, and "
                "after this the copies on this Mac are the only ones you have.",
                id="confirm-summary",
            )
            if self._candidates.held_back:
                with VerticalScroll(id="confirm-excluded"):
                    if unverified:
                        yield Static(
                            f"[b]{unverified}[/b] file(s) will be [b]kept[/b] because their "
                            "local copy is missing or incomplete:",
                            classes="dialog-warning",
                        )
                        yield Static(_preview(self._candidates.unverified))
                    if self._candidates.changed_on_device:
                        yield Static(
                            f"[b]{len(self._candidates.changed_on_device)}[/b] file(s) on the "
                            "iPhone are [b]no longer the ones that were imported[/b] and will "
                            "be kept. Import again to bring the new versions over:",
                            classes="dialog-error",
                        )
                        yield Static(_preview(self._candidates.changed_on_device))
                    if self._candidates.edited_on_device:
                        edited = self._candidates.edited_on_device
                        yield Static(
                            f"[b]{len(group_by_item(edited))}[/b] item(s) — "
                            f"[b]{len(edited)}[/b] file(s) — were [b]edited on the iPhone[/b]. "
                            "This version imports the originals but not the edits yet, so "
                            "they stay on the iPhone. Support is on the way:",
                            classes="dialog-help",
                        )
                        yield Static(_preview(edited))
                    if self._candidates.absent_from_device:
                        yield Static(
                            f"[b]{len(self._candidates.absent_from_device)}[/b] file(s) are no "
                            "longer on the iPhone at all — nothing to delete there:",
                            classes="dialog-help",
                        )
                        yield Static(_preview(self._candidates.absent_from_device))
            with RadioSet(id="confirm-scope"):
                yield RadioButton(f"Everything ({self._items} items)", value=True, id="scope-all")
                yield RadioButton("Only the most recent…", id="scope-recent")
            with Horizontal(id="confirm-count-row"):
                yield Input(
                    value=str(self.DEFAULT_RECENT),
                    placeholder=str(self.DEFAULT_RECENT),
                    type="integer",
                    max_length=7,
                    disabled=True,
                    id="confirm-count",
                )
                yield Static("items, newest first", id="confirm-count-label")
            yield Static("", id="confirm-selection")
            yield Checkbox(
                "Read every local copy back and compare it (slower, recommended)",
                value=True,
                id="confirm-deep",
            )
            yield Static(
                f"This cannot be undone. Type [b]{CONFIRM_WORD}[/b] to enable the button.",
                classes="dialog-help",
            )
            yield Input(placeholder=CONFIRM_WORD, id="confirm-input")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="confirm-cancel")
                yield Button(
                    f"Delete {self._items} item(s) from iPhone",
                    variant="error",
                    id="confirm-ok",
                    disabled=True,
                )

    def on_mount(self) -> None:
        """Focus the confirmation input and show what the default scope selects."""
        self._recompute()
        self.query_one("#confirm-input", Input).focus()

    # -- scope -------------------------------------------------------------

    @on(RadioSet.Changed, "#confirm-scope")
    def _on_scope_changed(self, event: RadioSet.Changed) -> None:
        """Enable the item count only for the partial option, then recompute."""
        self.query_one("#confirm-count", Input).disabled = event.index != ONLY_RECENT
        self._recompute()

    @on(Input.Changed, "#confirm-count")
    def _on_count_changed(self, event: Input.Changed) -> None:
        del event
        self._recompute()

    def _requested_items(self) -> int | None:
        """Return the number of items the user asked for, or ``None`` for all."""
        if self.query_one("#confirm-scope", RadioSet).pressed_index != ONLY_RECENT:
            return None
        raw = self.query_one("#confirm-count", Input).value.strip()
        try:
            return int(raw)
        except ValueError:
            return 0

    def _recompute(self) -> None:
        """Rebuild the selection and repaint everything that depends on it."""
        limit = self._requested_items()
        self._selection = select_newest_items(
            self._candidates.deletable, limit, self._candidates.taken
        )
        summary = self.query_one("#confirm-selection", Static)
        button = self.query_one("#confirm-ok", Button)

        if not self._selection:
            summary.update("[b]Nothing selected.[/b] Choose at least one item.")
            button.label = "Delete nothing"
        else:
            items = len(group_by_item(self._selection))
            size = sum(record.size for record in self._selection)
            clamped = (
                f" (only {items} are available)"
                if limit is not None and limit > self._items
                else ""
            )
            summary.update(
                f"Will delete [b]{items}[/b] item(s){clamped} — [b]{len(self._selection)}[/b] "
                f"file(s), [b]{human_bytes(size)}[/b]"
                f"{_date_range(self._selection, self._candidates.taken)}."
            )
            button.label = f"Delete {items} item(s) from iPhone"
        self._refresh_button()

    def _refresh_button(self) -> None:
        """The destructive button needs both an exact word and a non-empty selection."""
        typed = self.query_one("#confirm-input", Input).value.strip()
        self.query_one("#confirm-ok", Button).disabled = (
            typed != CONFIRM_WORD or not self._selection
        )

    # -- confirmation ------------------------------------------------------

    @on(Input.Changed, "#confirm-input")
    def _on_word_changed(self, event: Input.Changed) -> None:
        """Unlock the destructive button only on an exact match."""
        del event
        self._refresh_button()

    @on(Input.Submitted, "#confirm-input")
    def _on_word_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip() == CONFIRM_WORD and self._selection:
            self._accept()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-ok" and self._selection:
            self._accept()
        elif event.button.id == "confirm-cancel":
            self.dismiss(None)

    def _accept(self) -> None:
        """Hand back what to delete, and how thoroughly to check it first."""
        self.dismiss(
            DeleteRequest(
                records=self._selection,
                deep=self.query_one("#confirm-deep", Checkbox).value,
            )
        )

    def action_cancel(self) -> None:
        """Escape always means "do not delete"."""
        self.dismiss(None)


class PermanentDeleteScreen(ModalScreen[bool]):
    """Shown once, before the first deletion: this does not go to a bin.

    Deleting a photo *in the Photos app* moves it to Recently Deleted, where iOS
    keeps it for thirty days. Deleting it through the photo service, which is what
    this application and Image Capture do, does not: measured on an iPhone 13
    running iOS 26.6, the item and its files were gone from the library and were
    never in Recently Deleted.

    That is a different promise from the one everybody has learned from the Photos
    app, so it is made once, in its own dialog. Somebody who has read it can turn it
    off for good.

    **It does not ask for the word.** It used to, and it sat before the confirmation,
    so one deletion meant typing ``DELETE`` twice. It is now the last gate: the user
    has already chosen what goes and typed the word to confirm it, and asking again
    two seconds later trains people to type it without reading. One word per
    deletion, and this dialog is the last thing they read before it happens.

    Returns True to go ahead with the deletion, False to back out.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="permanent-dialog"):
            yield Label("Deleted means deleted", classes="dialog-title dialog-danger")
            yield Static(
                "Photos removed by this application do [b]not[/b] go to Recently "
                "Deleted. The iPhone removes them outright, the way Image Capture "
                "does — there is no thirty-day window and nothing to restore from.",
                id="permanent-warning",
            )
            yield Static(
                "After a deletion, the copies in your destination folder are the only "
                "ones you have. Nothing is deleted unless its copy on this Mac has "
                "just been checked, byte for byte — but a checked copy is still one "
                "copy, and one copy is not a backup.",
                classes="dialog-help",
            )
            yield Checkbox("Do not show this again", value=False, id="permanent-dismiss")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="permanent-cancel")
                yield Button("Delete permanently", variant="error", id="permanent-ok")

    def on_mount(self) -> None:
        """Focus Cancel, not the red button.

        The dangerous action should not be one stray Return away from a dialog that
        has just appeared.
        """
        self.query_one("#permanent-cancel", Button).focus()

    @property
    def dismissed_for_good(self) -> bool:
        """Whether the user ticked the box before leaving."""
        return bool(self.query_one("#permanent-dismiss", Checkbox).value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "permanent-ok")

    def action_cancel(self) -> None:
        self.dismiss(False)


class LibraryChangedScreen(ModalScreen[bool]):
    """Shown when the phone no longer matches what the manifest remembers.

    Returns True when the user wants the stale rows tidied away. Tidying touches
    the manifest only -- not one file on the Mac, not one on the phone.
    """

    BINDINGS = [("escape", "cancel", "Leave it")]

    def __init__(self, change: LibraryChange) -> None:
        """
        :param change: Result of :func:`~ipm.core.history.detect_changes`.
        """
        super().__init__()
        self._change = change

    def compose(self) -> ComposeResult:
        change = self._change
        with Vertical(id="changed-dialog"):
            yield Label("This iPhone is not the one this folder remembers", classes="dialog-title")
            if change.changed:
                yield Static(
                    f"[b]{len(change.changed)}[/b] file(s) on the iPhone have the same names as "
                    "photos already imported here, but they are [b]different photos[/b].\n"
                    "The usual cause is an iPhone that was erased and set up again: iOS starts "
                    "numbering from IMG_0001 once more.",
                    classes="dialog-warning",
                )
                with VerticalScroll(id="changed-list"):
                    yield Static(_preview(change.changed))
            if change.everything_vanished:
                yield Static(
                    f"None of the [b]{change.tracked}[/b] file(s) this folder imported are on "
                    "the iPhone any more.\nEither they were all deleted there, or this folder "
                    "belongs to a different phone.",
                    classes="dialog-warning",
                )
            yield Static(
                "Nothing has been deleted, and nothing will be: those files are already "
                "excluded from the delete button. Importing will bring the new photos over "
                "as new files, beside the ones you already have.",
                classes="dialog-help",
            )
            yield Static(
                "Tidying up removes the out-of-date [b]rows[/b] from this folder's import "
                "history. Your photos on this Mac are not touched, and neither is the iPhone.",
                classes="dialog-help",
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Leave it as it is", id="changed-keep")
                yield Button("Tidy up the history", variant="primary", id="changed-tidy")

    def on_mount(self) -> None:
        self.query_one("#changed-keep", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "changed-tidy")

    def action_cancel(self) -> None:
        self.dismiss(False)


def _date_range(records: list[ImportRecord], taken: Mapping[str, float]) -> str:
    """Describe the span the selection covers, e.g. ``, 12/03/2024 → 05/08/2025``.

    Uses the same dates that chose the selection (:func:`~ipm.core.items.taken_at`),
    so the range shown is the range that will go.
    """
    stamps = [stamp for record in records if (stamp := taken_at(record, taken)) > 0]
    if not stamps:
        return ""
    try:
        oldest = datetime.fromtimestamp(min(stamps)).strftime("%d/%m/%Y")
        newest = datetime.fromtimestamp(max(stamps)).strftime("%d/%m/%Y")
    except (OverflowError, OSError, ValueError):
        return ""
    return f", {oldest}" if oldest == newest else f", {oldest} → {newest}"


def _preview(records: Iterable[ImportRecord], limit: int = 10) -> str:
    """Render at most *limit* device paths, with a "and N more" tail."""
    items = [record.device_path for record in records]
    shown = "\n".join(f"  • {escape(path)}" for path in items[:limit])
    if len(items) > limit:
        shown += f"\n  … and {len(items) - limit} more"
    return shown
