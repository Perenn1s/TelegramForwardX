import _local_dir  # noqa: F401  (isolates local data; must stay first)
import configparser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from browser_publisher import BrowserPublisher, BrowserSettings, BrowserUnavailable, BrowserAttention
from playwright.async_api import Error as PlaywrightError
from manual_browser import (endpoint_file, launch_command, manual_profile, path_size,
                            prune_profile, saved_endpoint, validate_endpoint, open_browser)


class ManualBrowserTests(unittest.TestCase):
    def test_only_local_browser_websocket_is_accepted(self):
        endpoint = "ws://127.0.0.1:9223/devtools/browser/123-abc"
        self.assertEqual(validate_endpoint(endpoint, 9223), endpoint)
        for value in ("ws://evil.invalid:9223/devtools/browser/abc",
                      "ws://127.0.0.1:9224/devtools/browser/abc",
                      "ws://user:pass@127.0.0.1:9223/devtools/browser/abc",
                      "ws://127.0.0.1:9223/devtools/page/abc",
                      "ws://127.0.0.1:9223/devtools/browser/abc?token=bad"):
            with self.assertRaises(ValueError):
                validate_endpoint(value, 9223)

    def test_no_blind_attach_to_an_existing_debug_port(self):
        endpoint_file("msedge").unlink(missing_ok=True)
        with self.assertRaisesRegex(ValueError, "--open-browser"):
            saved_endpoint(BrowserSettings())

    def test_saved_websocket_identity_is_preserved(self):
        marker = endpoint_file("msedge")
        self.addCleanup(marker.unlink, missing_ok=True)
        url = "ws://127.0.0.1:9223/devtools/browser/pinned-session"
        marker.write_text(json.dumps({"endpoint": url}))
        self.assertEqual(saved_endpoint(BrowserSettings()), url)

    def test_port_collision_does_not_launch_or_attach(self):
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        sock.connect_ex.return_value = 0
        with tempfile.TemporaryDirectory() as directory, \
             patch("manual_browser.socket.socket", return_value=sock), \
             patch("manual_browser.current_endpoint", return_value="ws://127.0.0.1:9223/devtools/browser/other"), \
             patch("manual_browser.subprocess.Popen") as launch:
            with self.assertRaisesRegex(ValueError, "occupied"):
                open_browser(directory, BrowserSettings())
            launch.assert_not_called()

    def test_manual_mode_cannot_be_hidden(self):
        config = configparser.ConfigParser()
        config.read_dict({"Browser": {"mode": "manual", "headless": "true"}})
        with self.assertRaisesRegex(ValueError, "headless=false"):
            BrowserSettings.from_config(config)


