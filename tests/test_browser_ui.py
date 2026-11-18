import _local_dir  # noqa: F401  (isolates local data; must stay first)
"""Real local browser + simulated X UI. Opt in with MEMEBOT_BROWSER_TESTS=1.

Every request is intercepted; these tests cannot contact X or publish a post.
"""

import asyncio
import os
from pathlib import Path
import tempfile
import socket
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from playwright.async_api import async_playwright

from browser_publisher import BrowserPublisher, BrowserSettings, BrowserAttention, PostLimitReached, composer_text_matches
from manual_browser import browser_executable, current_endpoint, open_browser, saved_endpoint


FIXTURE = """<!doctype html><html><body>
<a data-testid="AppTabBar_Profile_Link" href="/example_user">Profile</a>
<div role="dialog" aria-modal="true">
<div contenteditable="true" role="textbox" data-testid="tweetTextarea_0"></div>
<input type="file" data-testid="fileInput" multiple>
<button data-testid="tweetButton">Post</button>
</div>
<div data-testid="toast"></div>
<a href="/example_user/status/100">Old post</a>
<script>
window.clicks=0;
document.querySelector('input').onchange=()=>{
  const attachments=document.createElement('div');
  attachments.dataset.testid='attachments';
  attachments.textContent='Media preview';
  for (const file of document.querySelector('input').files) {
    const preview=document.createElement('div');
    preview.setAttribute('role','group'); preview.textContent=file.name;
    attachments.append(preview);
  }
  document.querySelector('[role=dialog]').append(attachments);
};
document.querySelector('button').onclick=()=>{
  window.clicks++;
  const toast=document.querySelector('[data-testid="toast"]');
  toast.innerHTML='<a href="/example_user/status/123">View</a>';
};
</script></body></html>"""


class ComposerTextTests(unittest.TestCase):
    def test_empty_editor_placeholder(self):
        for actual in ('', '\n', '\r\n'):
            with self.subTest(actual=actual):
                self.assertTrue(composer_text_matches(actual, ''))

    def test_unexpected_content_is_rejected_for_media_only_post(self):
        for actual in ('unexpected text', ' ', '\u200b', '\n\n'):
            with self.subTest(actual=actual):
                self.assertFalse(composer_text_matches(actual, ''))

    def test_captions_preserve_whitespace_and_unicode(self):
        self.assertTrue(composer_text_matches('中文 🐈\r\nsecond line', '中文 🐈\nsecond line'))
        for actual, expected in [('caption\n', 'caption'), ('caption', 'caption '),
                                 ('cat', 'cat #猫猫'), ('', '\n')]:
            with self.subTest(actual=actual, expected=expected):
                self.assertFalse(composer_text_matches(actual, expected))


