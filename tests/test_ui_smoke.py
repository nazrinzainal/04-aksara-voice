"""Browser smoke test with Playwright and the installed desktop browser (no browser download).

    .venv\\Scripts\\python.exe -m pytest -m ui

By default it starts its own server with a temporary data folder and ALL engines (Chatterbox
takes ~1 min to load and ~20-40 s per Malay generation on CPU). To test an already running
studio instead set VOX_URL=http://127.0.0.1:7788 - note that this adds two items to that
studio's history.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from conftest import start_server, wait_for_engines

pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    if os.environ.get("VOX_URL"):
        yield os.environ["VOX_URL"].rstrip("/")
        return
    proc, client = start_server("", tmp_path_factory.mktemp("vox-ui"))
    wait_for_engines(client, timeout=400)
    yield client.base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def page(base_url):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as ex:  # no suitable installed browser
            pytest.skip(f"installed desktop browser not available: {ex}")
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        pg = context.new_page()
        pg.errors = []
        pg.on("console", lambda m: m.type == "error" and pg.errors.append(m.text))
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.goto(base_url + "/")
        pg.wait_for_selector(".voice-card")
        yield pg
        browser.close()


def wait_until(page, expression: str, timeout_ms: int = 10_000):
    """Poll a JS expression via evaluate(). (wait_for_function uses eval, which the app's CSP blocks.)"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.evaluate(expression):
            return
        time.sleep(0.1)
    raise AssertionError("timed out waiting for: " + expression)


def _generate(page, timeout_ms):
    before = page.locator("#history .hist-item:not(.hist-ghost)").count()
    page.click("#generate")
    page.wait_for_selector("#cancel-gen:not([hidden])", timeout=5000)
    assert page.text_content(".gen-label").startswith("Generating")
    page.wait_for_selector("#cancel-gen[hidden]", state="attached", timeout=timeout_ms)
    assert page.is_visible("#player") and not page.is_visible("#player-empty")
    wait_until(page, f"document.querySelectorAll('#history .hist-item:not(.hist-ghost)').length > {before}")


def test_voices_languages_filters(page):
    assert page.evaluate("document.documentElement.lang") == "ms"
    assert page.locator(".voice-card").count() == 4
    assert page.text_content("#sel-name") == "Melayu Perempuan"
    page.click("#filters [data-gender=local]")
    assert page.locator(".voice-card").count() == 2
    page.click("#filters [data-gender=all]")
    page.click("#lang-tabs [data-lang=en]")
    assert page.evaluate("document.documentElement.lang") == "en"
    assert page.locator(".voice-card").count() == 16
    assert page.text_content("#sel-name") == "Heart"
    # 16 voices scroll inside the library; Recent Generations is not pushed down
    assert page.evaluate("(l => l.scrollHeight > l.clientHeight)(document.querySelector('#voices'))")
    page.click("#filters [data-gender=local]")
    assert page.locator(".voice-card").count() == 8
    page.click("#filters [data-gender=male]")
    assert page.text_content("#sel-name") != "Heart"  # selected female voice was switched, not hidden
    assert page.locator(".voice-card.selected").count() == 1
    page.click("#filters [data-gender=all]")
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def test_generate_malay_and_english(page):
    page.click("#lang-tabs [data-lang=ms]")
    page.click('.voice-card[data-id="chatterbox:ms-malaysia-f"]')
    page.fill("#text", "Selamat pagi. Ini ujian ringkas.")
    _generate(page, timeout_ms=240_000)
    assert "Melayu Perempuan" in page.text_content("#player-voice")

    page.click("#lang-tabs [data-lang=en]")
    page.click('.voice-card[data-id="kokoro:af_heart"]')
    page.fill("#text", "Hello, this is a quick smoke test.")
    _generate(page, timeout_ms=60_000)
    assert "Heart" in page.text_content("#player-voice")
    assert page.locator("#history .hist-item:not(.hist-ghost)").count() >= 2
    assert page.get_attribute("#download", "href").startswith("/audio/")


def test_keyboard_and_volume(page):
    page.click("#mute")
    assert page.get_attribute("#mute", "aria-pressed") == "true"
    assert page.evaluate("localStorage.getItem('vox.muted')") == "1"
    page.click("#mute")
    assert page.get_attribute("#mute", "aria-pressed") == "false"
    # Esc stops playback (pause first: the previous generation may still be auto-playing)
    page.evaluate("document.querySelector('#audio').pause()")
    page.click("#play")
    wait_until(page, "!document.querySelector('#audio').paused")
    page.keyboard.press("Escape")
    assert page.evaluate("document.querySelector('#audio').paused")
    # Space on a voice card selects it and must NOT toggle playback
    page.focus('.voice-card[data-id="kokoro:af_bella"]')
    page.keyboard.press(" ")
    assert page.text_content("#sel-name") == "Bella"
    assert page.evaluate("document.querySelector('#audio').paused")
    # Ctrl+Enter generates from the text box (Kokoro: fast)
    page.focus("#text")
    page.keyboard.press("Control+Enter")
    page.wait_for_selector("#cancel-gen:not([hidden])", timeout=5000)
    page.wait_for_selector("#cancel-gen[hidden]", state="attached", timeout=60_000)
    assert "Bella" in page.text_content("#player-voice")


def test_no_console_errors(page):
    assert page.errors == []