class ProfileSizeTests(unittest.TestCase):
    """The dedicated profile must stay small without a second manual login."""

    def build_profile(self, root, cache_mb=8):
        profile = Path(root)
        (profile / "Default" / "Network").mkdir(parents=True)
        (profile / "Default" / "Local Storage").mkdir()
        (profile / "Default" / "IndexedDB").mkdir()
        for login in (profile / "Local State", profile / "Default" / "Preferences",
                      profile / "Default" / "Network" / "Cookies",
                      profile / "Default" / "Local Storage" / "leveldb",
                      profile / "Default" / "IndexedDB" / "x.com.indexeddb"):
            login.write_bytes(b"session")
        for name in ("component_crx_cache", "ProvenanceData", "BrowserMetrics", "Crashpad"):
            (profile / name).mkdir()
            (profile / name / "payload").write_bytes(b"x" * 1024 * 1024)
        for name in ("Cache", "Code Cache", "Service Worker"):
            (profile / "Default" / name).mkdir()
            (profile / "Default" / name / "data").write_bytes(b"x" * (cache_mb * 1024 * 1024 // 3))
        return profile

    def assert_login_survives(self, profile):
        self.assertTrue((profile / "Local State").exists())
        self.assertTrue((profile / "Default" / "Network" / "Cookies").exists())
        self.assertTrue((profile / "Default" / "Preferences").exists())
        self.assertTrue((profile / "Default" / "Local Storage" / "leveldb").exists())
        self.assertTrue((profile / "Default" / "IndexedDB" / "x.com.indexeddb").exists())

    def test_every_cache_is_cleared_without_losing_the_x_login(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = self.build_profile(directory, cache_mb=30)
            for name in ("WidevineCdm", "Snapshots"):
                (profile / name).mkdir()
                (profile / name / "payload").write_bytes(b"x" * 1024)
            (profile / "BrowserMetrics-spare.pma").write_bytes(b"x" * 1024)
            freed = prune_profile(profile)
            self.assertGreater(freed, 30 * 1024 * 1024)
            self.assertLess(path_size(profile), 1024 * 1024)
            for name in ("component_crx_cache", "WidevineCdm", "Snapshots", "BrowserMetrics-spare.pma",
                         "Default/Service Worker", "Default/Cache", "Default/Code Cache"):
                self.assertFalse((profile / name).exists(), name)
            self.assert_login_survives(profile)

    def test_small_profile_is_still_left_without_page_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = self.build_profile(directory, cache_mb=1)
            self.assertGreater(prune_profile(profile), 0)
            self.assertFalse((profile / "Default" / "Cache").exists())
            self.assert_login_survives(profile)

    def test_missing_profile_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(prune_profile(Path(directory) / "absent"), 0)

    def test_old_profile_budget_setting_is_accepted_and_ignored(self):
        config = configparser.ConfigParser()
        config.read_dict({"Browser": {"max_profile_mb": "300"}})
        self.assertEqual(BrowserSettings.from_config(config), BrowserSettings())

    def test_launch_keeps_the_saved_profile_and_disables_caches(self):
        settings = BrowserSettings()
        profile = Path("C:/bot/state/x-browser-manual-msedge")
        command = launch_command("msedge.exe", profile, settings)
        self.assertIn(f"--user-data-dir={profile.resolve()}", command)
        self.assertIn(f"--remote-debugging-port={settings.debug_port}", command)
        for flag in ("--disable-background-networking", "--disable-component-update",
                     "--disable-breakpad", "--disable-sync",
                     "--disk-cache-size=1", "--media-cache-size=1"):
            self.assertIn(flag, command)
        self.assertEqual(command[-1], "https://x.com/home")

    def test_profile_is_pruned_before_launch_and_never_under_a_live_browser(self):
        order = Mock()
        with tempfile.TemporaryDirectory() as directory, \
             patch("manual_browser.port_in_use", return_value=False), \
             patch("manual_browser.browser_executable", return_value="msedge.exe"), \
             patch("manual_browser.current_endpoint", return_value="ws://127.0.0.1:9223/devtools/browser/new"), \
             patch("manual_browser.prune_profile", return_value=0) as prune, \
             patch("manual_browser.subprocess.Popen") as launch:
            order.attach_mock(prune, "prune")
            order.attach_mock(launch, "launch")
            open_browser(directory, BrowserSettings())
            prune.assert_called_once_with(manual_profile("msedge"))
            self.assertEqual([call[0] for call in order.mock_calls], ["prune", "launch"])
        with tempfile.TemporaryDirectory() as directory, \
             patch("manual_browser.port_in_use", return_value=True), \
             patch("manual_browser.current_endpoint", side_effect=ValueError("other")), \
             patch("manual_browser.prune_profile") as prune:
            with self.assertRaisesRegex(ValueError, "occupied"):
                open_browser(directory, BrowserSettings())
            prune.assert_not_called()


class DisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_tab_reconnects_without_closing_external_browser(self):
        p = BrowserPublisher('.', BrowserSettings())
        browser = Mock(is_connected=Mock(return_value=True), close=AsyncMock())
        p.attached_browser = browser
        p.page = Mock(is_closed=Mock(return_value=True), close=AsyncMock())
        p.playwright = Mock(stop=AsyncMock())
        p.start = AsyncMock()
        await p.ensure_ready()
        p.start.assert_awaited_once()
        browser.close.assert_not_awaited()

    async def test_open_tab_does_not_restart_browser(self):
        p = BrowserPublisher('.', BrowserSettings())
        p.page = Mock(is_closed=Mock(return_value=False))
        p.attached_browser = Mock(is_connected=Mock(return_value=True))
        p.start = AsyncMock()
        await p.ensure_ready()
        p.start.assert_not_awaited()

    async def test_closure_during_navigation_is_known_preclick_failure(self):
        p = BrowserPublisher('.', BrowserSettings())
        p.ensure_ready = AsyncMock()
        p.page = Mock(is_closed=Mock(return_value=True), evaluate=AsyncMock(return_value=[1920, 1080]),
                      add_locator_handler=AsyncMock(side_effect=PlaywrightError('closed')),
                      remove_locator_handler=AsyncMock())
        with self.assertRaises(BrowserUnavailable):
            await p.prepare('message')
        self.assertFalse(p.prepared)

    async def test_start_opens_saved_profile_before_attaching(self):
        p = BrowserPublisher('.', BrowserSettings())
        page = Mock(url='https://x.com/home', is_closed=Mock(return_value=False),
                    goto=AsyncMock(), set_default_timeout=Mock(), evaluate=AsyncMock(return_value=[1920, 1080]),
                    locator=Mock(return_value=Mock(wait_for=AsyncMock())))
        browser = Mock(contexts=[Mock(pages=[page], new_page=AsyncMock())])
        driver = Mock(chromium=Mock(connect_over_cdp=AsyncMock(return_value=browser)))
        p.check_account = AsyncMock()
        with patch('browser_publisher.open_browser') as launch, \
             patch('browser_publisher.saved_endpoint', return_value='ws://local'), \
             patch('browser_publisher.async_playwright', return_value=Mock(start=AsyncMock(return_value=driver))):
            await p.start_manual()
        launch.assert_called_once()
        driver.chromium.connect_over_cdp.assert_awaited_once_with('ws://local', timeout=45000, no_defaults=True)
        browser.contexts[0].new_page.assert_not_awaited()
        page.goto.assert_not_awaited()
        self.assertIs(p.page, page)

    async def test_start_managed_reuses_initial_blank_tab(self):
        with tempfile.TemporaryDirectory() as directory:
            p = BrowserPublisher(directory, BrowserSettings(mode='managed'))
            page = Mock(url='about:blank', is_closed=Mock(return_value=False), goto=AsyncMock())
            context = Mock(pages=[page], new_page=AsyncMock())
            driver = Mock(chromium=Mock(launch_persistent_context=AsyncMock(return_value=context)))
            p.verify_login = AsyncMock()
            with patch('browser_publisher.async_playwright', return_value=Mock(start=AsyncMock(return_value=driver))):
                await p.start()
            context.new_page.assert_not_awaited()
            page.goto.assert_awaited_once_with('https://x.com/home', wait_until='domcontentloaded')
            args = driver.chromium.launch_persistent_context.await_args.kwargs['args']
            self.assertEqual(args, ['--disk-cache-size=1', '--media-cache-size=1', '--no-proxy-server'])

    async def test_select_page_preserves_login_and_unrelated_tabs(self):
        p = BrowserPublisher('.', BrowserSettings())
        unrelated = Mock(url='https://example.org', is_closed=Mock(return_value=False), goto=AsyncMock())
        login = Mock(url='https://x.com/i/flow/login', is_closed=Mock(return_value=False))
        p.context = Mock(pages=[unrelated, login], new_page=AsyncMock())
        self.assertIs(await p.select_page(), login)
        p.context.new_page.assert_not_awaited()
        unrelated.goto.assert_not_awaited()

    async def test_select_page_waits_for_delayed_startup_tab(self):
        p = BrowserPublisher('.', BrowserSettings())
        page = Mock(url='https://x.com/home')
        p.context = Mock(pages=[], wait_for_event=AsyncMock(return_value=page), new_page=AsyncMock())
        self.assertIs(await p.select_page(), page)
        p.context.new_page.assert_not_awaited()

    async def test_select_page_creates_tab_if_only_unrelated_tabs_exist(self):
        p = BrowserPublisher('.', BrowserSettings())
        unrelated = Mock(url='https://example.org', is_closed=Mock(return_value=False), goto=AsyncMock())
        p.context = Mock(pages=[unrelated], new_page=AsyncMock())
        self.assertIs(await p.select_page(), p.context.new_page.return_value)
        unrelated.goto.assert_not_awaited()

    async def test_disconnect_leaves_external_browser_and_other_tabs_open(self):
        p = BrowserPublisher(".", BrowserSettings())
        p.context = context = Mock(close=AsyncMock())
        p.attached_browser = browser = Mock(close=AsyncMock())
        p.page = page = Mock(close=AsyncMock())
        p.playwright = runtime = Mock(stop=AsyncMock())
        await p.close()
        page.close.assert_awaited_once()
        context.close.assert_not_awaited()
        browser.close.assert_not_awaited()
        runtime.stop.assert_awaited_once()
        self.assertTrue(p.is_disconnected())

    async def test_unconfirmed_composer_stays_open_for_review(self):
        p = BrowserPublisher(".", BrowserSettings())
        p.keep_page = True
        p.page = page = Mock(close=AsyncMock())
        p.playwright = runtime = Mock(stop=AsyncMock())
        await p.close()
        page.close.assert_not_awaited()
        runtime.stop.assert_awaited_once()

    async def test_disconnect_while_reading_account_is_still_preclick(self):
        p = BrowserPublisher('.', BrowserSettings())
        p.page = Mock(is_closed=Mock(return_value=True), locator=Mock(return_value=Mock(
            wait_for=AsyncMock(), get_attribute=AsyncMock(side_effect=PlaywrightError('closed')))))
        with self.assertRaises(BrowserUnavailable):
            await p.check_account()

    async def test_idle_close_is_graceful_and_next_delivery_can_reopen(self):
        p = BrowserPublisher('.', BrowserSettings())
        p.context = Mock()
        p.page = Mock(close=AsyncMock())
        p.playwright = Mock(stop=AsyncMock())
        browser = Mock(is_connected=Mock(return_value=True))
        async def send(method):
            self.assertEqual(method, 'Browser.close')
            browser.is_connected.return_value = False
        session = Mock(send=AsyncMock(side_effect=send))
        browser.new_browser_cdp_session = AsyncMock(return_value=session)
        p.attached_browser = browser
        await p.close_idle()
        self.assertTrue(p.is_disconnected())
        p.start = AsyncMock()
        await p.ensure_ready()
        p.start.assert_awaited_once_with()

    async def test_idle_close_failure_keeps_connection_for_next_delivery(self):
        p = BrowserPublisher('.', BrowserSettings())
        p.context = Mock()
        p.page = Mock(is_closed=Mock(return_value=False))
        p.playwright = runtime = Mock(stop=AsyncMock())
        p.attached_browser = Mock(is_connected=Mock(return_value=True),
            new_browser_cdp_session=AsyncMock(side_effect=PlaywrightError('unsupported')))
        await p.close_idle()
        runtime.stop.assert_not_awaited()
        self.assertFalse(p.is_disconnected())
        p.start = AsyncMock()
        await p.ensure_ready()
        p.start.assert_not_awaited()

    async def test_closed_page_allows_cleanup_before_browser_disconnect_event(self):
        p = BrowserPublisher('.', BrowserSettings(timeout_seconds=1))
        p.context = Mock()
        p.page = Mock(is_closed=Mock(return_value=True), close=AsyncMock())
        p.playwright = runtime = Mock(stop=AsyncMock())
        browser = Mock(is_connected=Mock(return_value=True))
        browser.new_browser_cdp_session = AsyncMock(return_value=Mock(
            send=AsyncMock(side_effect=PlaywrightError('Target closed'))))
        p.attached_browser = browser
        await p.close_idle()
        runtime.stop.assert_awaited_once()
        self.assertIsNone(p.playwright)

    async def test_close_request_releases_transport_without_waiting_for_browser_exit(self):
        p = BrowserPublisher('.', BrowserSettings(timeout_seconds=1))
        p.context = Mock()
        p.page = Mock(is_closed=Mock(return_value=False), close=AsyncMock())
        p.playwright = runtime = Mock(stop=AsyncMock())
        p.attached_browser = Mock(is_connected=Mock(return_value=True),
            new_browser_cdp_session=AsyncMock(return_value=Mock(send=AsyncMock())))
        await p.close_idle()
        runtime.stop.assert_awaited_once()
        self.assertIsNone(p.playwright)

    async def test_idle_close_never_closes_failed_or_prepared_composer(self):
        for keep, prepared in ((True, False), (False, True)):
            p = BrowserPublisher('.', BrowserSettings())
            p.keep_page, p.prepared = keep, prepared
            p.context = Mock()
            p.attached_browser = Mock(new_browser_cdp_session=AsyncMock())
            await p.close_idle()
            p.attached_browser.new_browser_cdp_session.assert_not_awaited()


class StartupLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_login_pins_account_and_releases_window_only_after_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            p = BrowserPublisher(directory, BrowserSettings(login_timeout_seconds=900))
            p.state_dir.mkdir()
            p.keep_page = True
            link = Mock(wait_for=AsyncMock(), get_attribute=AsyncMock(return_value='/example_user'))
            p.page = Mock(locator=Mock(return_value=link))
            await p.verify_login(allow_login=True)
            self.assertEqual(json.loads(p.account_file.read_text())['username'], 'example_user')
            self.assertFalse(p.keep_page)
            self.assertEqual(link.wait_for.await_args_list[0].kwargs['timeout'], 900000)

    async def test_login_to_wrong_account_never_overwrites_pinned_account(self):
        with tempfile.TemporaryDirectory() as directory:
            p = BrowserPublisher(directory, BrowserSettings())
            p.state_dir.mkdir()
            p.account_file.write_text(json.dumps({'username': 'original'}))
            p.keep_page = True
            p.page = Mock(locator=Mock(return_value=Mock(
                wait_for=AsyncMock(), get_attribute=AsyncMock(return_value='/wrong'))))
            with self.assertRaisesRegex(BrowserAttention, 'Wrong X account'):
                await p.verify_login(allow_login=True)
            self.assertEqual(json.loads(p.account_file.read_text())['username'], 'original')
            self.assertTrue(p.keep_page)

    async def test_login_timeout_keeps_window_for_manual_completion(self):
        p = BrowserPublisher('.', BrowserSettings())
        p.keep_page = True
        p.page = Mock(locator=Mock(return_value=Mock(wait_for=AsyncMock(side_effect=PlaywrightError('timeout')))))
        p.check_account = AsyncMock()
        with self.assertRaises(PlaywrightError):
            await p.verify_login(allow_login=True)
        self.assertTrue(p.keep_page)
        p.check_account.assert_not_awaited()
