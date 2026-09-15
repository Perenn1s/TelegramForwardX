"""Publish through X's visible website. No API calls, cookies exports or passwords."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, expect
from manual_browser import manual_profile, saved_endpoint, open_browser

log = logging.getLogger(__name__)
PROFILE_LINK = '[data-testid="AppTabBar_Profile_Link"]'
EDITOR = '[data-testid="tweetTextarea_0"][contenteditable="true"]'
POST_BUTTON = '[data-testid="tweetButton"], [data-testid="tweetButtonInline"]'
# Scope to the visible dialog containing the editor. X can omit aria-modal
# during rendering; the home timeline's inline editor must never be selected.
COMPOSER = '[role="dialog"]:visible'
SAVE_POST_DIALOG = '[data-testid="confirmationSheetDialog"]'
SAVE_POST_TITLE = re.compile(r'Save (?:post|Tweet)\?|保存(?:帖子|推文)[？?]?', re.IGNORECASE)
# X retains both the character counter and a zero-width idle upload bar.
# Real/indeterminate progress remains blocking; Post must also be enabled.
UPLOAD_PROGRESS = ('[role="progressbar"]:visible:not([data-testid="countdown-circle"] *)'
                   ':not([aria-valuenow="0"]:has([data-testid="progressBar-bar"][style*="width: 0%"]))')
STATUS_PATH = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d+)(?:/.*)?$")


class BrowserAttention(RuntimeError):
    """Login, account selection, or website UI needs a human before posting."""


class BrowserUnavailable(BrowserAttention):
    """Browser/tab disconnected before any publish click; safe to retry."""


@dataclass(frozen=True)
class BrowserSettings:
    channel: str = "msedge"
    expected_username: str = ""
    timeout_seconds: int = 45
    upload_timeout_seconds: int = 180
    login_timeout_seconds: int = 600
    headless: bool = False
    mode: str = "manual"
    debug_port: int = 9223

    @classmethod
    def from_config(cls, config):
        section = config["Browser"] if config.has_section("Browser") else {}
        channel = section.get("channel", "msedge" if os.name == "nt" else "chromium").strip()
        username = section.get("expected_username", "").strip().lstrip("@")
        if channel not in ("msedge", "chrome", "chromium"):
            raise ValueError("Browser.channel must be msedge, chrome or chromium")
        if username and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
            raise ValueError("Browser.expected_username must be an X handle")
        times = {key: int(section.get(key, default)) for key, default in (
            ("timeout_seconds", 45), ("upload_timeout_seconds", 180), ("login_timeout_seconds", 600))}
        if any(value < 1 or value > 3600 for value in times.values()):
            raise ValueError("Browser timeouts must be between 1 and 3600 seconds")
        headless = str(section.get("headless", "false")).lower()
        if headless not in ("true", "false"):
            raise ValueError("Browser.headless must be true or false")
        mode = section.get("mode", "manual" if channel in ("msedge", "chrome") else "managed").strip().lower()
        port = int(section.get("debug_port", "9223"))
        if mode not in ("manual", "managed"):
            raise ValueError("Browser.mode must be manual or managed")
        if mode == "manual" and channel not in ("msedge", "chrome"):
            raise ValueError("Manual browser mode supports Edge or Chrome")
        if not 1024 <= port <= 65535:
            raise ValueError("Browser.debug_port must be between 1024 and 65535")
        if mode == "manual" and headless == "true":
            raise ValueError("Manual browser mode requires headless=false")
        return cls(channel, username, **times, headless=headless == "true", mode=mode, debug_port=port)


def status_id(href, username):
    """Only accept a permalink belonging to the pinned publishing account."""
    parsed = urlsplit(href or "")
    if parsed.netloc and parsed.netloc.lower() not in ("x.com", "www.x.com", "twitter.com", "www.twitter.com"):
        return None
    match = STATUS_PATH.fullmatch(parsed.path)
    return match[2] if match and match[1].lower() == username.lower() else None


def composer_text_matches(actual, expected):
    """An empty contenteditable's placeholder <br> renders as one newline."""
    actual = actual.replace("\r\n", "\n")
    expected = expected.replace("\r\n", "\n")
    if not expected:
        return actual in ("", "\n")
    # Preserve every character for captions, including intentional whitespace.
    return actual == expected


