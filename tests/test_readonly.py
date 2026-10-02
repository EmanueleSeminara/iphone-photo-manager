"""The file system of the iPhone is read-only, and this test proves it.

There is no exception at all: the AFC backend -- detection, the device
panel, the media scan, the import, the library check -- uses read-only AFC and
lockdown calls and nothing else. Deletion moved to :mod:`ipm.device.ptp`, where the
phone's own photo service removes whole library items, because unlinking a file over
AFC left its row behind in the Photos app.

That property cannot be unit-tested against hardware (see ``MANUAL_TESTING.md``), so
it is enforced statically: the AFC backend is parsed and every call it makes into
``pymobiledevice3`` is checked against an allow-list. A write call anywhere in it
fails this test. If deletion over AFC is ever needed again -- for a Live Photo half
the photo service declines to remove -- adding it here is a deliberate act that has
to update :data:`WRITE_IS_ALLOWED_IN`, not something that can slip in.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "ipm"
AFC_MODULE = SOURCE_ROOT / "device" / "afc.py"

READ_ONLY_CALLS = frozenset(
    {
        "connect",
        "aclose",
        "close",
        "get_value",
        "get_device_info",
        "listdir",
        "isdir",
        "stat",
        "fopen",
        "fread",
        "fclose",
    }
)
"""AFC / lockdown methods that only ever read from the device."""

WRITING_CALLS = frozenset(
    {
        "rm",
        "rm_single",
        "makedirs",
        "mkdir",
        "rename",
        "link",
        "symlink",
        "set_file_contents",
        "set_file_metadata",
        "fwrite",
        "ftruncate",
        "truncate",
        "push",
        "pull",
        "remove",
        "unlink",
        "wipe",
        "shutdown",
        "reboot",
        "set_value",
        "remove_value",
        "unpair",
        "pair",
    }
)
"""Methods that modify the device (``pull`` writes locally but is banned anyway:
the importer needs the atomic-rename path in ``download_file``)."""

WRITE_IS_ALLOWED_IN: dict[str, set[str]] = {}
"""Which method may call which mutating API. Deliberately empty: nothing may."""


def _method_calls(source: str, filename: str = "<source>") -> list[tuple[str, str]]:
    """Return ``(enclosing function, method)`` for every attribute call in *source*.

    Deliberately blind to *what* the method is called on. The receiver is exactly
    the thing that must not be trusted here: an earlier version of this test only
    recognised the literal shape ``self._afc.x()``, and a write was therefore
    invisible when it went through a local variable -- which is how ``connect()``
    holds the service, since it builds it before there is a ``self`` to hang it on.
    The method *name* is what says whether a call removes a photo, so the name is
    what is matched, and :data:`WRITING_CALLS` is what decides.

    Names are matched loosely on purpose. Ordinary calls in this module
    (``list.append``, ``posixpath.join``, ``logger.warning``) collide with nothing
    in :data:`WRITING_CALLS`, so the width costs no false positives -- and a day
    when it does is a day worth looking at the collision.
    """
    tree = ast.parse(source, filename=filename)
    found: list[tuple[str, str]] = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                found.append((function.name, node.func.attr))
    return found


DEVICE_FACTORIES = frozenset({"AfcService", "create_using_usbmux"})
"""Calls that hand back a live device object, so whatever they are assigned to is one."""


def _device_locals(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names inside *function* that hold a device object.

    ``connect()`` builds the service and the lockdown session before there is a
    ``self`` to store them on, so inside it they are ordinary local variables. A
    receiver-based check that only knew about ``self._afc`` was blind to that whole
    function -- which is the one place in this module where the pattern is forced
    rather than chosen.
    """
    names: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if isinstance(value, ast.Await):
            value = value.value
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name):
            continue
        if value.func.id in DEVICE_FACTORIES:
            names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
    return names


def _is_device_receiver(node: ast.expr, locals_: set[str]) -> bool:
    """Whether *node* evaluates to something that talks to the phone.

    Two shapes count: the stored services (``self._afc``, ``self._lockdown``) and
    any local the function assigned from a device factory.
    """
    if isinstance(node, ast.Name):
        return node.id in locals_
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in ("_afc", "_lockdown")
    )


