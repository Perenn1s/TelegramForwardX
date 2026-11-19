import _local_dir  # noqa: F401  (isolates local data; must stay first)
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import local_storage
from local_storage import (MIGRATED_MARKER, RUNTIME_MARKER, check_profile_size, clean_stale_media,
                           clean_stale_runtimes, endpoint_file, local_root, manual_profile, managed_profile,
                           media_temp_dir, migrate_legacy_storage, profile_in_use, remove_tree, storage_report)
from manual_browser import open_browser, prune_everything, prune_profile
from browser_publisher import BrowserSettings


def build_profile(root, cache_mb=1):
    root = Path(root)
    for relative, data in {
        "Local State": b"{}",
        "Default/Network/Cookies": b"cookie",
        "Default/Preferences": b"{}",
        "Default/Local Storage/leveldb/000003.log": b"session",
        "Default/IndexedDB/x.com.indexeddb/1": b"db",
        "Default/Service Worker/CacheStorage/blob": b"x" * (cache_mb * 1024 * 1024),
        "ShaderCache/data": b"x" * 1024,
    }.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(data)
    return root


def login_files(root):
    return {name: (Path(root) / name).read_bytes() for name in (
        "Local State", "Default/Network/Cookies", "Default/Preferences",
        "Default/Local Storage/leveldb/000003.log", "Default/IndexedDB/x.com.indexeddb/1")}


