"""The persistent Textual application.

One screen, always on: a device panel that fills itself in as soon as a phone is
plugged in, a local-library panel fed by the manifest, and two buttons -- import,
and (separately, never automatically) delete. Neither button acts on its click:
both open a confirmation dialog first (:mod:`ipm.tui.screens`).

Anything that came from the phone -- device name, file paths, error text -- is run
through :func:`rich.markup.escape` before it reaches a widget: those strings are
chosen by whoever owns the device, and a file called ``[link=…]x.HEIC`` would
otherwise be interpreted as console markup rather than shown.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from time import monotonic

from rich.markup import escape
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, ProgressBar, RichLog, Static

from ipm import __version__
from ipm.config import Config, clamp_concurrency, save_config
from ipm.core.database import (
    MANIFEST_DIRNAME,
    ManifestDatabase,
    manifest_path_for,
)
from ipm.core.deleter import DeleteProgress, Deleter, VerifyProgress
from ipm.core.formatting import human_bytes, human_duration, human_rate
from ipm.core.history import LibraryChange, detect_changes
from ipm.core.importer import Importer, ImportProgress, cleanup_partials
from ipm.core.library import count_items, item_key
from ipm.core.photos_library import LibraryReconciliation, read_snapshot, reconcile
from ipm.core.runlog import LOG_NAME, RunLog
from ipm.device.afc import AfcDeviceBackend
from ipm.device.monitor import DEFAULT_POLL_INTERVAL, DeviceMonitor
from ipm.device.photos_db import PhotosDatabaseCopy, database_size
from ipm.device.ptp import PtpService
from ipm.errors import friendly_message
from ipm.models import DeleteStats, DeviceInfo, ImportRecord, LibraryStatus, RemoteFile
from ipm.system import DiskSpace, disk_space, open_in_file_manager
from ipm.tui.screens import (
    ConfirmDeleteScreen,
    ConfirmImportScreen,
    DestinationScreen,
    LibraryChangedScreen,
    PermanentDeleteScreen,
)
from ipm.tui.theme import APPLE_DARK, THEME_NAME

__all__ = ["IpmApp"]

logger = logging.getLogger(__name__)

_SCAN_RENDER_INTERVAL = 0.15
"""Minimum seconds between two panel repaints while scanning."""

_BUTTON_LABELS = (
    ("#import-button", "Import all media", "Import"),
    ("#delete-button", "Delete from iPhone", "Delete"),
    ("#folder-button", "Change folder", "Folder"),
)
"""Button captions, wide and narrow. Three full-width stacked buttons would cost
nine rows of a terminal that is already short of them; three short ones on one row
cost three."""

_ERROR_MARK = "\u2717"

_SEVERITY_MARKS = {"[red]": _ERROR_MARK, "[yellow]": "!", "[green]": "\u2713"}
"""Leading markup tag -> the glyph that says the same thing without colour.

The convention was already in the code: a message whose *first* tag is red is an
error, yellow a warning, green something that finished. Reading it here rather than
passing a severity to every one of the fifty-odd call sites keeps the two from
drifting apart."""

NARROW_WIDTH = 108
"""Columns below which the two panels stop fitting side by side.

Measured against a full panel -- a real device name, a real folder path, six-figure
counts -- rather than an empty one: at 108 columns and above both cards render every
line whole, and below that they start wrapping and each grow a row. Stacking is
better than wrapping long before the layout actually breaks, because a wrapped
`Files  17005 - 33.0 GB (+247 parts)` is harder to read than the same line with the
panels one above the other.