@unittest.skipUnless(os.environ.get("MEMEBOT_BROWSER_TESTS") == "1", "Set MEMEBOT_BROWSER_TESTS=1 for local browser integration tests")
class BrowserUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch(
            channel="msedge" if os.name == "nt" else "chromium", headless=True, chromium_sandbox=True)
        # A real context lets the publisher open a replacement tab.
        self.page = await (await self.browser.new_context()).new_page()
        self.page.set_default_timeout(1000)
        self.html = FIXTURE
        await self.page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=self.html))
        self.publisher = BrowserPublisher(self.directory.name, BrowserSettings(
            expected_username="example_user", timeout_seconds=1, upload_timeout_seconds=1))
        self.publisher.page = self.page

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    async def test_unicode_text_and_local_media_publish_once(self):
        path = Path(self.directory.name) / "test.jpg"
        path.write_bytes(b"local fixture")
        await self.publisher.prepare("中文 🐈\nsecond line", path)
        self.assertEqual(await self.page.locator('input').evaluate("input => input.files[0].name"), "test.jpg")
        self.assertEqual(await self.page.evaluate("window.clicks"), 0)
        self.assertEqual(await self.publisher.submit(), "123")
        self.assertEqual(await self.page.evaluate("window.clicks"), 1)
        with self.assertRaises(BrowserAttention):
            await self.publisher.submit()
        self.assertEqual(await self.page.evaluate("window.clicks"), 1)

    async def test_existing_draft_is_not_overwritten(self):
        self.html = FIXTURE.replace('data-testid="tweetTextarea_0"></div>', 'data-testid="tweetTextarea_0">existing draft</div>')
        with self.assertRaisesRegex(BrowserAttention, "existing draft"):
            await self.publisher.prepare("new message")
        self.assertEqual(await self.page.evaluate("window.clicks"), 0)

    async def test_reply_opens_saved_parent_instead_of_another_post(self):
        self.publisher.username = 'example_user'
        self.html = FIXTURE.replace('</body>', '''
        <article data-testid="tweet"><a href="/example_user/status/900"><time>now</time></a>
        <button data-testid="reply" onclick="window.parentClicked='900'; document.querySelector('[role=dialog]').style.display='block'">Reply</button></article>
        <article data-testid="tweet"><a href="/example_user/status/901"><time>now</time></a>
        <button data-testid="reply" onclick="window.parentClicked='901'">Reply</button></article>
        <script>document.querySelector('[role=dialog]').style.display='none'</script></body>''')
        await self.publisher.prepare('', reply_to='900')
        self.assertEqual(self.page.url, 'https://x.com/example_user/status/900')
        self.assertEqual(await self.page.evaluate('window.parentClicked'), '900')
        self.assertEqual(await self.publisher.submit(), '123')

    async def test_missing_reply_target_never_falls_back_to_standalone_post(self):
        self.publisher.username = 'example_user'
        with self.assertRaises(BrowserAttention):
            await self.publisher.prepare('reply', reply_to='900')
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)

    async def test_media_only_post_with_empty_editor_placeholder(self):
        self.html = FIXTURE.replace('data-testid="tweetTextarea_0"></div>',
                                   'data-testid="tweetTextarea_0"><div><br></div></div>')
        path = Path(self.directory.name) / 'photo.jpg'
        path.write_bytes(b'local fixture')
        await self.publisher.prepare('', path)
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(), '\n')
        self.assertTrue(self.publisher.prepared)
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)
        self.assertEqual(await self.publisher.submit(), '123')
        self.assertEqual(await self.page.evaluate('window.clicks'), 1)

    async def test_visible_composer_without_aria_modal_is_used(self):
        self.html = FIXTURE.replace(' aria-modal="true"', '')
        path = Path(self.directory.name) / 'photo.jpg'
        path.write_bytes(b'local fixture')
        await self.publisher.prepare('', path)
        self.assertTrue(self.publisher.prepared)
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)
        self.assertEqual(await self.publisher.submit(), '123')

    async def test_ready_composer_does_not_wait_for_unrelated_deferred_script(self):
        release = asyncio.Event()
        async def slow_script(route):
            await release.wait()
            await route.fulfill(status=200, content_type='text/javascript', body='')
        self.html = FIXTURE.replace('<html>', '<html><head><script defer src="/slow.js"></script></head>')
        await self.page.route('**/slow.js', slow_script)
        try:
            await asyncio.wait_for(self.publisher.prepare('caption'), 3)
            self.assertEqual(await self.page.evaluate('document.readyState'), 'interactive')
            self.assertTrue(self.publisher.prepared)
            self.assertEqual(await self.page.evaluate('window.clicks'), 0)
        finally:
            release.set()
            await self.page.wait_for_load_state('domcontentloaded')

    async def test_empty_composer_startup_timeout_retries_once(self):
        navigations = []
        async def composer_route(route):
            navigations.append(route.request.url)
            body = FIXTURE if len(navigations) == 2 else FIXTURE.replace('tweetTextarea_0', 'not-ready')
            await route.fulfill(status=200, content_type='text/html', body=body)
        await self.page.route('**/compose/post', composer_route)
        path = Path(self.directory.name) / 'photo.jpg'
        path.write_bytes(b'local fixture')
        await self.publisher.prepare('', path)
        self.assertEqual(len(navigations), 2)
        self.assertTrue(self.publisher.prepared)
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)
        self.assertEqual(await self.publisher.submit(), '123')
        self.assertEqual(await self.page.evaluate('window.clicks'), 1)

    async def test_persistent_missing_composer_stops_after_two_attempts(self):
        self.html = FIXTURE.replace('tweetTextarea_0', 'not-ready')
        with patch.object(self.page, 'goto', wraps=self.page.goto) as navigate:
            with self.assertRaisesRegex(BrowserAttention, 'waiting for visible composer editor; TimeoutError; 2 attempts'):
                await self.publisher.prepare('caption')
            self.assertEqual(navigate.call_count, 2)
        self.assertFalse(self.publisher.prepared)
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)

    async def test_draft_entered_during_startup_timeout_prevents_reload(self):
        self.html = FIXTURE.replace('role="dialog"', 'role="region"').replace('</script>', '''
          setTimeout(()=>document.querySelector('[contenteditable]').textContent='manual draft', 100);
        </script>''')
        with patch.object(self.page, 'goto', wraps=self.page.goto) as navigate:
            with self.assertRaisesRegex(BrowserAttention, 'existing draft'):
                await self.publisher.prepare('queued caption')
            self.assertEqual(navigate.call_count, 1)
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(), 'manual draft')
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)

    async def test_unexpected_text_after_media_upload_blocks_publish(self):
        self.html = FIXTURE.replace("document.querySelector('[role=dialog]').append(attachments);", """
          document.querySelector('[role=dialog]').append(attachments);
          document.querySelector('[contenteditable]').textContent='unexpected text';
        """)
        path = Path(self.directory.name) / 'photo.jpg'
        path.write_bytes(b'local fixture')
        with self.assertRaisesRegex(BrowserAttention, 'verifying final post content; expected 0 characters'):
            await self.publisher.prepare('', path)
        self.assertFalse(self.publisher.prepared)
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)

    async def test_reused_tab_draft_is_preserved_before_navigation(self):
        await self.page.goto('https://x.com/home')
        await self.page.locator('[contenteditable]').fill('my unsent draft')
        with self.assertRaisesRegex(BrowserAttention, 'existing draft'):
            await self.publisher.prepare('queued message')
        self.assertEqual(self.page.url, 'https://x.com/home')
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(), 'my unsent draft')
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)

    async def test_two_images_attached_to_one_post_in_order(self):
        paths = [Path(self.directory.name) / name for name in ('first.jpg', 'second.jpg')]
        for path in paths:
            path.write_bytes(b'local fixture')
        await self.publisher.prepare('album caption', paths)
        self.assertEqual(await self.page.locator('input').evaluate('e => [...e.files].map(f=>f.name)'),
                         ['first.jpg', 'second.jpg'])
        self.assertEqual(await self.publisher.submit(), '123')
        self.assertEqual(await self.page.evaluate('window.clicks'), 1)

    async def test_oversized_album_is_rejected_before_navigation(self):
        with self.assertRaisesRegex(BrowserAttention, 'at most 4'):
            await self.publisher.prepare('album', ['image.jpg'] * 5)
        self.assertEqual(self.page.url, 'about:blank')

    async def test_missing_second_preview_blocks_post(self):
        self.html = FIXTURE.replace("document.querySelector('input').files)",
                                    "[...document.querySelector('input').files].slice(0, 1))")
        paths = [Path(self.directory.name) / name for name in ('one.jpg', 'two.jpg')]
        for path in paths:
            path.write_bytes(b'local fixture')
        with self.assertRaisesRegex(BrowserAttention, 'did not become ready'):
            await self.publisher.prepare('album', paths)
        self.assertFalse(self.publisher.prepared)
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)

    async def test_background_composer_is_never_used(self):
        self.html = FIXTURE.replace('</body>', '''
          <div contenteditable="true" data-testid="tweetTextarea_0">background draft</div>
          <input type="file" data-testid="fileInput">
          <button data-testid="tweetButtonInline" onclick="window.wrongClick=true">Wrong Post</button>
        </body>''')
        await self.publisher.prepare("correct dialog")
        self.assertEqual(await self.page.locator('[contenteditable]').last.inner_text(), "background draft")
        self.assertEqual(await self.publisher.submit(), "123")
        self.assertFalse(await self.page.evaluate("!!window.wrongClick"))

    async def test_character_counter_does_not_block_media_upload(self):
        self.html = FIXTURE.replace('<button data-testid="tweetButton">', '''
          <div data-testid="countdown-circle"><div role="progressbar">Character count</div></div>
          <div role="progressbar" aria-valuenow="0"><div data-testid="progressBar-bar" style="width: 0%"></div></div>
          <button data-testid="tweetButton">''')
        path = Path(self.directory.name) / "photo.jpg"
        path.write_bytes(b"local fixture")
        await self.publisher.prepare("hello", path)
        self.assertTrue(self.publisher.prepared)
        self.assertEqual(await self.publisher.submit(), "123")

    async def test_video_upload_must_finish_before_prepare_returns(self):
        self.html = FIXTURE.replace("document.querySelector('[role=dialog]').append(attachments);", """
          document.querySelector('[role=dialog]').append(attachments);
          const progress = document.createElement('div');
          progress.setAttribute('role','progressbar'); progress.textContent='Uploading';
          document.querySelector('[role=dialog]').append(progress);
        """)
        path = Path(self.directory.name) / "video.mp4"
        path.write_bytes(b"local video fixture")
        with self.assertRaisesRegex(BrowserAttention, "did not become ready"):
            await self.publisher.prepare("video", path)
        self.assertFalse(self.publisher.prepared)
        self.assertEqual(await self.page.evaluate("window.clicks"), 0)

    async def test_wrong_logged_in_account_blocks_before_typing(self):
        self.html = FIXTURE.replace('href="/example_user"', 'href="/other"')
        with self.assertRaisesRegex(BrowserAttention, "Wrong X account"):
            await self.publisher.prepare("new message")
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(), "")

    async def test_missing_login_blocks_before_typing(self):
        self.html = FIXTURE.replace('AppTabBar_Profile_Link', 'missing')
        with self.assertRaisesRegex(BrowserAttention, "login expired"):
            await self.publisher.prepare("new message")

    async def test_startup_waits_for_manual_login_then_verifies_without_posting(self):
        self.publisher.state_dir.mkdir()
        self.publisher.keep_page = True
        self.html = FIXTURE.replace('data-testid="AppTabBar_Profile_Link"',
                                    'hidden data-testid="AppTabBar_Profile_Link"')
        await self.page.goto('https://x.com/home')
        task = asyncio.create_task(self.publisher.verify_login(allow_login=True))
        try:
            await asyncio.sleep(0.1)
            self.assertFalse(task.done())
            self.assertEqual(await self.page.evaluate('window.clicks'), 0)
            # Simulate the account link appearing after the user signs in.
            await self.page.locator('[data-testid="AppTabBar_Profile_Link"]').evaluate('e => e.hidden=false')
            await asyncio.wait_for(task, 3)
            self.assertEqual(self.publisher.username, 'example_user')
            self.assertTrue(self.publisher.account_file.exists())
            self.assertFalse(self.publisher.keep_page)
            self.assertEqual(await self.page.evaluate('window.clicks'), 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_no_toast_permalink_never_uses_timeline_post(self):
        await self.publisher.prepare("hello")
        await self.page.evaluate("() => { document.querySelector('button').onclick=()=>{window.clicks++}; }")
        self.assertEqual(await self.page.evaluate("window.clicks"), 0)
        with self.assertRaisesRegex(BrowserAttention, "no new post permalink"):
            await self.publisher.submit()
        self.assertEqual(await self.page.evaluate("window.clicks"), 1)

    async def test_old_toast_cannot_confirm_new_post(self):
        self.html = FIXTURE.replace('<div data-testid="toast"></div>', '<div data-testid="toast"><a href="/example_user/status/123">View</a></div>')
        await self.publisher.prepare("hello")
        with self.assertRaises(BrowserAttention):
            await self.publisher.submit()

    async def test_disabled_publish_button_never_clicked(self):
        self.html = FIXTURE.replace('<button data-testid=', '<button disabled data-testid=')
        with self.assertRaisesRegex(BrowserAttention, "did not become ready"):
            await self.publisher.prepare("hello")
        self.assertEqual(await self.page.evaluate("window.clicks"), 0)

    async def test_hashtag_autocomplete_is_dismissed_without_changing_caption(self):
        self.html = FIXTURE.replace('</script>', '''
          const editor=document.querySelector('[contenteditable]');
          editor.oninput=()=>{
            editor.setAttribute('aria-controls','typeaheadDropdownWrapped-1');
            const cover=document.createElement('div'); cover.id='typeaheadDropdownWrapped-1';
            cover.setAttribute('role','listbox');
            cover.style='position:fixed;inset:0;z-index:9999';
            document.body.append(cover);
          };
          document.addEventListener('keydown', e=>{if(e.key==='Escape') window.composerClosed=true;});
          editor.onblur=()=>document.querySelector('#typeaheadDropdownWrapped-1')?.remove();
        </script>''')
        await self.publisher.prepare('cat #猫猫')
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(), 'cat #猫猫')
        self.assertEqual(await self.page.locator('[role="listbox"]').count(), 0)
        self.assertFalse(await self.page.evaluate('!!window.composerClosed'))
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)
        self.assertEqual(await self.publisher.submit(), '123')

    async def test_late_hashtag_autocomplete_is_dismissed_before_post_check(self):
        # Observed on live X: the list opened only while the video uploaded,
        # after the post-typing check; the first Tab merely moved focus; and
        # aria-controls stayed set after the list closed.
        self.html = FIXTURE.replace('</script>', '''
          const editor=document.querySelector('[contenteditable]');
          window.tabs=0;
          document.querySelector('input[type="file"]').addEventListener('change', ()=>{
            if(document.querySelector('#typeaheadDropdownWrapped-3')) return;
            editor.setAttribute('aria-controls','typeaheadDropdownWrapped-3');
            const cover=document.createElement('div'); cover.id='typeaheadDropdownWrapped-3';
            cover.setAttribute('role','listbox');
            cover.style='position:fixed;inset:0;z-index:9999';
            document.body.append(cover);
          });
          document.addEventListener('keydown', e=>{
            if(e.key==='Tab' && ++window.tabs >= 2) document.querySelector('#typeaheadDropdownWrapped-3')?.remove();
          });
        </script>''')
        path = Path(self.directory.name) / 'clip.mp4'
        path.write_bytes(b'local fixture')
        await self.publisher.prepare('喵：找我呢？\n#猫猫', path)
        self.assertEqual(await self.page.locator('[role="listbox"]').count(), 0)
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(), '喵：找我呢？\n#猫猫')
        self.assertTrue(self.publisher.prepared)
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)
        self.assertEqual(await self.publisher.submit(), '123')

    async def test_daily_limit_after_click_is_unpublished_and_tab_replaced(self):
        # Observed on live X: the composer stays open with the post and a banner.
        self.html = FIXTURE.replace("toast.innerHTML='<a href=\"/example_user/status/123\">View</a>';", '''
          const banner=document.createElement('div');
          banner.textContent="You’ve hit the daily post limit. Subscribe to Plus for higher limits.";
          document.querySelector('[role=dialog]').prepend(banner);''')
        await self.page.context.route('**/*', lambda route: route.fulfill(
            status=200, content_type='text/html', body='<html><body>home</body></html>'))
        old = self.page
        await self.publisher.prepare('caption')
        with self.assertRaises(PostLimitReached):
            await self.publisher.submit()
        self.assertTrue(old.is_closed())
        self.assertIsNot(self.publisher.page, old)
        self.assertFalse(self.publisher.page.is_closed())
        self.assertFalse(self.publisher.keep_page)

    async def test_short_window_keeps_account_check_working(self):
        # Observed on live X after the monitor slept: a 1015x468 window moves
        # the profile link into "More", which looked like an expired login.
        self.html = FIXTURE.replace('<div role="dialog"', '''<style>
          @media (max-height: 600px) { [data-testid="AppTabBar_Profile_Link"] { display: none } }
        </style><div role="dialog"''')
        await self.page.set_viewport_size({'width': 1015, 'height': 468})
        await self.publisher.prepare('caption')
        self.assertEqual(await self.page.evaluate('[innerWidth, innerHeight]'), [1280, 900])
        self.assertEqual(await self.publisher.submit(), '123')

    async def test_daily_limit_banner_stops_before_typing(self):
        self.html = FIXTURE.replace('<div role="dialog" aria-modal="true">',
                                    '<div role="dialog" aria-modal="true"><div>You have hit the daily post limit.</div>')
        with self.assertRaises(PostLimitReached):
            await self.publisher.prepare('caption')
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(), '')
        self.assertFalse(self.publisher.prepared)
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)

    async def test_unknown_overlay_blocks_before_prepared_and_never_clicks(self):
        self.html = FIXTURE.replace('</body>', '<div style="position:fixed;inset:0;z-index:9999"></div></body>')
        with self.assertRaisesRegex(BrowserAttention, 'unobstructed'):
            await self.publisher.prepare('caption')
        self.assertFalse(self.publisher.prepared)
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)

    async def test_save_prompt_returns_to_composer_without_saving_or_discarding_post(self):
        self.html = FIXTURE.replace('</script>', '''
          const editor=document.querySelector('[contenteditable]');
          editor.oninput=()=>{
            editor.setAttribute('aria-controls','typeaheadDropdownWrapped-1');
            if(document.querySelector('#typeaheadDropdownWrapped-1')) return;
            const list=document.createElement('div'); list.id='typeaheadDropdownWrapped-1';
            list.setAttribute('role','listbox'); list.textContent='#猫猫';
            document.body.append(list);
          };
          document.addEventListener('keydown', e=>{
            if(e.key !== 'Tab') return;
            document.querySelector('#typeaheadDropdownWrapped-1')?.remove();
            const prompt=document.createElement('div');
            prompt.tabIndex=-1;
            prompt.dataset.testid='confirmationSheetDialog';
            prompt.style='position:fixed;inset:0;z-index:9999;background:white';
            prompt.innerHTML='<h2>Save post?</h2><p>You can save this to send later from your drafts.&#x20;</p>' +
              '<button data-testid="confirmationSheetConfirm">Save</button>' +
              '<button data-testid="confirmationSheetCancel">Discard</button>';
            prompt.querySelector('[data-testid="confirmationSheetCancel"]').onclick=()=>{
              window.discarded=true; editor.textContent=''; prompt.remove();
            };
            prompt.querySelector('[data-testid="confirmationSheetConfirm"]').onclick=()=>{
              window.saved=true; editor.textContent=''; prompt.remove();
            };
            prompt.onkeydown=e=>{if(e.key==='Escape'){
              window.dismissed=true; prompt.remove(); e.stopPropagation();
            }};
            document.body.append(prompt);
          });
        </script>''')
        path = Path(self.directory.name) / 'photo.jpg'
        path.write_bytes(b'local fixture')
        await self.publisher.prepare('cat #猫猫', path)
        self.assertTrue(await self.page.evaluate('window.dismissed'))
        self.assertFalse(await self.page.evaluate('!!window.discarded'))
        self.assertFalse(await self.page.evaluate('!!window.saved'))
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(), 'cat #猫猫')
        self.assertEqual(await self.page.locator('input').evaluate('e => e.files.length'), 1)
        self.assertTrue(self.publisher.prepared)
        self.assertEqual(await self.publisher.submit(), '123')
        self.assertEqual(await self.page.evaluate('window.clicks'), 1)

    async def test_other_confirmation_is_not_automatically_cancelled(self):
        self.html = FIXTURE.replace('</body>', '''
          <div data-testid="confirmationSheetDialog" style="position:fixed;inset:0;z-index:9999">
            <h2>Delete post?</h2>
            <button data-testid="confirmationSheetCancel" onclick="window.cancelled=true;this.parentNode.remove()">Cancel</button>
          </div></body>''')
        with self.assertRaises(BrowserAttention):
            await self.publisher.prepare('caption')
        self.assertFalse(await self.page.evaluate('!!window.cancelled'))
        self.assertEqual(await self.page.evaluate('window.clicks'), 0)