def _device_calls(module: Path) -> list[tuple[str, str]]:
    """Return ``(enclosing function, method)`` for every call on a device object.

    Narrower than :func:`_method_calls`, and only used by the "is this call one we
    have classified?" review prompt below, which cannot afford the wider net: over
    the whole module that question would be asked of ``append`` and ``join`` too.
    The write ban does not use this -- see the docstring above for why.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    found: list[tuple[str, str]] = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        locals_ = _device_locals(function)
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if _is_device_receiver(node.func.value, locals_):
                found.append((function.name, node.func.attr))
    return found


def test_nothing_writes_to_the_device_file_system() -> None:
    source = AFC_MODULE.read_text(encoding="utf-8")
    for function, method in _method_calls(source, str(AFC_MODULE)):
        if method in WRITING_CALLS:
            assert method in WRITE_IS_ALLOWED_IN.get(function, set()), (
                f"{function}() calls the mutating AFC method {method}(); the file-system "
                "channel is read-only, and deleting a file this way leaves its row in the "
                "Photos library. Deletion belongs in ipm/device/ptp.py."
            )


WRITES_THE_GUARD_MUST_SEE = (
    # The one that was actually missed: connect() holds the service in a local,
    # because it is the method that builds it.
    "async def connect():\n    afc = AfcService()\n    await afc.rm('/DCIM/x')\n",
    # An alias inside an ordinary method.
    "def scan(self):\n    service = self._afc\n    service.rm('/DCIM/x')\n",
    # The plain shape, which was always caught -- kept so a rewrite cannot lose it.
    "def scan(self):\n    self._afc.rm('/DCIM/x')\n",
    # Reached through another object rather than off self directly.
    "def scan(self):\n    self._session.afc.rm('/DCIM/x')\n",
)
"""Ways of spelling a deletion that the guard has to recognise, one per shape.

Every one of these passed the guard before the receiver check was dropped.
"""


@pytest.mark.parametrize("source", WRITES_THE_GUARD_MUST_SEE)
def test_the_guard_sees_a_write_however_it_is_written(source: str) -> None:
    """The guard must key on the method, not on how the receiver is spelled.

    This is a test of the test. The write ban is the cheapest stand-in there is for
    a hardware test on the one path where a bug cannot be undone, so what it can
    and cannot see is itself worth pinning: a future tidy-up that narrows it back
    to one syntactic shape fails here rather than silently going quiet.
    """
    methods = {method for _, method in _method_calls(source)}
    assert methods & WRITING_CALLS, (
        "the read-only guard did not recognise this as a write to the device:\n"
        f"{source}"
    )


def test_every_device_call_is_a_known_one() -> None:
    """An unrecognised call is a review prompt, not necessarily a bug."""
    for function, method in _device_calls(AFC_MODULE):
        assert method in READ_ONLY_CALLS or method in WRITING_CALLS, (
            f"{function}() calls the unclassified device method {method}(); add it to "
            "READ_ONLY_CALLS or WRITING_CALLS after checking what it does."
        )


def test_files_are_opened_for_reading() -> None:
    """``fopen`` must name its mode: the default is "r", but a typo would truncate."""
    tree = ast.parse(AFC_MODULE.read_text(encoding="utf-8"), filename=str(AFC_MODULE))
    opens = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fopen"
    ]
    assert opens, "download_file no longer opens files through fopen -- review this test"
    for call in opens:
        modes = [keyword.value for keyword in call.keywords if keyword.arg == "mode"]
        assert modes, "fopen() must pass mode= explicitly"
        assert all(isinstance(mode, ast.Constant) and mode.value == "r" for mode in modes)


HARDWARE_PACKAGES = frozenset({"pymobiledevice3", "ImageCaptureCore", "Foundation", "objc"})
"""Everything that talks to a device or to the Objective-C bridge."""


@pytest.mark.parametrize(
    "module",
    sorted(path for path in SOURCE_ROOT.rglob("*.py") if path.parent.name != "device"),
    ids=lambda path: str(path.name),
)
def test_hardware_access_stays_in_the_device_layer(module: Path) -> None:
    """No module outside ``ipm.device`` may import a hardware or bridge package."""
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    offenders = {name for name in imported if name.split(".")[0] in HARDWARE_PACKAGES}
    assert not offenders, (
        f"{module} imports {', '.join(sorted(offenders))}; hardware access belongs in ipm/device/."
    )


def test_the_afc_backend_offers_no_way_to_delete() -> None:
    """A regression guard with a name: ``delete_file`` must not come back here.

    It existed once and did real damage -- the file went, the library row did not.
    Re-adding a method with that name to this class would silently restore the old
    behaviour for anything that duck-types the backend.
    """
    tree = ast.parse(AFC_MODULE.read_text(encoding="utf-8"), filename=str(AFC_MODULE))
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    forbidden = {name for name in names if "delete" in name or "remove" in name}
    assert not forbidden, (
        f"ipm/device/afc.py defines {', '.join(sorted(forbidden))}; the file-system channel "
        "must not be able to delete. See ipm/device/ptp.py."
    )