It used to be 90, which was the width at which the layout *broke* rather than the
width at which it stopped being pleasant to read. The maintainer looked at 100
columns and asked for the stacking to come sooner."""

WAITING_MESSAGE = (
    "[b]Waiting for an iPhone…[/b]\n\n"
    "Connect it with a USB cable, unlock the screen and tap [b]Trust[/b] if asked.\n"
    "The fields below fill in on their own as soon as the phone is recognised."
)


class IpmApp(App[None]):
    """Main application: live device panel, import button, delete button."""

    CSS_PATH = "app.tcss"
    TITLE = "iphone-photo-manager"

    BINDINGS = [
        ("i", "start_import", "Import"),
        ("d", "start_delete", "Delete"),
        ("f", "change_folder", "Folder"),
        ("r", "rescan", "Rescan"),
        ("v", "verify_library", "Verify"),
        ("escape", "stop", "Stop"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, config: Config) -> None:
        """
        :param config: Settings loaded from disk (possibly overridden by CLI flags).
        """
        super().__init__()
        self.config = config
        self._monitor = DeviceMonitor(DEFAULT_POLL_INTERVAL)
        self._backend: AfcDeviceBackend | None = None
        self._device: DeviceInfo | None = None
        self._media: list[RemoteFile] | None = None
        self._edited: set[str] | None = None
        """Item keys of the photos with an edit on the phone, from the same scan as
        :attr:`_media`. ``None`` means they could not be read, and then nothing may be
        deleted: an unknown edit is an edit that could be lost."""
        self._status = LibraryStatus()
        self._database: ManifestDatabase | None = None
        self._busy = False
        self._cancel: asyncio.Event | None = None
        self._scan_note = ""
        self._reconciliation: LibraryReconciliation | None = None
        self._disk: DiskSpace | None = None
        self._panel_height = 0
        """Rows both panels have been matched to; 0 until the first layout."""
        self._narrow = False
        """Whether the terminal is too narrow for two columns."""
        self._quit_armed = False
        """Set by a q that was refused because a job was running."""
        self._run_log: RunLog | None = None

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="panels"):
            with Vertical(id="device-card", classes="card"):
                yield Static("iPhone", classes="card-title")
                yield Static(WAITING_MESSAGE, id="device-body")
            with Vertical(id="library-card", classes="card"):
                yield Static("On this Mac", classes="card-title")
                yield Static("", id="library-body")
        with Vertical(id="actions"):
            with Horizontal(id="buttons"):
                yield Button("Import all media", variant="primary", id="import-button", disabled=True)
                # Short on purpose: the direction is the part that must be on the
                # button, and "imported files" was a third of a row of terminal to
                # say what the tooltip, the dialog and the panel beside it all say
                # already. It was also why three buttons did not fit in 80 columns.
                yield Button(
                    "Delete from iPhone",
                    variant="error",
                    id="delete-button",
                    disabled=True,
                )
                yield Button("Change folder", id="folder-button")
            yield ProgressBar(total=100, show_eta=False, id="progress")
            with Horizontal(id="status-row"):
                # The line carries three different kinds of message -- "Idle.", the
                # outcome of the last action, and a live byte count -- so it needs a
                # word that covers all three. Without one it read as a sentence
                # floating under the buttons with nothing to attach it to.
                yield Static("Status:", id="status-label")
                yield Static("Idle.", id="progress-detail")
        yield RichLog(id="activity", markup=True, wrap=True, highlight=False)
        yield Footer()

    async def on_mount(self) -> None:
        """Open the manifest, start polling, and ask for a folder if needed."""
        self.register_theme(APPLE_DARK)
        self.theme = THEME_NAME
        self.query_one("#activity", RichLog).border_title = "Activity"
        self._activity("[dim]iphone-photo-manager ready.[/dim]")
        if self.config.destination is None:
            self._prompt_for_destination(initial=True)
        else:
            self._open_database()
            self._refresh_status()
        self._apply_width(self.size.width)
        self._render_all()
        self._watch_devices()

    async def on_unmount(self) -> None:
        """Close the device session and the manifest on exit."""
        if self._backend is not None:
            await self._backend.aclose()
            self._backend = None
        if self._database is not None:
            self._database.close()
            self._database = None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide *Stop* from the footer while there is nothing to stop.

        Returning ``None`` removes the binding rather than greying it out, so the
        footer stays a list of things that can actually be done right now.
        """
        del parameters
        if action == "stop":
            return True if self._busy else None
        return True

    def action_stop(self) -> None:
        """Ask the running job to stop at the next safe point.

        Nothing is interrupted mid-file: the importer finishes the chunk it is
        writing and promotes or discards its ``.ipm-part``, and the deleter stops
        between items. That is why this is a request and not a kill.
        """
        if not self._busy or self._cancel is None:
            return
        if self._cancel.is_set():
            self._activity("[dim]Already stopping — finishing the current file.[/dim]")
            return
        self._cancel.set()
        self._activity("[yellow]Stopping…[/yellow] finishing the current file first.")
        self._set_detail("Stopping…")

    async def action_quit(self) -> None:
        """Quit, but not out from under a running job on the first press.

        A transfer can be sixteen minutes long, and ``q`` is next to nothing on the
        keyboard. Saying what a second press will do follows the same rule the
        command-line guidelines give for Ctrl-C.
        """
        if self._busy and not self._quit_armed:
            self._quit_armed = True
            self._activity(
                "[yellow]A job is running.[/yellow] Press [b]escape[/b] to stop it, "
                "or [b]q[/b] again to quit anyway."
            )
            return
        await super().action_quit()

    def on_resize(self, event: events.Resize) -> None:
        """Re-decide the layout when the terminal changes size."""
        self._apply_width(event.size.width)

    def _apply_width(self, width: int) -> None:
        """Stack the panels when there is not enough room to sit them side by side.

        Textual's CSS has no media queries, so the breakpoint lives here and the
        stylesheet keys off the class.
        """
        narrow = width < NARROW_WIDTH
        if narrow == self._narrow:
            return
        self._narrow = narrow
        self.screen.set_class(narrow, "-narrow")
        for button_id, long, short in _BUTTON_LABELS:
            self.query_one(button_id, Button).label = short if narrow else long
        # The matched height is a two-column idea; stacked, it would only pad the
        # shorter card with blank rows.
        self._panel_height = 0
        for body in ("#device-body", "#library-body"):
            self.query_one(body, Static).styles.min_height = None
        self.call_after_refresh(self._match_panel_heights)
        if narrow:
            # Stacked, the screen scrolls; Textual would otherwise leave it wherever
            # the focused button happens to be, which is past the iPhone panel.
            self.call_after_refresh(self.screen.scroll_home, animate=False)

    # -- device polling ----------------------------------------------------

    @work(exclusive=True, group="monitor")
    async def _watch_devices(self) -> None:
        """Poll usbmuxd forever and react to connect/disconnect events.

        This loop never opens a lockdown session itself: it only asks the local
        daemon which devices are plugged in, which is cheap and invisible to the
        phone. The handshake happens once, in :meth:`_attach_device`.
        """
        while True:
            try:
                events = await self._monitor.poll()
            except Exception as exc:  # noqa: BLE001 - polling must never die
                logger.debug("device poll failed: %s", exc)
                events = []
            for event in events:
                if event.kind == "connected":
                    self._activity(f"[green]iPhone detected[/green] ([dim]{escape(event.serial)}[/dim]).")
                    self._attach_device(event.serial)
                else:
                    await self._handle_disconnect(event.serial)
            await asyncio.sleep(self._monitor.poll_interval)

    @work(exclusive=True, group="attach")
    async def _attach_device(self, serial: str) -> None:
        """Open lockdown + AFC once, read the device info, then scan the media roots."""
        if self._backend is not None:
            return
        self._scan_note = "Connecting…"
        self._render_device()
        try:
            backend = await AfcDeviceBackend.connect(serial)
        except Exception as exc:  # noqa: BLE001 - shown as a friendly line
            logger.warning("connect failed", exc_info=exc)
            self._scan_note = ""
            self._activity(f"[yellow]{escape(friendly_message(exc))}[/yellow]")
            self._render_all()
            return

        self._backend = backend
        try:
            self._device = await backend.get_info()
        except Exception as exc:  # noqa: BLE001
            logger.warning("device info failed", exc_info=exc)
            self._activity(f"[yellow]{escape(friendly_message(exc))}[/yellow]")
        self._greet_device()
        self._render_all()
        self._refresh_status()
        await self._scan_media()
        await self._check_history()

    def _greet_device(self) -> None:
        """Say whether this folder has seen this phone before, and remember it now.

        Cheap, and the first line of defence against the two silent mistakes: the
        wrong folder for this phone, and a phone that has been restored since the
        last import.
        """
        database = self._database
        info = self._device
        if database is None or info is None:
            return
        try:
            known = database.known_device(info.serial)
            database.remember_device(
                info.serial,
                name=info.name,
                product_type=info.product_type,
                ios_version=info.ios_version,
            )
        except Exception as exc:  # noqa: BLE001 - never break the panel over this
            logger.warning("device history unavailable", exc_info=exc)
            return

        if known is None:
            self._activity(
                "[dim]First time this iPhone is used with this folder.[/dim]"
                if self._status.total
                else "[dim]New folder, new iPhone — nothing has been imported here yet.[/dim]"
            )
        else:
            seen = known.last_seen.astimezone().strftime("%d/%m/%Y at %H:%M")
            self._activity(f"[dim]Known iPhone — last used with this folder on {seen}.[/dim]")

    async def _scan_media(self) -> None:
        """Count what is on the phone and cache the listing for the next import."""
        backend = self._backend
        if backend is None:
            return
        self._scan_note = "Scanning the iPhone…"
        self._reconciliation = None
        self._edited = None
        self._render_device()
        last_render = 0.0

        def on_scan(count: int, total: int) -> None:
            # Repainting on every file would mean ~16 000 full renders on a real
            # library, which starves the event loop and makes the app feel frozen.
            nonlocal last_render
            self._scan_note = f"Scanning… {count} files, {human_bytes(total)}"
            now = monotonic()
            if now - last_render < _SCAN_RENDER_INTERVAL:
                return
            last_render = now
            self._render_device()

        try:
            self._media = await backend.list_media(on_progress=on_scan)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scan failed", exc_info=exc)
            self._media = None
            self._scan_note = ""
            self._activity(f"[yellow]{escape(friendly_message(exc))}[/yellow]")
        else:
            self._scan_note = ""
            counts = count_items(self._media)
            self._activity(
                f"Found [b]{counts.items}[/b] item(s) on the iPhone — "
                f"{counts.files} file(s), {human_bytes(counts.bytes_total)}."
            )
            await self._scan_edits(backend, self._media)
        self._render_all()

    async def _scan_edits(self, backend: AfcDeviceBackend, media: list[RemoteFile]) -> None:
        """Find the photos edited on the phone, which deleting would cost their edit.

        Part of every scan rather than of the delete button alone, so the number is in
        the log before anybody opens the dialog -- and so the delete flow can rely on
        :attr:`_edited` describing the same moment as :attr:`_media`.
        """
        try:
            self._edited = await backend.list_edited_items()
        except Exception as exc:  # noqa: BLE001
            logger.warning("edit scan failed", exc_info=exc)
            self._edited = None
            self._activity(
                "[yellow]Could not read which photos were edited on the iPhone, so deleting "
                "is off until a rescan succeeds (press [b]r[/b]).[/yellow]"
            )
            return
        edited = len({item_key(item.path) for item in media} & self._edited)
        if edited:
            self._activity(
                f"[dim]{edited} of them were edited on the iPhone. The originals are "
                "imported, the edits are not yet, so those photos stay on the iPhone when "
                "you delete.[/dim]"
            )

    async def _handle_disconnect(self, serial: str) -> None:
        """Clear the panel, and abort any running job, when the cable goes away."""
        if self._backend is None or self._backend.serial not in (serial, ""):
            return
        backend, self._backend = self._backend, None
        self._device = None
        self._media = None
        self._edited = None
        self._reconciliation = None
        self._scan_note = ""
        if self._busy and self._cancel is not None:
            self._cancel.set()
            self._activity(
                "[yellow]The iPhone was disconnected during the operation. Nothing was "
                "corrupted — reconnect it and press the button again to resume.[/yellow]"
            )
        else:
            self._activity("[dim]iPhone disconnected.[/dim]")
        await backend.aclose()
        self._render_all()

    # -- destination and manifest -----------------------------------------

    def _open_database(self) -> None:
        """(Re)open the manifest, and the run log beside it, for this destination."""
        if self._database is not None:
            self._database.close()
            self._database = None
        if self._run_log is not None:
            self._run_log.close()
            self._run_log = None
        destination = self.config.destination
        if destination is None:
            return
        self._run_log = RunLog(destination / MANIFEST_DIRNAME / LOG_NAME)
        self._run_log.open(f"iphone-photo-manager {__version__} — {destination}")
        try:
            destination.mkdir(parents=True, exist_ok=True)
            self._database = ManifestDatabase(manifest_path_for(destination))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cannot open manifest", exc_info=exc)
            self._activity(f"[red]{escape(friendly_message(exc))}[/red]")

    @work(exclusive=True, group="destination")
    async def _prompt_for_destination(self, initial: bool = False) -> None:
        """Ask for the destination folder and remember the answer."""
        chosen = await self.push_screen_wait(DestinationScreen(self.config.destination))
        if chosen is None:
            if initial and self.config.destination is None:
                self._activity("[yellow]No folder chosen yet — press [b]f[/b] to pick one.[/yellow]")
            return
        self.config = self.config.with_destination(chosen)
        try:
            save_config(self.config)
        except OSError as exc:
            self._activity(f"[yellow]Could not save the setting: {escape(friendly_message(exc))}[/yellow]")
        self._open_database()
        self._activity(f"Destination folder: [b]{escape(str(self.config.destination))}[/b]")
        self._refresh_status()
        self._render_all()

    @work(exclusive=True, group="status")
    async def _refresh_status(self) -> None:
        """Recompute the local-library counters from the manifest and the disk."""
        database = self._database
        destination = self.config.destination
        if database is None or destination is None:
            self._status = LibraryStatus()
            self._disk = None
        else:
            udid = self._backend.serial if self._backend is not None else None
            media = self._media if self._backend is not None else None
            try:
                self._status = await asyncio.to_thread(
                    database.status, destination, udid, media
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("status failed", exc_info=exc)
                self._status = LibraryStatus()
            self._disk = await asyncio.to_thread(disk_space, destination)
        self._render_all()

    # -- actions -----------------------------------------------------------

    @on(Button.Pressed, "#import-button")
    def _on_import_pressed(self, event: Button.Pressed) -> None:
        del event
        self.action_start_import()

    @on(Button.Pressed, "#delete-button")
    def _on_delete_pressed(self, event: Button.Pressed) -> None:
        del event
        self.action_start_delete()

    @on(Button.Pressed, "#folder-button")
    def _on_folder_pressed(self, event: Button.Pressed) -> None:
        del event
        self.action_change_folder()

    def action_change_folder(self) -> None:
        """Pick (or change) the destination folder."""
        if self._busy:
            self._activity("[yellow]Wait for the current operation to finish.[/yellow]")
            return
        self._prompt_for_destination()

    def action_rescan(self) -> None:
        """Re-read the device listing and the local counters."""
        if self._backend is None:
            self._activity("[yellow]Connect an iPhone before rescanning.[/yellow]")
            return
        if self._busy:
            self._activity("[yellow]Wait for the current operation to finish.[/yellow]")
            return
        self._rescan_worker()

    @work(exclusive=True, group="rescan")
    async def _rescan_worker(self) -> None:
        self._activity("Rescanning the iPhone…")
        await self._scan_media()
        self._refresh_status()
        await self._check_history()

    async def _check_history(self) -> None:
        """Compare the manifest with the scan, and speak up when they disagree.

        Every rule elsewhere already refuses to delete a row the phone contradicts.
        This is about telling the user *why* files are suddenly undeletable, and
        offering to clear the stale rows out of the way.
        """
        database = self._database
        backend = self._backend
        files = self._media
        if database is None or backend is None or files is None:
            return
        try:
            change = await asyncio.to_thread(
                lambda: detect_changes(database.live_records(backend.serial), files)
            )
        except Exception as exc:  # noqa: BLE001 - informational only
            logger.warning("history check failed", exc_info=exc)
            return

        if not change.needs_attention:
            if change.absent:
                self._activity(
                    f"[dim]{len(change.absent)} imported file(s) are no longer on the iPhone "
                    "(deleted there, or already removed from here).[/dim]"
                )
            return

        if change.changed:
            self._activity(
                f"[red]{len(change.changed)} file(s) on the iPhone now differ from what was "
                "imported under the same names.[/red]"
            )
        if change.everything_vanished:
            self._activity(
                f"[yellow]None of the {change.tracked} file(s) imported into this folder are "
                "on the iPhone any more.[/yellow]"
            )

        if not await self.push_screen_wait(LibraryChangedScreen(change)):
            self._activity("[dim]The import history was left as it is.[/dim]")
            return

        try:
            await asyncio.to_thread(self._tidy_history, change)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tidy failed", exc_info=exc)
            self._activity(f"[red]{escape(friendly_message(exc))}[/red]")
            return
        self._activity(
            f"[green]History tidied[/green] — {len(change.changed)} out-of-date row(s) forgotten, "
            f"{len(change.absent)} marked as no longer on the iPhone. No file was touched."
        )
        self._refresh_status()

    def _tidy_history(self, change: LibraryChange) -> None:
        """Bring the manifest back in line with the phone. Rows only, never files."""
        database = self._database
        backend = self._backend
        if database is None or backend is None:
            return
        for record in change.changed:
            # The local file stays on disk, untracked: it is a real photo, just not
            # the one sitting at that device path any more.
            database.forget(record.device_udid, record.device_path)
        if change.absent:
            database.mark_deleted(
                backend.serial, [record.device_path for record in change.absent]
            )

    def action_verify_library(self) -> None:
        """Check the scan against the iPhone's own photo database."""
        if self._backend is None:
            self._activity("[yellow]Connect an iPhone before verifying.[/yellow]")
            return
        if self._busy or self._media is None:
            self._activity("[yellow]Wait for the current operation to finish.[/yellow]")
            return
        self._verify_worker()

    @work(exclusive=True, group="verify")
    async def _verify_worker(self) -> None:
        """Copy ``Photos.sqlite`` and reconcile it against the last scan.

        This is the only way to tell a live photo from one in Recently Deleted: the
        deletion is a flag in the database, and the file stays on disk for 30 days.
        The copy is big, so this runs only when the user asks for it.
        """
        backend = self._backend
        files = self._media
        if backend is None or files is None:
            return

        self._begin_job("Reading the iPhone's photo database…")
        size = await database_size(backend)
        self._activity(
            "Checking against the iPhone's photo library — copying its database"
            + (f" ({human_bytes(size)}, about {human_duration(size / 34e6)})" if size else "")
            + "…"
        )
        copied = 0

        def on_chunk(chunk: bytes) -> None:
            nonlocal copied
            copied += len(chunk)
            bar = self.query_one("#progress", ProgressBar)
            bar.update(total=max(size, 1), progress=min(copied, size or copied))
            self._set_detail(f"{human_bytes(copied)} / {human_bytes(size)}")

        try:
            async with PhotosDatabaseCopy(backend, on_chunk=on_chunk) as database:
                self._set_detail("Reading the database…")
                snapshot = await asyncio.to_thread(read_snapshot, database)
                self._reconciliation = await asyncio.to_thread(reconcile, snapshot, files)
        except Exception as exc:  # noqa: BLE001 - never show a traceback
            logger.warning("library verification failed", exc_info=exc)
            self._activity(f"[red]{escape(friendly_message(exc))}[/red]")
            self._end_job("Library check failed.")
            return

        check = self._reconciliation
        self._activity(
            f"[green]Library check[/green] — the Photos app shows "
            f"[b]{check.snapshot.visible}[/b] items; "
            f"{len(check.trashed_on_disk)} more are in Recently Deleted "
            f"(still on disk), {check.snapshot.hidden} hidden."
        )
        if check.is_complete:
            self._activity(
                "[green]Every item in the library has its file on the device — "
                "nothing would be left behind by an import.[/green]"
            )
        else:
            self._activity(
                f"[red]{len(check.missing_files)} item(s) in the library have no file on "
                "the device and cannot be imported:[/red]"
            )
            for key in check.missing_files[:10]:
                self._activity(f"  [red]{escape(key)}[/red]")
            if len(check.missing_files) > 10:
                self._activity(f"  [red]… and {len(check.missing_files) - 10} more.[/red]")

        self._end_job("Library check done.")
        self._render_all()

    def action_start_import(self) -> None:
        """Ask for confirmation, then copy what the folder does not already have."""
        if self._busy or self._backend is None:
            return
        if self.config.destination is None:
            self._prompt_for_destination()
            return
        self._run_import()

    def action_start_delete(self) -> None:
        """Ask for confirmation, then delete verified files from the phone."""
        if self._busy or self._backend is None or self._database is None:
            return
        self._run_delete()

    # -- import ------------------------------------------------------------

    @work(exclusive=True, group="job")
    async def _run_import(self) -> None:
        """Plan, ask, and only then transfer.

        The plan is built before the dialog so the user is told exactly how many
        files and how many bytes are about to arrive, and it is handed to the
        importer afterwards rather than recomputed.
        """
        backend = self._backend
        database = self._database
        destination = self.config.destination
        if backend is None or database is None or destination is None:
            return

        self._begin_job("Preparing…")
        cancel = self._cancel = asyncio.Event()
        importer = Importer(
            backend,
            database,
            destination,
            concurrency=clamp_concurrency(self.config.concurrency),
            on_progress=self._on_import_progress,
            cancel=cancel,
        )

        if self._media is None:
            await self._scan_media()
            if self._media is None:
                self._end_job("Import stopped.")
                return

        try:
            self._set_detail("Working out what still has to be copied…")
            plan = await importer.build_plan(self._media)
        except Exception as exc:  # noqa: BLE001 - never show a traceback
            logger.warning("planning failed", exc_info=exc)
            self._activity(f"[red]{escape(friendly_message(exc))}[/red]")
            self._end_job("Import stopped.")
            return

        if not plan.to_copy:
            self._activity(
                f"[green]Nothing to import[/green] — all {plan.already_imported} file(s) "
                "on the iPhone are already in the destination folder."
            )
            self._end_job("Nothing to import.")
            return

        if not await self.push_screen_wait(ConfirmImportScreen(plan, destination)):
            self._activity("[dim]Import cancelled.[/dim]")
            self._end_job("Idle.")
            return

        if cancel.is_set() or self._backend is None:
            # The cable was pulled while the dialog was open.
            self._activity("[yellow]The iPhone is no longer connected. Nothing was copied.[/yellow]")
            self._end_job("Import stopped.")
            return

        started = datetime.now()
        try:
            removed = await asyncio.to_thread(cleanup_partials, destination)
            if removed:
                self._activity(f"[dim]Removed {removed} leftover partial file(s).[/dim]")

            stats = await importer.run(plan=plan)
        except Exception as exc:  # noqa: BLE001 - never show a traceback
            logger.warning("import failed", exc_info=exc)
            self._activity(f"[red]{escape(friendly_message(exc))}[/red]")
            self._end_job("Import stopped.")
            self._refresh_status()
            return

        elapsed = (datetime.now() - started).total_seconds()
        self._activity(
            f"[green]Import finished[/green] in {human_duration(elapsed)} — "
            f"copied [b]{stats.copied}[/b], skipped [b]{stats.skipped}[/b] "
            f"(already imported), errors [b]{stats.failed}[/b], "
            f"transferred [b]{human_bytes(stats.bytes_copied)}[/b]."
        )
        for message in stats.errors[:10]:
            self._activity(f"  [yellow]{escape(message)}[/yellow]")
        if len(stats.errors) > 10:
            self._activity(f"  [yellow]… and {len(stats.errors) - 10} more.[/yellow]")
        if stats.cancelled:
            self._activity(
                "[yellow]The import was interrupted. Press [b]Import[/b] again to resume "
                "where it stopped.[/yellow]"
            )

        self._end_job(
            f"Import done — {stats.copied} copied, {stats.skipped} skipped, {stats.failed} failed."
        )
        self._refresh_status()
        if not stats.cancelled and open_in_file_manager(destination):
            self._activity(f"[dim]Opened {escape(str(destination))}.[/dim]")

    def _on_import_progress(self, progress: ImportProgress) -> None:
        """Feed the progress bar from the importer (already on the event loop)."""
        bar = self.query_one("#progress", ProgressBar)
        if progress.phase == "scanning":
            # Still counting: how many files there are is the thing being found out.
            bar.update(total=None, progress=0)
            self._set_detail(
                f"Scanning the iPhone… {progress.files_total} file(s), "
                f"{human_bytes(progress.bytes_total)}"
            )
            return
        total = max(progress.bytes_total, 1)
        bar.update(total=total, progress=min(progress.bytes_done, total))
        self._set_detail(
            f"{progress.files_done}/{progress.files_total} files · "
            f"{human_bytes(progress.bytes_done)} / {human_bytes(progress.bytes_total)} · "
            f"{human_rate(progress.bytes_per_second)} · ETA {human_duration(progress.eta_seconds)}"
            + (f" · {progress.current}" if progress.current else "")
        )

    # -- delete ------------------------------------------------------------

    @work(exclusive=True, group="job")
    async def _run_delete(self) -> None:
        """Verify, confirm, then remove the verified files from the device."""
        backend = self._backend
        database = self._database
        destination = self.config.destination
        if backend is None or database is None or destination is None:
            return

        # The cancel event is created before the dialog, not after it: a cable pulled
        # while the user is typing DELETE must be able to stop the run that follows.
        cancel = self._cancel = asyncio.Event()
        self._begin_job("Checking which files are safe to delete…")

        if self._media is None or self._edited is None:
            # Deleting without a listing would mean trusting the manifest alone about
            # what is on the phone, and deleting without the edits would risk one.
            # Scan first, always.
            await self._scan_media()
            if self._media is None or self._edited is None:
                self._end_job("Deletion stopped.")
                return

        deleter = Deleter(
            backend,
            database,
            destination,
            on_progress=self._on_delete_progress,
            cancel=cancel,
            device_files=self._media,
            edited_items=self._edited,
        )
        try:
            candidates = await deleter.candidates()
        except Exception as exc:  # noqa: BLE001
            logger.warning("verification failed", exc_info=exc)
            self._activity(f"[red]{escape(friendly_message(exc))}[/red]")
            self._end_job("Idle.")
            return

        if not candidates.deletable and candidates.edited_on_device:
            self._activity(
                "[yellow]Nothing can be deleted yet: every imported photo that is still on "
                "the iPhone was edited there, and this version does not import edits.[/yellow]"
            )
            self._end_job("Idle.")
            return

        if not candidates.deletable:
            self._activity(
                "[yellow]Nothing can be deleted yet: no imported file has a verified copy "
                "on this Mac.[/yellow]"
            )
            self._end_job("Idle.")
            return

        request = await self.push_screen_wait(ConfirmDeleteScreen(candidates))
        if request is None or not request.records:
            self._activity("[dim]Deletion cancelled.[/dim]")
            self._end_job("Idle.")
            return

        # After the confirmation, not before it. Leading with a red warning about a
        # thing the user has not yet decided to do is noise; the same warning as the
        # last gate, once they have seen what would go and typed the word, is the
        # sentence they will actually read.
        if not self.config.permanent_delete_warning_seen and not await self._warn_once():
            self._activity("[dim]Deletion cancelled.[/dim]")
            self._end_job("Idle.")
            return

        if cancel.is_set() or self._backend is None:
            self._activity(
                "[yellow]The iPhone is no longer connected. Nothing was deleted.[/yellow]"
            )
            self._end_job("Deletion stopped.")
            return

        selected = request.records
        if request.deep:
            selected = await self._verify_deeply(deleter, selected)
            if not selected:
                self._end_job("Nothing was deleted.")
                self._refresh_status()
                return

        # The photo-library session is opened here and nowhere else: it needs the
        # phone unlocked and takes about fifteen seconds to build its catalogue, so
        # making it a condition of merely *running* the app would be a poor trade for
        # something only the delete button needs.
        self._begin_job("Opening the iPhone's photo library…")
        service = PtpService(device_name=self._device.name if self._device else None)
        try:
            try:
                await service.connect(self._on_catalog_progress)
            except Exception as exc:  # noqa: BLE001
                logger.warning("photo library unavailable", exc_info=exc)
                self._activity(f"[red]{escape(friendly_message(exc))}[/red]")
                self._activity(
                    "[yellow]Nothing was deleted. Your photos are untouched on both "
                    "sides.[/yellow]"
                )
                self._end_job("Deletion stopped.")
                return

            deleter.assets = service
            self._begin_job("Deleting…")
            try:
                stats = await deleter.run(selected)
            except Exception as exc:  # noqa: BLE001
                logger.warning("delete failed", exc_info=exc)
                self._activity(f"[red]{escape(friendly_message(exc))}[/red]")
                self._end_job("Deletion stopped.")
                self._refresh_status()
                return
        finally:
            await service.aclose()

        self._report_deletion(stats)
        self._end_job(f"Deleted {stats.items_deleted} item(s) from the iPhone.")
        self._refresh_status()
        await self._scan_media()

    def _on_catalog_progress(self, items: int) -> None:
        """Show the photo library being enumerated, which is not instantaneous."""
        self._set_detail(
            f"Reading the iPhone's photo library… {items} item(s) so far"
            if items
            else "Reading the iPhone's photo library…"
        )

    def _report_deletion(self, stats: DeleteStats) -> None:
        """Say what the phone actually did, as measured by the confirming scan."""
        # "Deletion finished" in green over "removed 0" is the wrong face for a run
        # where the phone did nothing. The colour follows what happened.
        went = stats.items_deleted > 0
        headline = "Deletion finished" if went else "Nothing was deleted"
        self._activity(
            f"[{'green' if went else 'yellow'}]{headline}[/] — removed "
            f"[b]{stats.items_deleted}[/b] item(s) ([b]{stats.deleted}[/b] file(s)), freed "
            f"[b]{human_bytes(stats.bytes_freed)}[/b] on the iPhone, kept "
            f"[b]{stats.failed}[/b]."
        )
        if stats.retried:
            # Said out loud rather than hidden: it is the phone misbehaving, and a
            # user who sees it twice should be able to report it.
            self._activity(
                "[dim]The iPhone ignored the first request, so it was sent again.[/dim]"
            )
        if stats.cancelled:
            self._activity(
                "[yellow]The deletion was stopped. What is reported above is what really "
                "went; everything else is still on the iPhone.[/yellow]"
            )
        for message in stats.errors[:10]:
            self._activity(f"  [yellow]{escape(message)}[/yellow]")
        if len(stats.errors) > 10:
            self._activity(f"  [yellow]… and {len(stats.errors) - 10} more.[/yellow]")
        if stats.leftover:
            # Three different situations share this list, and each gets its own
            # sentence. If nothing was removed, the phone simply refused. If something
            # was, a leftover is either a whole item the phone ignored -- still on the
            # phone, intact, worth asking again -- or a part that failed to ride along
            # with its photo, which is the thing check 17 exists to catch. Calling the
            # first kind "still there although their photo was deleted" was a plain
            # lie about photos the user still had (seen on 2026-10-02).
            if not went:
                self._activity(
                    f"[yellow]The iPhone accepted the request but removed nothing. These "
                    f"{len(stats.leftover)} file(s) are still there — press [b]d[/b] again "
                    "to try once more:[/yellow]"
                )
                self._list_paths(stats.leftover)
                return
            untouched = set(stats.untouched)
            halves = [path for path in stats.leftover if path not in untouched]
            if stats.untouched:
                self._activity(
                    f"[yellow]{stats.items_untouched} item(s) were not removed — the iPhone "
                    "ignored that part of the request. They are still on the phone, "
                    "untouched, and their copies on this Mac are kept. Press [b]d[/b] again "
                    "to try once more:[/yellow]"
                )
                self._list_paths(stats.untouched)
            if halves:
                # Not expected: a Live Photo's video half and its .AAE sidecar have no
                # library item of their own and should go with the still. Say so
                # loudly rather than let the count quietly drift.
                self._activity(
                    f"[yellow]{len(halves)} file(s) are still on the iPhone although "
                    "their photo was deleted:[/yellow]"
                )
                self._list_paths(halves)

    def _list_paths(self, paths: list[str]) -> None:
        """Write up to ten device paths under a report line, then a count of the rest."""
        for path in paths[:10]:
            self._activity(f"  [yellow]{escape(path)}[/yellow]")
        if len(paths) > 10:
            self._activity(f"  [yellow]… and {len(paths) - 10} more.[/yellow]")

    async def _warn_once(self) -> bool:
        """Say once, before the first deletion, that there is no bin to recover from.

        Everyone has learned from the Photos app that a deleted photo waits thirty
        days in Recently Deleted. Deleting through the photo service does not work
        that way, which was measured rather than assumed -- see
        :class:`~ipm.tui.screens.PermanentDeleteScreen`. A wrong expectation about
        an irreversible action is worth its own dialog, and worth typing the word
        for; the checkbox is remembered in the settings file so it is asked once.

        Shown *after* the confirmation dialog, as the last thing before anything is
        removed. It was the first thing for a while, and that was the wrong order: it
        warned about the consequences of a decision the user had not taken yet, and
        made them type ``DELETE`` twice for one deletion.

        :return: True to go ahead with the deletion.
        """
        screen = PermanentDeleteScreen()
        proceed = await self.push_screen_wait(screen)
        if screen.dismissed_for_good:
            self.config = self.config.with_permanent_delete_warning_seen()
            try:
                save_config(self.config)
            except OSError as exc:
                # Not worth stopping a deletion over; the notice simply comes back.
                logger.warning("could not remember the deletion warning", exc_info=exc)
        return proceed

    async def _verify_deeply(
        self, deleter: Deleter, records: list[ImportRecord]
    ) -> list[ImportRecord]:
        """Re-read the selected local copies and return only the ones that check out.

        This is the slow, thorough pass the user asked for in the dialog: every byte
        of every selected copy is read back and hashed against what was recorded when
        it was imported. A file that no longer matches keeps its original on the phone.
        """
        total = sum(record.size for record in records)
        self._begin_job(
            f"Checking {len(records)} file(s), {human_bytes(total)}, against their checksums…"
        )
        started = monotonic()

        def on_verify(progress: VerifyProgress) -> None:
            bar = self.query_one("#progress", ProgressBar)
            bar.update(total=max(progress.bytes_total, 1), progress=progress.bytes_done)
            elapsed = max(monotonic() - started, 1e-6)
            speed = progress.bytes_done / elapsed
            remaining = max(progress.bytes_total - progress.bytes_done, 0)
            self._set_detail(
                f"Checking {progress.files_done}/{progress.files_total} · "
                f"{human_bytes(progress.bytes_done)} / {human_bytes(progress.bytes_total)} · "
                f"{human_rate(speed)} · ETA "
                f"{human_duration(remaining / speed if speed > 0 else None)}"
                + (f" · {escape(progress.current)}" if progress.current else "")
            )

        try:
            result = await deleter.verify(records, on_verify)
        except Exception as exc:  # noqa: BLE001 - never show a traceback
            logger.warning("deep verification failed", exc_info=exc)
            self._activity(f"[red]{escape(friendly_message(exc))}[/red]")
            return []

        if result.cancelled:
            self._activity("[yellow]The check was interrupted. Nothing was deleted.[/yellow]")
            return []

        if result.unhashed:
            self._activity(
                f"[dim]{len(result.unhashed)} file(s) were imported before checksums existed; "
                "they were checked by size only, and now have one for next time.[/dim]"
            )
        if result.rejected:
            self._activity(
                f"[red]{result.rejected} local copy/copies did not match what was imported "
                "and will be kept on the iPhone:[/red]"
            )
            for record in (result.mismatched + result.unreadable)[:10]:
                self._activity(f"  [red]{escape(record.local_path)}[/red]")
            if result.rejected > 10:
                self._activity(f"  [red]… and {result.rejected - 10} more.[/red]")
            self._activity(
                "[yellow]Import again to replace them before deleting anything.[/yellow]"
            )

        safe = result.safe
        self._activity(
            f"[green]Checked[/green] — [b]{len(safe)}[/b] file(s) match their checksum "
            f"exactly{f', {result.rejected} do not' if result.rejected else ''}."
        )
        return safe

    def _on_delete_progress(self, progress: DeleteProgress) -> None:
        """Feed the progress bar from the deleter."""
        bar = self.query_one("#progress", ProgressBar)
        total = max(progress.items_total, 1)
        bar.update(total=total, progress=min(progress.items_done, total))
        self._set_detail(
            f"Deleting {progress.items_done}/{progress.items_total} item(s)"
            + (f" · {escape(progress.current)}" if progress.current else "")
        )

    # -- rendering ---------------------------------------------------------

    def _begin_job(self, detail: str) -> None:
        """Mark the app busy, disable the buttons and show the progress bar.

        The bar starts *indeterminate* -- ``total=None`` makes it pulse -- because
        several of the things that take time cannot say how long they will be until
        they are under way: cataloguing the phone's library takes twenty seconds and
        only knows how many items it has found so far. A bar sitting at 0% for
        twenty seconds is indistinguishable from one that has hung. Whichever phase
        does know its total sets one, and the bar fills normally from then on.
        """
        self._busy = True
        bar = self.query_one("#progress", ProgressBar)
        bar.visible = True
        bar.update(total=None, progress=0)
        self._set_detail(detail)
        self._render_controls()

    def _end_job(self, detail: str) -> None:
        """Clear the busy flag and re-enable whatever is applicable."""
        self._busy = False
        self._cancel = None
        self._quit_armed = False
        self.query_one("#progress", ProgressBar).visible = False
        self._set_detail(detail)
        self._render_controls()

    def _set_detail(self, text: str) -> None:
        """Update the single line under the progress bar."""
        self.query_one("#progress-detail", Static).update(text)

    def _activity(self, message: str, mark: str | None = None) -> None:
        """Append a line to the activity panel, and to the run log on disk.

        The line gets a one-character severity marker in front of it, taken from
        the colour the message already opens with. **In addition to the colour, not
        instead of it**: the colour is what you notice while scrolling, the marker
        is what survives a terminal that has none.

        That is not hypothetical -- Textual honours ``NO_COLOR``, so an app run with
        it set renders monochrome, and there red comes out *darker* than ordinary
        text. Without a marker the one line that says the iPhone is locked would be
        the least visible thing on screen.

        :param message: Console markup, as it should appear.
        :param mark: Override the marker; by default it follows the leading tag.
        """
        stamp = datetime.now().strftime("%H:%M:%S")
        if mark is None:
            mark = next(
                (glyph for tag, glyph in _SEVERITY_MARKS.items() if message.startswith(tag)),
                " ",
            )
        shown = message
        if self.no_color:
            # Textual's monochrome filter converts colour to *luminance*, and red on
            # a dark background has very little: an error rendered through it comes
            # out dimmer than ordinary text, which is the opposite of what it needs.
            # So drop the colour before the filter sees it and let the marker and
            # the weight carry the meaning instead.
            plain = escape(Text.from_markup(message).plain)
            shown = f"[b]{plain}[/b]" if mark == _ERROR_MARK else plain
        self.query_one(RichLog).write(f"[dim]{stamp}[/dim] {mark} {shown}")
        if self._run_log is not None:
            # Without the markup: the file is meant to be pasted into a bug report,
            # not rendered.
            self._run_log.write(f"{stamp} {mark} {Text.from_markup(message).plain}")

    def _render_all(self) -> None:
        """Repaint every reactive part of the screen."""
        self._render_device()
        self._render_library()
        self._render_controls()
        self.call_after_refresh(self._match_panel_heights)

    def _match_panel_heights(self) -> None:
        """Give both panels the height of the taller one.

        Done by measuring rather than by counting lines, because a line longer than
        the panel is wide occupies two rows -- "16984 items (matches the count in
        the Photos app)" is one line and two rows on a narrow terminal, and
        balancing on the line count leaves the border visibly short.

        Done by measuring rather than in the stylesheet, because ``height: 100%`` on
        the cards makes them equal and then *clips* whichever one outgrows the row.
        A status panel that silently hides its last line is worse than an uneven
        border.
        """
        bodies = [
            self.query_one("#device-body", Static),
            self.query_one("#library-body", Static),
        ]
        if self._narrow:
            return
        heights = [body.outer_size.height for body in bodies]
        if not all(heights):
            return  # not laid out yet; the next refresh will come back here
        target = max(heights)
        # Setting it unconditionally would schedule a layout on every repaint, and
        # this method runs after every one of them.
        if target == self._panel_height:
            return
        self._panel_height = target
        for body in bodies:
            body.styles.min_height = target

    def _render_device(self) -> None:
        """Repaint the iPhone panel on its own, without rebalancing the row."""
        self.query_one("#device-body", Static).update("\n".join(self._device_lines()))

    def _device_lines(self) -> list[str]:
        """Build the iPhone panel, one line per entry."""
        if self._backend is None or self._device is None:
            note = f"\n\n[dim]{self._scan_note}[/dim]" if self._scan_note else ""
            return (WAITING_MESSAGE + note).split("\n")

        info = self._device
        lines = [f"[b]{escape(info.name or 'iPhone')}[/b]"]
        if info.model_name:
            lines.append(f"Model      {escape(info.model_name)}")
        if info.ios_version:
            lines.append(f"iOS        {escape(info.ios_version)}")
        if info.battery_percent is not None:
            lines.append(f"Battery    {info.battery_percent}%")
        if info.storage_total is not None:
            # Phrased like the Mac panel's Disk line on purpose: the two sit side by
            # side, and "free of total" reads the same way on both.
            lines.append(
                f"Storage    [b]{human_bytes(info.storage_free)}[/b] free of "
                f"{human_bytes(info.storage_total)}"
            )
        if self._media is not None:
            counts = count_items(self._media)
            # Kept short enough not to wrap in half a terminal: a hint that folds
            # onto a second line costs more attention than it gives back.
            # "(as Photos counts)" described how the number is *worked out* and read
            # as a claim that it is what the Photos app shows -- which it is not: it
            # counts everything on the phone's storage, and the Photos app hides
            # whatever is in Recently Deleted. Say what is counted, not how.
            lines.append(
                f"Library    [b]{counts.items}[/b] items [dim](incl. Recently Deleted)[/dim]"
            )
            parts = (
                f" [dim](+{counts.extra_files} parts)[/dim]" if counts.extra_files else ""
            )
            lines.append(
                f"Files      {counts.files} · {human_bytes(counts.bytes_total)}{parts}"
            )
            check = self._reconciliation
            if check is None:
                lines.append("[dim]Press [b]v[/b] to check against the phone's library.[/dim]")
            else:
                trashed = len(check.trashed_on_disk)
                lines.append(
                    f"In Photos  [b]{check.snapshot.visible}[/b] items"
                    + (
                        f" [dim](+{trashed} in Recently Deleted)[/dim]"
                        if trashed
                        else " [dim](checked, all accounted for)[/dim]"
                    )
                )
                if not check.is_complete:
                    lines.append(
                        f"[red]{len(check.missing_files)} item(s) have no file on the device[/red]"
                    )
        elif self._scan_note:
            lines.append(f"Library    [dim]{self._scan_note}[/dim]")
        else:
            lines.append("Library    —")
        if self._media is not None and self._scan_note:
            # A rescan keeps the previous numbers on screen; without this line there
            # would be no sign that anything is happening.
            lines.append(f"           [dim]{self._scan_note}[/dim]")
        return lines

    def _render_library(self) -> None:
        """Repaint the Mac panel on its own, without rebalancing the row."""
        self.query_one("#library-body", Static).update("\n".join(self._library_lines()))

    def _library_lines(self) -> list[str]:
        """Build the Mac panel from the manifest counters and the volume.

        Laid out to answer, in order, the three questions someone actually has in
        front of this screen: where are my photos going, how much of the phone do I
        already have, and is there room for the rest.
        """
        destination = self.config.destination
        status = self._status
        # The label column is the same eleven characters as the iPhone panel beside
        # it. It used to be twenty-two here, with sentence-shaped labels ("Imported &
        # verified", "Local copy missing"), and the maintainer's verdict was that the
        # panel read as prose and the numbers were hard to find. Same rhythm on both
        # sides means the eye lands in the same place twice.
        lines = [
            f"Folder     [b]{escape(str(destination))}[/b]"
            if destination
            else "Folder     [yellow]not set[/yellow]",
            self._disk_line(),
            "",
            f"Verified   [b]{status.verified}[/b] files · {human_bytes(status.verified_bytes)}",
            self._kinds_line(),
            f"Missing    [b]{status.unverified}[/b] [dim](no local copy)[/dim]",
            f"Deleted    [b]{status.deleted}[/b] [dim](gone from the iPhone)[/dim]",
        ]
        lines.extend(self._coverage_lines())
        return lines

    def _disk_line(self) -> str:
        """One line about the volume the photos are landing on, or a blank."""
        disk = self._disk
        if disk is None:
            return ""
        share = f" ({disk.used * 100 // disk.total}% used)" if disk.total else ""
        return (
            f"Disk       [b]{human_bytes(disk.free)}[/b] free of "
            f"{human_bytes(disk.total)}[dim]{share}[/dim]"
        )

    def _kinds_line(self) -> str:
        """What the folder is made of, in items rather than files.

        A Live Photo counts as one photo: its ``.MOV`` half shares a base name with
        the still and has no library item of its own, which is the same rule the
        delete scope uses. An extension nobody recognises is counted apart rather
        than guessed at, and only shown when there is one.
        """
        status = self._status
        if not status.verified_items:
            return ""
        spare = f" [dim]· {status.other} other[/dim]" if status.other else ""
        # Deliberately the row under `Verified`, which counts files: the two numbers
        # differ by the Live Photo halves and the sidecars, and putting `Items`
        # directly beneath `... files` is what makes that visible instead of looking
        # like an arithmetic mistake.
        return (
            f"Items      [b]{status.verified_items}[/b] · "
            f"[b]{status.photos}[/b] photos · [b]{status.videos}[/b] videos{spare}"
        )

    def _coverage_lines(self) -> list[str]:
        """How much of the connected phone this folder already holds.

        One line, and only with a phone attached. How many files are *on* the phone
        is already in the panel beside this one, so repeating it here would cost a
        line to say nothing; what is not there, and is the number someone actually
        wants before starting a transfer, is the difference.

        Hidden without a phone: the figures would describe a scan that may be hours
        old, and a stale "up to date" is worse than silence.
        """
        status = self._status
        if self._backend is None or status.on_device_files == 0:
            return []

        if status.is_complete:
            return ["", "[green]Everything on the iPhone is in this folder.[/green]"]

        lines = [
            "",
            f"To import  [b]{status.pending_files}[/b] files · "
            f"{human_bytes(status.pending_bytes)}",
        ]
        disk = self._disk
        if disk is not None and not disk.fits(status.pending_bytes):
            lines.append(
                f"[red]Not enough room: {human_bytes(disk.free)} free.[/red]"
            )
        return lines

    def _render_controls(self) -> None:
        """Enable or disable the buttons according to the current state."""
        connected = self._backend is not None
        has_destination = self.config.destination is not None and self._database is not None

        import_button = self.query_one("#import-button", Button)
        import_button.disabled = self._busy or not connected or not has_destination
        import_button.tooltip = (
            "Connect and unlock your iPhone first"
            if not connected
            else "Choose a destination folder first"
            if not has_destination
            else "Copy every photo and video that is not on this Mac yet"
        )

        delete_button = self.query_one("#delete-button", Button)
        delete_button.disabled = (
            self._busy or not connected or not has_destination or self._status.verified == 0
        )
        delete_button.tooltip = (
            "Connect and unlock your iPhone first"
            if not connected
            else "Import and verify some files first"
            if self._status.verified == 0
            else f"Remove {self._status.verified} verified file(s) from the iPhone"
        )

        self.query_one("#folder-button", Button).disabled = self._busy


def run(config: Config) -> None:
    """Start the application (blocking until the user quits)."""
    IpmApp(config).run()