@unittest.skipUnless(os.name == "nt" and os.environ.get("MEMEBOT_BROWSER_TESTS") == "1",
                     "Opt-in local Edge attach test")
class ManualAttachUITests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_close_and_reopen_preserve_profile_and_other_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            settings = BrowserSettings(debug_port=port, timeout_seconds=5)
            children = []
            def launch_fixture(args, **kwargs):
                # Only replace the dedicated test browser's startup URL. Node
                # and the separate browser instance use the real subprocess module.
                self.assertEqual(args[-1], 'https://x.com/home')
                child = subprocess.Popen([*args[:-1], '--headless=new', '--no-first-run', 'about:blank'], **kwargs)
                children.append(child)
                return child
            launcher = SimpleNamespace(Popen=launch_fixture, DEVNULL=subprocess.DEVNULL)
            other_runtime = await async_playwright().start()
            other = publisher = None
            try:
                other = await other_runtime.chromium.launch(channel='msedge', headless=True, chromium_sandbox=True)
                other_page = await other.new_page()
                await other_page.goto('data:text/html,<title>Unrelated browser</title>')
                endpoints = []
                with patch('manual_browser.subprocess', launcher):
                    for cycle in range(2):
                        await asyncio.to_thread(open_browser, directory, settings)
                        endpoint = saved_endpoint(settings)
                        endpoints.append(endpoint)
                        publisher = BrowserPublisher(directory, settings)
                        publisher.playwright = await async_playwright().start()
                        publisher.attached_browser = await publisher.playwright.chromium.connect_over_cdp(endpoint, no_defaults=True)
                        publisher.context = publisher.attached_browser.contexts[0]
                        await publisher.context.route('**/*', lambda route: route.fulfill(
                            status=200, content_type='text/html', body=FIXTURE))
                        publisher.page = await publisher.select_page()
                        self.assertEqual(len(publisher.context.pages), 1,
                                         f'cycle={cycle}, pages={[page.url for page in publisher.context.pages]}')
                        await publisher.page.goto('http://memebot.test')
                        if cycle == 0:
                            await publisher.page.evaluate("localStorage.setItem('fixture', 'preserved')")
                        else:
                            self.assertEqual(await publisher.page.evaluate("localStorage.getItem('fixture')"), 'preserved')
                        await publisher.close_idle()
                        self.assertTrue(publisher.is_disconnected())
                        self.assertIsNone(publisher.playwright, f'Browser connected: {publisher.attached_browser.is_connected() if publisher.attached_browser else None}; process: {children[-1].poll()}')
                        self.assertEqual(await asyncio.to_thread(children[-1].wait, 10), 0)
                        self.assertTrue(other.is_connected())
                        self.assertEqual(await other_page.title(), 'Unrelated browser')
                self.assertNotEqual(endpoints[0], endpoints[1])
            finally:
                if publisher:
                    await publisher.close()
                if other:
                    await other.close()
                await other_runtime.stop()
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    await asyncio.to_thread(child.wait, 10)

    async def test_disconnect_preserves_ordinary_browser_and_its_tab(self):
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            child = subprocess.Popen([
                str(browser_executable("msedge")), f"--user-data-dir={directory}",
                f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                "--headless=new", "--no-first-run", "--no-proxy-server", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            runtime = browser = publisher = None
            try:
                endpoint = None
                for _ in range(30):
                    try:
                        endpoint = await asyncio.to_thread(current_endpoint, port)
                        break
                    except Exception:
                        await asyncio.sleep(0.2)
                self.assertIsNotNone(endpoint)
                runtime = await async_playwright().start()
                browser = await runtime.chromium.connect_over_cdp(endpoint, no_defaults=True)
                context = browser.contexts[0]
                original = await context.new_page()
                await original.goto("data:text/html,<title>Keep this tab</title>")
                publisher = BrowserPublisher(directory, BrowserSettings())
                publisher.playwright = runtime
                publisher.context = context
                publisher.attached_browser = browser
                publisher.page = await context.new_page()
                await publisher.close()
                runtime = await async_playwright().start()
                browser = await runtime.chromium.connect_over_cdp(endpoint, no_defaults=True)
                titles = [await page.title() for page in browser.contexts[0].pages]
                self.assertIn("Keep this tab", titles)
            finally:
                if browser and browser.is_connected():
                    await browser.close()
                if runtime:
                    await runtime.stop()
                if child.poll() is None:
                    child.terminate()
                await asyncio.to_thread(child.wait, 10)


if __name__ == "__main__":
    unittest.main()
