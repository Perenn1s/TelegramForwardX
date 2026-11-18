"""Per-machine runtime data, kept outside the (possibly cloud-synced) program folder.

The program folder holds only small shared files: the EXE, config.ini, the
Telegram session, the delivery journal, logs and the pinned X account. Bulky or
fast-changing data lives under %LOCALAPPDATA%\\MemeBot on each computer:

* the dedicated browser profile, whose X offline cache is rebuilt (~150 MB) on
  every launch and is cleared before the next one;
* the browser's local control endpoint;
* media downloaded from Telegram while one post is being prepared.

Every deletion in this module is guarded: only known names, only inside an
expected root, never through a symlink/junction, only while nothing uses it,
and every removal is logged.
"""

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import time

from delivery_store import instance_lock

LOCAL_DIR_ENV = "MEMEBOT_LOCAL_DIR"
MIGRATED_MARKER = "MOVED-TO-LOCAL-APPDATA.txt"
RUNTIME_MARKER = "memebot-runtime.json"
MEDIA_PREFIX = "forward-"
# tempfile.TemporaryDirectory names: prefix + 8 chars from [a-z0-9_].
STALE_MEDIA = re.compile(rf"^{MEDIA_PREFIX}[a-z0-9_]{{8}}$")
CHANNELS = ("msedge", "chrome")
# Expected size of a pruned, closed profile is ~20 MB, and ~170 MB while X's
# offline cache is loaded. More than this right after pruning means something
# unknown is growing; it is reported, never deleted blindly.
PROFILE_WARN_BYTES = 300 * 1024 ** 2

log = logging.getLogger(__name__)


def local_root():
    override = os.environ.get(LOCAL_DIR_ENV)
    if override:
        root = Path(override)
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        root = Path(base) / "MemeBot"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "MemeBot"
    root.mkdir(parents=True, exist_ok=True)
    return root


def manual_profile(channel):
    return local_root() / f"x-browser-manual-{channel}"


def managed_profile():
    return local_root() / "x-browser"


def endpoint_file(channel):
    return local_root() / f"x-browser-endpoint-{channel}.json"


def media_temp_dir():
    path = local_root() / "tmp"
    path.mkdir(exist_ok=True)
    return path


@contextmanager
def runtime_lock():
    """One MemeBot per computer may use the shared local data at a time."""
    with instance_lock(local_root() / "runtime.lock"):
        yield


def is_link(path):
    path = Path(path)
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def path_size(path):
    """Bytes held by a file or directory tree. Links are not followed."""
    path = Path(path)
    try:
        if is_link(path):
            return 0
        if path.is_dir():
            return sum(path_size(child) for child in path.iterdir())
        return path.stat().st_size
    except OSError:
        return 0


def inside(path, root):
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except OSError:
        return False


def remove_tree(path, root):
    """Delete one directory or file that must lie strictly inside root.

    A link is never followed: only the link itself would be removed, and it
    is refused here because none of MemeBot's own data is ever a link.
    Returns the bytes actually reclaimed. Locked files are left in place.
    """
    path, root = Path(path), Path(root)
    if is_link(path) or not path.exists():
        return 0
    if not inside(path, root) or path.resolve() == root.resolve():
        log.warning("Refused to delete %s: outside %s.", path, root)
        return 0
    before = path_size(path)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            pass
    return before - path_size(path)


def profile_in_use(profile):
    """True while a Chromium-based browser holds this user-data directory.

    On Windows the running browser keeps "lockfile" open without sharing and
    deletes it on exit, so an existing file that cannot be opened proves a
    live browser even when its control port is closed. A stale lockfile left
    by a crash can be opened and does not count as in use.
    """
    lockfile = Path(profile) / "lockfile"
    if os.name != "nt":
        return (Path(profile) / "SingletonLock").is_symlink()
    if not lockfile.exists():
        return False
    try:
        handle = os.open(lockfile, os.O_RDWR)
    except PermissionError:
        return True
    except OSError:
        return False
    os.close(handle)
    return False