class LocalStorageTests(unittest.TestCase):
    def setUp(self):
        # Fresh per-machine directory and program folder for every test.
        self.local = tempfile.TemporaryDirectory()
        self.addCleanup(self.local.cleanup)
        env = patch.dict(os.environ, {"MEMEBOT_LOCAL_DIR": self.local.name})
        env.start()
        self.addCleanup(env.stop)
        self.program = tempfile.TemporaryDirectory()
        self.addCleanup(self.program.cleanup)
        self.state = Path(self.program.name) / "state"
        self.state.mkdir()

    def test_default_location_is_per_user_local_appdata(self):
        with tempfile.TemporaryDirectory() as appdata, \
             patch.dict(os.environ, {"LOCALAPPDATA": appdata}), patch.object(local_storage.os, "name", "nt"):
            os.environ.pop("MEMEBOT_LOCAL_DIR")
            self.assertEqual(local_root(), Path(appdata) / "MemeBot")
            self.assertEqual(manual_profile("msedge"), Path(appdata) / "MemeBot" / "x-browser-manual-msedge")

    def test_profile_moves_out_of_program_folder_with_login_intact(self):
        legacy = build_profile(self.state / "x-browser-manual-msedge", cache_mb=2)
        before = login_files(legacy)
        (self.state / "x-browser-endpoint-msedge.json").write_text("{}")
        migrate_legacy_storage(self.program.name, prune=prune_everything)
        target = manual_profile("msedge")
        self.assertFalse(legacy.exists())
        self.assertFalse((self.state / "x-browser-endpoint-msedge.json").exists())
        self.assertEqual(login_files(target), before)
        # The copy was pruned first: X's offline cache is not carried along.
        self.assertFalse((target / "Default" / "Service Worker").exists())
        self.assertFalse(any(path.name.endswith(".moving-" + str(os.getpid())) for path in local_root().iterdir()))

    def test_existing_local_profile_is_never_overwritten_or_merged(self):
        legacy = build_profile(self.state / "x-browser-manual-msedge")
        target = build_profile(manual_profile("msedge"))
        (target / "Default" / "Network" / "Cookies").write_bytes(b"newer login")
        migrate_legacy_storage(self.program.name, prune=prune_everything)
        self.assertTrue(legacy.exists())
        self.assertEqual((target / "Default" / "Network" / "Cookies").read_bytes(), b"newer login")
        self.assertEqual((legacy / "Default" / "Network" / "Cookies").read_bytes(), b"cookie")

    def test_profile_in_use_is_not_moved_or_pruned(self):
        legacy = build_profile(self.state / "x-browser-manual-msedge")
        with patch("local_storage.profile_in_use", return_value=True), \
             patch("manual_browser.profile_in_use", return_value=True):
            migrate_legacy_storage(self.program.name, prune=prune_everything)
            with self.assertRaisesRegex(ValueError, "still open"):
                prune_profile(legacy)
        self.assertTrue((legacy / "Default" / "Service Worker").exists())
        self.assertFalse(manual_profile("msedge").exists())

    def test_failed_verification_keeps_the_original(self):
        legacy = build_profile(self.state / "x-browser-manual-msedge")
        before = login_files(legacy)
        real_manifest = local_storage.file_manifest

        def lossy_manifest(root):
            # The copy appears to be missing the cookie database.
            manifest = real_manifest(root)
            if ".moving-" in str(root):
                manifest.pop(os.path.join("Default", "Network", "Cookies"))
            return manifest

        with patch("local_storage.file_manifest", side_effect=lossy_manifest):
            migrate_legacy_storage(self.program.name, prune=prune_everything)
        self.assertEqual(login_files(legacy), before)
        self.assertFalse(manual_profile("msedge").exists())
        self.assertEqual([path for path in local_root().iterdir() if "moving" in path.name], [])

    def test_interrupted_cleanup_of_moved_copy_is_finished_later(self):
        legacy = build_profile(self.state / "x-browser-manual-msedge")
        build_profile(manual_profile("msedge"))
        (legacy / MIGRATED_MARKER).write_text("moved")
        migrate_legacy_storage(self.program.name)
        self.assertFalse(legacy.exists())
        self.assertTrue((manual_profile("msedge") / "Default" / "Network" / "Cookies").exists())

    def test_managed_profile_moves_too(self):
        build_profile(self.state / "x-browser")
        migrate_legacy_storage(self.program.name)
        self.assertTrue((managed_profile() / "Default" / "Network" / "Cookies").exists())
        self.assertFalse((self.state / "x-browser").exists())

    def test_unrelated_state_files_are_never_touched(self):
        for name in ("deliveries.sqlite3", "forwarder.log", "x-browser-account.json", "x-browser-notes.txt"):
            (self.state / name).write_text("keep")
        (self.state / "live-check").mkdir()
        migrate_legacy_storage(self.program.name)
        for name in ("deliveries.sqlite3", "forwarder.log", "x-browser-account.json", "x-browser-notes.txt", "live-check"):
            self.assertTrue((self.state / name).exists(), name)

    def test_remove_tree_refuses_paths_outside_its_root_and_the_root_itself(self):
        outside = Path(self.program.name) / "outside.txt"
        outside.write_text("keep")
        self.assertEqual(remove_tree(outside, self.state), 0)
        self.assertEqual(remove_tree(self.state / ".." / "outside.txt", self.state), 0)
        self.assertEqual(remove_tree(self.state, self.state), 0)
        self.assertTrue(outside.exists() and self.state.exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_links_are_never_followed(self):
        import _winapi
        precious = Path(self.program.name) / "precious"
        precious.mkdir()
        (precious / "file").write_text("keep")
        link = self.state / "x-browser-manual-msedge"
        _winapi.CreateJunction(str(precious), str(link))
        migrate_legacy_storage(self.program.name, prune=prune_everything)
        self.assertEqual(remove_tree(link, self.state), 0)
        self.assertTrue((precious / "file").exists())
        # A junction placed inside a profile cache is not followed either.
        profile = build_profile(manual_profile("chrome"))
        shutil.rmtree(profile / "ShaderCache")
        _winapi.CreateJunction(str(precious), str(profile / "ShaderCache"))
        prune_profile(profile)
        self.assertTrue((precious / "file").exists())

    @unittest.skipUnless(os.name == "nt", "Windows browser lockfile")
    def test_running_browser_lockfile_is_detected(self):
        import _winapi
        profile = build_profile(manual_profile("msedge"))
        self.assertFalse(profile_in_use(profile))
        (profile / "lockfile").write_text("")
        self.assertFalse(profile_in_use(profile))  # Stale after a crash.
        handle = _winapi.CreateFile(str(profile / "lockfile"), 0xC0000000, 0, 0, 3, 0, 0)  # no sharing
        try:
            self.assertTrue(profile_in_use(profile))
            with self.assertRaisesRegex(ValueError, "still open"):
                prune_profile(profile)
        finally:
            _winapi.CloseHandle(handle)
        self.assertTrue((profile / "Default" / "Service Worker").exists())

    def test_open_browser_never_prunes_a_profile_held_without_debug_port(self):
        build_profile(manual_profile("msedge"))
        with patch("manual_browser.port_in_use", return_value=False), \
             patch("manual_browser.profile_in_use", return_value=True), \
             patch("manual_browser.browser_executable", return_value="msedge.exe"), \
             patch("manual_browser.subprocess.Popen") as launch:
            with self.assertRaisesRegex(ValueError, "still open"):
                open_browser(self.program.name, BrowserSettings())
            launch.assert_not_called()
        self.assertTrue((manual_profile("msedge") / "Default" / "Service Worker").exists())

    def test_open_browser_writes_endpoint_locally_not_in_program_folder(self):
        with patch("manual_browser.port_in_use", return_value=False), \
             patch("manual_browser.browser_executable", return_value="msedge.exe"), \
             patch("manual_browser.current_endpoint", return_value="ws://127.0.0.1:9223/devtools/browser/new"), \
             patch("manual_browser.subprocess.Popen") as launch:
            open_browser(self.program.name, BrowserSettings())
        self.assertIn(f"--user-data-dir={manual_profile('msedge').resolve()}", launch.call_args[0][0])
        self.assertTrue(endpoint_file("msedge").exists())
        self.assertEqual(list(self.state.iterdir()), [])

    def test_only_memebot_media_folders_are_cleaned(self):
        stale_local = media_temp_dir() / "forward-ab12_x9z"
        stale_program = Path(self.program.name) / "forward-q1w2e3r4"
        for folder in (stale_local, stale_program):
            folder.mkdir()
            (folder / "clip.mp4").write_bytes(b"x" * 2048)
        keep = [Path(self.program.name) / "forward-notes", Path(self.program.name) / "forwarder-old",
                media_temp_dir() / "forward-TOOLONGNAME"]
        for folder in keep:
            folder.mkdir()
        (Path(self.program.name) / "forward-abcdefgh").write_text("a file, not a folder")
        self.assertGreater(clean_stale_media(self.program.name), 0)
        self.assertFalse(stale_local.exists() or stale_program.exists())
        for folder in keep:
            self.assertTrue(folder.exists(), folder)
        self.assertTrue((Path(self.program.name) / "forward-abcdefgh").exists())

    def test_only_exited_memebot_runtimes_are_removed(self):
        temp = Path(self.local.name) / "Temp"
        current, dead, alive, foreign, garbled = (temp / name for name in
                                                  ("_MEI1", "_MEI2", "_MEI3", "_MEI4", "_MEI5"))
        for folder in (current, dead, alive, foreign, garbled):
            folder.mkdir(parents=True)
            (folder / "python312.dll").write_bytes(b"x" * 4096)
        (current / RUNTIME_MARKER).write_text(json.dumps({"pids": [os.getpid()]}))
        (dead / RUNTIME_MARKER).write_text(json.dumps({"pids": [999999991, 999999992]}))
        (alive / RUNTIME_MARKER).write_text(json.dumps({"pids": [999999993, os.getpid()]}))
        (garbled / RUNTIME_MARKER).write_text("not json")
        with patch("local_storage.process_alive", side_effect=lambda pid: pid == os.getpid()):
            self.assertGreater(clean_stale_runtimes(current), 0)
        self.assertFalse(dead.exists())
        for folder in (current, alive, foreign, garbled):
            self.assertTrue(folder.exists(), folder)

    def use_real_appdata_layout(self):
        """Point %LOCALAPPDATA% at a temp folder and drop the test override."""
        appdata = Path(self.local.name) / "AppData" / "Local"
        appdata.mkdir(parents=True)
        env = patch.dict(os.environ, {"LOCALAPPDATA": str(appdata)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MEMEBOT_LOCAL_DIR")
        redirect = patch("local_storage.appdata_redirected", return_value=False)
        redirect.start()
        self.addCleanup(redirect.stop)
        return appdata

    def sandboxed_copy(self, appdata, package, cookie=b"cookie", mtime=None):
        folder = appdata / "Packages" / package / "LocalCache" / "Local" / "MemeBot"
        profile = build_profile(folder / "x-browser-manual-msedge")
        (profile / "Default" / "Network" / "Cookies").write_bytes(cookie)
        if mtime:
            os.utime(profile / "Default" / "Network" / "Cookies", (mtime, mtime))
        (folder / "runtime.lock").write_text("0")
        (folder / "x-browser-endpoint-msedge.json").write_text("{}")
        (folder / "tmp").mkdir()
        return folder

    def test_login_captured_by_a_packaged_app_is_recovered_automatically(self):
        appdata = self.use_real_appdata_layout()
        folder = self.sandboxed_copy(appdata, "Claude_test")
        migrate_legacy_storage(self.program.name, prune=prune_everything)
        target = appdata / "MemeBot" / "x-browser-manual-msedge"
        self.assertEqual((target / "Default" / "Network" / "Cookies").read_bytes(), b"cookie")
        self.assertTrue((target / "Local State").exists())
        # The sandboxed copy and MemeBot's own small leftovers there are gone.
        self.assertFalse(folder.exists())
        self.assertTrue((appdata / "Packages" / "Claude_test" / "LocalCache" / "Local").exists())

    def test_newest_captured_login_wins_and_others_are_left_alone(self):
        appdata = self.use_real_appdata_layout()
        old = self.sandboxed_copy(appdata, "Old_app", cookie=b"old", mtime=1_000_000)
        new = self.sandboxed_copy(appdata, "New_app", cookie=b"new", mtime=2_000_000)
        migrate_legacy_storage(self.program.name)
        self.assertEqual((appdata / "MemeBot" / "x-browser-manual-msedge" / "Default" / "Network" / "Cookies")
                         .read_bytes(), b"new")
        self.assertFalse(new.exists())
        self.assertTrue((old / "x-browser-manual-msedge" / "Default" / "Network" / "Cookies").exists())

    def test_existing_login_on_this_computer_is_never_replaced(self):
        appdata = self.use_real_appdata_layout()
        folder = self.sandboxed_copy(appdata, "Claude_test", cookie=b"sandboxed")
        build_profile(appdata / "MemeBot" / "x-browser-manual-msedge")
        migrate_legacy_storage(self.program.name)
        self.assertEqual((appdata / "MemeBot" / "x-browser-manual-msedge" / "Default" / "Network" / "Cookies")
                         .read_bytes(), b"cookie")
        self.assertTrue((folder / "x-browser-manual-msedge" / "Default" / "Network" / "Cookies").exists())

    def test_sandbox_folder_with_unknown_files_is_kept(self):
        appdata = self.use_real_appdata_layout()
        folder = self.sandboxed_copy(appdata, "Claude_test")
        (folder / "notes.txt").write_text("not MemeBot's")
        migrate_legacy_storage(self.program.name)
        self.assertTrue((folder / "notes.txt").exists())
        self.assertFalse((folder / "x-browser-manual-msedge").exists())

    def test_nothing_moves_while_started_inside_a_packaged_app(self):
        legacy = build_profile(self.state / "x-browser-manual-msedge")
        with patch("local_storage.appdata_redirected", return_value=True), \
             self.assertLogs("local_storage", "WARNING") as logs:
            migrate_legacy_storage(self.program.name, prune=prune_everything)
        self.assertIn("sandbox", "\n".join(logs.output))
        self.assertTrue((legacy / "Default" / "Service Worker").exists())
        self.assertFalse(manual_profile("msedge").exists())

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_redirect_probe_detects_a_sandboxed_write(self):
        appdata = Path(self.local.name) / "AppData" / "Local"
        captured = appdata / "Packages" / "Some_app" / "LocalCache" / "Local"
        captured.mkdir(parents=True)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(appdata)}):
            os.environ.pop("MEMEBOT_LOCAL_DIR")
            self.assertFalse(local_storage.appdata_redirected())
            name = f"memebot-redirect-probe-{os.getpid()}-123.tmp"
            (captured / name).write_text("")  # As if Windows had redirected the probe.
            with patch("local_storage.time.time_ns", return_value=123):
                self.assertTrue(local_storage.appdata_redirected())
        self.assertEqual([path.name for path in appdata.iterdir()], ["Packages"])  # Probe removed.

    def test_process_check_recognises_this_process(self):
        self.assertTrue(local_storage.process_alive(os.getpid()))
        self.assertFalse(local_storage.process_alive(0))

    def test_unexpected_growth_is_reported_not_deleted(self):
        profile = build_profile(manual_profile("msedge"))
        (profile / "Default" / "Mystery").mkdir()
        (profile / "Default" / "Mystery" / "blob").write_bytes(b"x" * 4096)
        with patch("local_storage.PROFILE_WARN_BYTES", 1024), self.assertLogs("local_storage", "WARNING") as logs:
            check_profile_size(profile)
        self.assertIn("Mystery", "\n".join(logs.output))
        self.assertTrue((profile / "Default" / "Mystery" / "blob").exists())

    def test_storage_report_flags_unused_and_unknown_items(self):
        build_profile(manual_profile("msedge"))
        build_profile(managed_profile())
        (self.state / "deliveries.sqlite3").write_text("db")
        (self.state / "forwarder.log.1").write_text("old log")
        (self.state / "live-check").mkdir()
        report = "\n".join(storage_report(self.program.name, BrowserSettings()))
        self.assertIn("x-browser-manual-msedge", report)
        self.assertIn("x-browser: ", report)
        self.assertIn("(not used by current settings)", report.split("x-browser: ")[1].splitlines()[0])
        self.assertIn("live-check", report)
        self.assertNotIn("forwarder.log.1", report)


if __name__ == "__main__":
    unittest.main()