class BrowserPublisher:
    def __init__(self, base_dir, settings, proxy=None):
        self.settings, self.proxy = settings, proxy
        self.state_dir = Path(base_dir) / "state"
        self.profile_dir = self.state_dir / "x-browser"
        if settings.mode == "manual":
            self.profile_dir = manual_profile(base_dir, settings.channel)
        self.account_file = self.state_dir / "x-browser-account.json"
        self.playwright = self.context = self.page = None
        self.username = ""
        self.prepared = False
        self.seen_statuses = set()
        self.attached_browser = None
        self.keep_page = False

    async def start(self, *, allow_login=False):
        self.state_dir.mkdir(exist_ok=True)
        if self.settings.mode == "manual":
            return await self.start_manual(allow_login=allow_login)
        self.playwright = await async_playwright().start()
        options = dict(channel=self.settings.channel,
                       headless=False if allow_login else self.settings.headless,
                       viewport={"width": 1280, "height": 900}, locale="en-US",
                       accept_downloads=False, chromium_sandbox=True)
        if self.proxy:
            if self.proxy.scheme == "socks5" and self.proxy.username:
                raise BrowserAttention("Browser SOCKS5 authentication is unsupported; use HTTP proxy or system TUN.")
            options["proxy"] = {"server": self.proxy.label}
            if self.proxy.username:
                options["proxy"].update(username=self.proxy.username, password=self.proxy.password)
        else:
            options["args"] = ["--no-proxy-server"]
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.profile_dir), **options)
            self.page = await self.select_page()
            self.keep_page = True
            self.page.set_default_timeout(self.settings.timeout_seconds * 1000)
            if not self.is_x_page(self.page):
                await self.page.goto("https://x.com/home", wait_until="domcontentloaded")
            await self.verify_login(allow_login=allow_login)
            log.info("X browser ready: @%s (%s). Publishing uses the website, not the developer API.",
                     self.username, self.settings.channel)
        except PlaywrightError as error:
            if self.page is not None and self.is_disconnected():
                raise BrowserUnavailable('Browser closed during startup; no publish click was made.') from error
            raise BrowserAttention("Cannot open X or verify login. Install Edge/Chrome, close any other MemeBot browser, "
                                   "then run MemeBot.exe --login-x. Complete login/checkpoints in that window.") from error

    async def start_manual(self, *, allow_login=False):
        try:
            await asyncio.to_thread(open_browser, self.state_dir.parent, self.settings)
            endpoint = saved_endpoint(self.state_dir.parent, self.settings)
            self.playwright = await async_playwright().start()
            self.attached_browser = await self.playwright.chromium.connect_over_cdp(
                endpoint, timeout=self.settings.timeout_seconds * 1000, no_defaults=True)
            self.context = self.attached_browser.contexts[0]
            # The endpoint belongs to our dedicated profile. Reuse its startup
            # X/login tab instead of opening a second tab on every connection.
            self.page = await self.select_page()
            self.keep_page = True
            self.page.set_default_timeout(self.settings.timeout_seconds * 1000)
            if not self.is_x_page(self.page):
                await self.page.goto("https://x.com/home", wait_until="domcontentloaded")
            await self.verify_login(allow_login=allow_login)
            log.info("X browser ready: @%s (attached %s). Browser uses its existing network settings.",
                     self.username, self.settings.channel)
            log.info("The dedicated browser closes after the saved queue is sent. "
                     "If closed before posting, it will reopen on the next attempt.")
        except (ValueError, KeyError) as error:
            raise BrowserAttention("Open the dedicated browser using MemeBot.exe --open-browser, "
                                   "log in manually, then run --login-x.") from error
        except PlaywrightError as error:
            if self.is_disconnected():
                raise BrowserUnavailable('Browser disconnected while connecting; the saved task can be retried.') from error
            raise BrowserAttention("Cannot connect to the dedicated browser or verify X login. "
                                   "Complete login in that window, then restart MemeBot or run --login-x.") from error

    @staticmethod
    def is_x_page(page):
        return urlsplit(page.url).hostname in ("x.com", "www.x.com", "twitter.com", "www.twitter.com")

    async def select_page(self):
        pages = [page for page in self.context.pages if not page.is_closed()]
        if not pages:
            # CDP can become ready just before the launcher's first tab appears.
            try:
                pages = [await self.context.wait_for_event("page", timeout=1000)]
            except PlaywrightTimeoutError:
                pass
        for page in pages:
            if self.is_x_page(page):
                return page
        for page in pages:
            if page.url in ("about:blank", "edge://newtab/", "chrome://newtab/"):
                return page
        return await self.context.new_page()

    async def verify_login(self, *, allow_login=False):
        if allow_login:
            log.info("If X asks you to sign in, complete login/checkpoints in the MemeBot browser. "
                     "Waiting up to %ss. No posts are sent during this check; the selected command continues after verification.",
                     self.settings.login_timeout_seconds)
        await self.page.locator(PROFILE_LINK).wait_for(
            state="visible", timeout=(self.settings.login_timeout_seconds if allow_login
                                       else self.settings.timeout_seconds) * 1000)
        await self.check_account(bind=allow_login)
        self.keep_page = False

    def is_disconnected(self):
        return (self.page is None or self.page.is_closed() or
                (self.attached_browser is not None and not self.attached_browser.is_connected()))

    async def ensure_ready(self):
        if not self.is_disconnected():
            return
        log.info('Opening the dedicated browser for the next delivery.')
        await self.close()
        await self.start()

    async def check_account(self, *, bind=False):
        link = self.page.locator(PROFILE_LINK)
        try:
            await link.wait_for(state="visible")
            path = urlsplit(await link.get_attribute("href") or "").path.strip("/")
        except PlaywrightError as error:
            if self.is_disconnected():
                raise BrowserUnavailable('Browser/tab closed before publishing; delivery remains saved.') from error
            raise BrowserAttention("X login expired or a checkpoint appeared. Run MemeBot.exe --login-x.") from error
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", path):
            raise BrowserAttention("Cannot identify the active X account; website layout may have changed.")
        pinned = self.settings.expected_username
        if not pinned and self.account_file.exists():
            pinned = json.loads(self.account_file.read_text(encoding="utf-8"))["username"]
        if pinned and path.lower() != pinned.lower():
            raise BrowserAttention(f"Wrong X account: @{path}; expected @{pinned}. Switch accounts in --login-x.")
        if not pinned and not bind:
            raise BrowserAttention("No publishing account pinned. Run MemeBot.exe --login-x first.")
        self.username = path
        if bind:
            temporary = self.account_file.with_suffix(".tmp")
            temporary.write_text(json.dumps({"username": path}), encoding="utf-8")
            temporary.replace(self.account_file)

    async def prepare(self, text, media_path=None):
        """All failures here precede the publish click and cannot create a post."""
        self.prepared = False
        media_paths = list(media_path) if isinstance(media_path, (list, tuple)) else [media_path] if media_path else []
        if len(media_paths) > 4:
            raise BrowserAttention('One X post supports at most 4 media items; the album needs manual review')
        await self.ensure_ready()
        self.keep_page = True
        stage = 'opening composer'
        save_dialog = self.page.locator(SAVE_POST_DIALOG).filter(has_text=SAVE_POST_TITLE)

        async def keep_current_post(dialog):
            # Dismiss the topmost save sheet back to editing. Its buttons are
            # Save/Discard: an internal "Cancel" test ID can mean Discard.
            # Never select either of those destructive navigation actions.
            await dialog.get_by_role('button').first.press('Escape')

        try:
            await self.page.add_locator_handler(save_dialog, keep_current_post)
            composer = await self.open_composer()
            editor = composer.locator(EDITOR)
            if text:
                stage = 'entering text'
                await editor.fill(text)
                await self.verify_composer_text(editor, text, stage)
                # Move focus out of the observed typeahead. Escape can bubble
                # into the composer and trigger X's "Save post?" confirmation.
                if (await editor.get_attribute('aria-controls') or '').startswith('typeahead'):
                    await editor.press('Tab')
            if media_paths:
                stage = 'uploading media'
                await composer.locator('input[type="file"][data-testid="fileInput"]').set_input_files([str(path) for path in media_paths])
                await composer.locator('[data-testid="attachments"]').first.wait_for(
                    state="visible", timeout=self.settings.upload_timeout_seconds * 1000)
                # Wait for upload completion even if X enables Post prematurely.
                await expect(composer.locator(UPLOAD_PROGRESS)).to_have_count(
                    0, timeout=self.settings.upload_timeout_seconds * 1000)
                stage = 'checking media previews'
                await expect(composer.locator('[data-testid="attachments"] [role="group"]')).to_have_count(
                    len(media_paths), timeout=self.settings.upload_timeout_seconds * 1000)
            stage = 'waiting for Post button'
            button = composer.locator(POST_BUTTON)
            await expect(button).to_be_enabled(timeout=self.settings.upload_timeout_seconds * 1000)
            stage = 'checking Post button is unobstructed'
            await button.click(trial=True, timeout=self.settings.timeout_seconds * 1000)
            stage = 'verifying final post content'
            await self.verify_composer_text(editor, text, stage)
            if media_paths:
                await expect(composer.locator('[data-testid="attachments"] [role="group"]')).to_have_count(
                    len(media_paths), timeout=self.settings.upload_timeout_seconds * 1000)
            await self.check_account()
            self.seen_statuses = await self.visible_statuses()
            self.prepared = True
        except (PlaywrightError, AssertionError) as error:
            if self.is_disconnected():
                raise BrowserUnavailable(f'Browser/tab closed during {stage}; no publish click was made.') from error
            raise BrowserAttention(f"X composer/upload did not become ready ({stage}; {type(error).__name__}). Check the browser for login, "
                                   "media errors or a changed layout; no publish click was made.") from error
        finally:
            with suppress(PlaywrightError):
                await self.page.remove_locator_handler(save_dialog)

    async def assert_no_draft(self, root):
        # Read a single DOM snapshot, rather than retaining positional locators
        # for editors that X may remove while hydrating the restored home tab.
        texts = await root.locator(EDITOR).evaluate_all('editors => editors.map(e => e.innerText)')
        if any(text.strip() for text in texts) or await root.locator('[data-testid="attachments"]').count():
            raise BrowserAttention("X composer contains an existing draft. Clear it manually before retrying.")

    async def open_composer(self):
        """Retry only empty-page startup, before typing or attaching any files."""
        for attempt in range(2):
            stage = 'checking existing drafts before navigation'
            try:
                # Also check before a retry: the user may have started a draft
                # while we were waiting. Never reload their content away.
                await self.assert_no_draft(self.page)
                stage = 'navigating to composer'
                # The actual account/editor below are the readiness checks.
                # Waiting for DOMContentLoaded can time out on unrelated scripts
                # even when the composer has already rendered and is usable.
                await self.page.goto('https://x.com/compose/post', wait_until='commit')
                stage = 'verifying account in composer'
                await self.check_account()
                stage = 'waiting for visible composer editor'
                composer = self.page.locator(COMPOSER).filter(has=self.page.locator(EDITOR))
                await composer.locator(EDITOR).wait_for(state='visible')
                stage = 'checking composer for a restored draft'
                await self.assert_no_draft(composer)
                return composer
            except PlaywrightTimeoutError as error:
                if self.is_disconnected():
                    raise BrowserUnavailable(f'Browser/tab closed during {stage}; no publish click was made.') from error
                if attempt == 0:
                    await self.assert_no_draft(self.page)
                    log.warning('X composer startup timed out (%s); retrying the empty composer once. '
                                'No text or media entered and no publish click made.', stage)
                    continue
                raise BrowserAttention(
                    f'X composer did not become ready ({stage}; TimeoutError; 2 attempts). '
                    'No text or media entered and no publish click was made. Check the browser for login or a changed layout.'
                ) from error

    async def verify_composer_text(self, editor, text, stage):
        actual = await editor.inner_text()
        if not composer_text_matches(actual, text):
            raise BrowserAttention(
                f"X composer text differs from the saved message ({stage}; "
                f"expected {len(text)} characters, read {len(actual)}); review it manually.")

    async def visible_statuses(self):
        links = self.page.locator('a[href*="/status/"]')
        return {value for href in await links.evaluate_all("links => links.map(a => a.getAttribute('href'))")
                if (value := status_id(href, self.username))}

    async def submit(self):
        """Exactly one click. Caller must persist 'sending' before entering here."""
        if not self.prepared:
            raise BrowserAttention("No prepared X post")
        self.prepared = False
        await self.check_account()
        # No network API interception/replay. Confirm only the visible success
        # toast's View link, never a similar old post in the home timeline.
        composer = self.page.locator(COMPOSER).filter(has=self.page.locator(EDITOR))
        await composer.locator(POST_BUTTON).click(timeout=self.settings.timeout_seconds * 1000)
        deadline = asyncio.get_running_loop().time() + self.settings.timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            for href in await self.page.locator(
                    '[data-testid="toast"] a[href*="/status/"], [role="alert"] a[href*="/status/"]'
            ).evaluate_all("links => links.map(a => a.getAttribute('href'))"):
                tweet_id = status_id(href, self.username)
                if tweet_id and tweet_id not in self.seen_statuses:
                    self.keep_page = False
                    return tweet_id
            await asyncio.sleep(0.2)
        raise BrowserAttention("Publish was clicked but no new post permalink was confirmed. Check X before retrying.")

    async def close_idle(self):
        """Called only after durable success and after all saved pending work drains."""
        if self.keep_page or self.prepared or self.context is None:
            return
        if self.settings.mode == "manual" and self.attached_browser is not None:
            browser = self.attached_browser
            if browser.is_connected():
                close_timeout = min(5, self.settings.timeout_seconds)
                session = None
                try:
                    # This connection was checked against the saved dedicated
                    # endpoint at launch. Close that instance, never processes
                    # selected by executable name or the user's daily browser.
                    session = await asyncio.wait_for(browser.new_browser_cdp_session(), close_timeout)
                    await asyncio.wait_for(session.send("Browser.close"), close_timeout)
                except Exception as error:
                    if not self.is_disconnected():
                        if session is not None:
                            with suppress(Exception):
                                await asyncio.wait_for(session.detach(), close_timeout)
                        log.warning("Dedicated browser did not close normally (%s); leaving it open. Sent records are unchanged.",
                                    type(error).__name__)
                        return
        # Release the CDP transport after the close request. In Edge, waiting
        # for browser-level disconnection first can keep the process alive even
        # after its pages close. A closed page/transport also permits cleanup
        # when the request loses its reply during shutdown.
        await self.close()
        log.info("Saved queue sent; dedicated browser closed. It will reopen for the next delivery.")

    async def close(self):
        if self.settings.mode == "manual":
            if self.page and not self.keep_page:
                with suppress(Exception):
                    await self.page.close()
            # Stop only the client transport. Never close the user-owned
            # browser/context; failed composers remain available for review.
        elif self.context:
            with suppress(Exception):
                await self.context.close()
        if self.playwright:
            with suppress(Exception):
                await self.playwright.stop()
        self.playwright = self.context = self.page = self.attached_browser = None
        self.keep_page = self.prepared = False


async def login_x(base_dir, config, proxy=None):
    from delivery_store import instance_lock
    settings = BrowserSettings.from_config(config)
    state = Path(base_dir) / "state"
    state.mkdir(exist_ok=True)
    with instance_lock(state / "forwarder.lock"):
        publisher = BrowserPublisher(base_dir, settings, proxy)
        try:
            await publisher.start(allow_login=True)
        finally:
            await publisher.close()