def file_manifest(root):
    root = Path(root)
    return {str(path.relative_to(root)): path.stat().st_size
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


def legacy_profiles(base_dir):
    state = Path(base_dir) / "state"
    pairs = [(state / f"x-browser-manual-{channel}", manual_profile(channel)) for channel in CHANNELS]
    return pairs + [(state / "x-browser", managed_profile())]


def local_appdata():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def appdata_redirected():
    """True when Windows silently redirects this process's %LOCALAPPDATA% writes.

    That happens when MemeBot is started from inside a packaged (MSIX) app,
    such as an app's built-in terminal: files land in that app's private
    Packages/<app>/LocalCache folder, invisible to MemeBot started normally.
    The process may not even report a package identity, so this checks the
    behaviour directly with a uniquely named, immediately removed probe file.
    A custom MEMEBOT_LOCAL_DIR is not under %LOCALAPPDATA% and is not probed.
    """
    if os.name != "nt" or os.environ.get(LOCAL_DIR_ENV):
        return False
    appdata = local_appdata()
    probe = appdata / f"memebot-redirect-probe-{os.getpid()}-{time.time_ns()}.tmp"
    try:
        probe.write_text("", encoding="utf-8")
    except OSError:
        return False
    try:
        packages = [entry for entry in (appdata / "Packages").iterdir() if entry.is_dir()]
    except OSError:
        packages = []
    try:
        return any((package / "LocalCache" / "Local" / probe.name).exists() for package in packages)
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def move_profile(source, target, source_root, prune=None, *, reason):
    """Copy -> verify every file and size -> switch -> mark -> delete source.

    Any doubt leaves the source untouched; an existing target is never
    overwritten or merged. Returns True once the target holds the profile.
    """
    if is_link(source) or not source.is_dir():
        log.warning("Not moving %s: it is not a plain folder.", source)
        return False
    if (source / MIGRATED_MARKER).is_file() and target.is_dir():
        # A previous move completed but could not delete every old file.
        freed = remove_tree(source, source_root)
        log.info("Removed %.0f MB left from the earlier move of %s.", freed / 1024 ** 2, source)
        return True
    if target.exists():
        log.warning("Kept %s: %s already exists on this computer, so the old copy was not merged or deleted. "
                    "Delete the old folder by hand once you no longer need it.", source, target)
        return False
    if profile_in_use(source):
        log.warning("Not moving %s yet: a browser is still using it. Close that window; it moves on the next start.",
                    source)
        return False
    if prune is not None:
        prune(source)  # Allow-listed caches only; shrinks the copy.
    staging = target.with_name(f"{target.name}.moving-{os.getpid()}")
    remove_tree(staging, local_root())
    try:
        shutil.copytree(source, staging, symlinks=True)
        if file_manifest(staging) != file_manifest(source):
            raise OSError("copied files differ from the original")
        os.replace(staging, target)
    except OSError as error:
        remove_tree(staging, local_root())
        log.warning("Could not move %s to %s (%s); the old copy is kept. Retrying on next start.",
                    source, target, error)
        return False
    (source / MIGRATED_MARKER).write_text(f"Moved to {target} at {time.strftime('%Y-%m-%d %H:%M:%S')}.\n",
                                          encoding="utf-8")
    freed = remove_tree(source, source_root)
    log.info("Moved %s to %s (%s); freed %.0f MB there. The X login is kept.",
             source, target, reason, freed / 1024 ** 2)
    return True


def packaged_app_copies(name):
    """MemeBot profiles that a packaged app's private storage captured."""
    try:
        found = list((local_appdata() / "Packages").glob(f"*/LocalCache/Local/MemeBot/{name}"))
    except OSError:
        return []
    return [path for path in found if path.is_dir() and not is_link(path)
            and (path / "Default" / "Network" / "Cookies").is_file()]


def tidy_packaged_app_folder(folder):
    """After adopting its profiles, drop MemeBot's own small leftovers there."""
    for name in ["runtime.lock", *(f"x-browser-endpoint-{channel}.{ext}" for channel in CHANNELS
                                   for ext in ("json", "tmp"))]:
        remove_tree(folder / name, folder)
    media = folder / "tmp"
    if media.is_dir() and not is_link(media):
        for entry in list(media.iterdir()):
            if STALE_MEDIA.match(entry.name) and entry.is_dir() and not is_link(entry):
                remove_tree(entry, media)
        with_suppressed_rmdir(media)
    with_suppressed_rmdir(folder)  # Only succeeds when nothing else is left.


def with_suppressed_rmdir(folder):
    try:
        folder.rmdir()
    except OSError:
        pass


def adopt_packaged_app_profiles(prune=None):
    """Recover a browser login that a packaged app's sandbox captured.

    If MemeBot once ran from inside a packaged app, its profile landed in
    that app's private copy of %LOCALAPPDATA%. Started normally, MemeBot
    moves the newest such login here when this computer has none yet.
    """
    if os.name != "nt" or os.environ.get(LOCAL_DIR_ENV):
        return
    for name in [f"x-browser-manual-{channel}" for channel in CHANNELS] + ["x-browser"]:
        target = local_root() / name
        if target.exists():
            continue
        copies = packaged_app_copies(name)
        if not copies:
            continue
        source = max(copies, key=lambda path: (path / "Default" / "Network" / "Cookies").stat().st_mtime)
        if move_profile(source, target, source.parent, prune, reason="recovered from a packaged app's private storage"):
            tidy_packaged_app_folder(source.parent)


def migrate_legacy_storage(base_dir, prune=None):
    """Move browser data out of the program folder, once, without loss.

    Call only while holding the program's instance lock. Inside a packaged
    app nothing is moved: it would land in that app's private storage.
    """
    if appdata_redirected():
        log.warning("MemeBot was started from inside another app's sandbox (for example an app's built-in terminal). "
                    "Windows keeps its %%LOCALAPPDATA%% private to that app, so no browser data is moved now. "
                    "Start MemeBot.exe or telegram.bat directly.")
        return
    state = Path(base_dir) / "state"
    for legacy, target in legacy_profiles(base_dir):
        if legacy.exists():
            move_profile(legacy, target, state, prune, reason="out of the program folder")
    adopt_packaged_app_profiles(prune)
    for channel in CHANNELS:
        # Derived pointer, rewritten at every launch; the new location is local.
        for name in (f"x-browser-endpoint-{channel}.json", f"x-browser-endpoint-{channel}.tmp"):
            remove_tree(state / name, state)


def clean_stale_media(base_dir):
    """Delete leftover per-post media folders after a crash or forced stop.

    Only folders named exactly like MemeBot's temporary media folders are
    touched, and only under the instance lock, so none is in use.
    """
    freed = 0
    for folder in (media_temp_dir(), Path(base_dir)):
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            if STALE_MEDIA.match(entry.name) and entry.is_dir() and not is_link(entry):
                freed += remove_tree(entry, folder)
    if freed:
        log.info("Removed %.0f MB of media left by an interrupted post.", freed / 1024 ** 2)
    return freed


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # Exists but not ours to query.
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def mark_runtime(current=None):
    """Tag this EXE's unpacked runtime folder so later runs can recognise it."""
    current = current or getattr(sys, "_MEIPASS", None)
    if not current:
        return
    try:
        (Path(current) / RUNTIME_MARKER).write_text(
            json.dumps({"pids": [os.getpid(), os.getppid()], "started": time.time()}), encoding="utf-8")
    except OSError:
        pass


def clean_stale_runtimes(current=None):
    """Delete unpacked EXE runtimes (~120 MB each) left by forced stops.

    Only folders carrying MemeBot's own marker are considered, never the
    current one, and only when every process that used it has exited. Other
    programs' _MEI folders carry no marker and are never touched.
    """
    current = current or getattr(sys, "_MEIPASS", None)
    if not current:
        return 0
    current = Path(current).resolve()
    parent = current.parent
    freed = 0
    for folder in parent.glob("_MEI*"):
        if not folder.is_dir() or is_link(folder) or folder.resolve() == current:
            continue
        try:
            pids = json.loads((folder / RUNTIME_MARKER).read_text(encoding="utf-8"))["pids"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not isinstance(pids, list) or any(process_alive(pid) for pid in pids):
            continue
        freed += remove_tree(folder, parent)
    if freed:
        log.info("Removed %.0f MB of unpacked runtime files left by earlier forced stops.", freed / 1024 ** 2)
    return freed


def check_profile_size(profile):
    """Report unexpected growth after pruning; never delete unknown data."""
    size = path_size(profile)
    if size <= PROFILE_WARN_BYTES:
        return size
    profile = Path(profile)
    # Folders one and two levels down (e.g. "Default/Service Worker").
    candidates = [child for top in profile.iterdir() if top.is_dir() and not is_link(top)
                  for child in [top, *(entry for entry in top.iterdir() if entry.is_dir() and not is_link(entry))]]
    largest = sorted(((path_size(child), child) for child in candidates if child.parent != profile
                      or not any(other.parent == child for other in candidates)),
                     key=lambda item: item[0], reverse=True)[:3]
    log.warning("Browser profile is still %.0f MB after cleanup. Largest folders: %s. Nothing else was deleted; "
                "report this so the cleanup list can be reviewed.", size / 1024 ** 2,
                ", ".join(f"{path.relative_to(profile)} {bytes_ / 1024 ** 2:.0f} MB" for bytes_, path in largest))
    return size


KNOWN_STATE = re.compile(r"^(deliveries\.sqlite3(-wal|-shm|-journal)?|(forwarder|supervisor)\.(log(\.\d+)?|lock)"
                         r"|x-browser-account\.json)$")


def storage_report(base_dir, settings=None):
    """Plain-text summary for --status and the startup log. Reads only."""
    mb = lambda value: f"{value / 1024 ** 2:.0f} MB" if value >= 1024 ** 2 else f"{value / 1024:.0f} KB"
    state = Path(base_dir) / "state"
    root = local_root()
    lines = [f"Program folder state: {mb(path_size(state))} ({state})",
             f"This computer's MemeBot data: {mb(path_size(root))} ({root})"]
    for profile in [manual_profile(channel) for channel in CHANNELS] + [managed_profile()]:
        if profile.exists():
            active = settings is not None and profile == (manual_profile(settings.channel)
                                                          if settings.mode == "manual" else managed_profile())
            lines.append(f"  {profile.name}: {mb(path_size(profile))}" + ("" if active else " (not used by current settings)"))
    media = path_size(media_temp_dir())
    if media:
        lines.append(f"  media being prepared or left over: {mb(media)}")
    unknown = []
    if state.is_dir():
        for entry in state.iterdir():
            if not KNOWN_STATE.match(entry.name):
                unknown.append(f"{entry.name} {mb(path_size(entry))}")
    if unknown:
        lines.append("  Not MemeBot's current files in state (review, delete by hand if unneeded): " + ", ".join(sorted(unknown)))
    return lines
