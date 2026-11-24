"""Browser tests for the static UI (auth-state rendering, tabs, chat errors).

They need Playwright and a Chromium/Chrome binary, so they are skipped when
either is missing (the default CI image has neither):

    pip install playwright && playwright install chromium
    pytest tests/ui

All network calls (config.json, Cognito token endpoint, /api/*) are mocked;
nothing leaves the machine.
"""

from __future__ import annotations

import base64
import functools
import http.server
import json
import shutil
import threading
import time
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
COGNITO = "https://auth.example.test"
CONFIG = {"cognitoDomain": COGNITO, "clientId": "client123", "redirectUri": "", "apiBase": "/api", "region": "us-east-1"}
MODELS = {
    "models": [
        {"id": "us.amazon.nova-micro-v1:0", "label": "Amazon Nova Micro", "provider": "Amazon", "default": False},
        {"id": "us.amazon.nova-lite-v1:0", "label": "Amazon Nova Lite", "provider": "Amazon", "default": True},
    ]
}


def _jwt(claims: dict) -> str:
    def enc(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


ID_TOKEN = _jwt({"email": "admin@example.com", "cognito:groups": ["admin"], "exp": int(time.time()) + 3600})


@pytest.fixture(scope="module")
def server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FRONTEND))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:  # noqa: BLE001 - bundled browser not installed
            chrome = shutil.which("google-chrome") or shutil.which("chromium")
            if not chrome:
                pytest.skip("no Chromium available for Playwright")
            b = p.chromium.launch(executable_path=chrome)
        yield b
        b.close()


def _page(browser, server, *, signed_in: bool, chat_events: list[tuple[str, dict]] | None = None):
    ctx = browser.new_context()
    page = ctx.new_page()
    config = {**CONFIG, "redirectUri": f"{server}/"}
    page.route("**/config.json", lambda r: r.fulfill(json=config))
    page.route("**/api/models", lambda r: r.fulfill(json=MODELS))
    page.route("**/api/conversations", lambda r: r.fulfill(json={"conversations": []}))
    page.route("**/api/kb/**", lambda r: r.fulfill(json={"documents": [], "jobs": []}))
    page.route(
        f"{COGNITO}/oauth2/token",
        lambda r: r.fulfill(json={"id_token": ID_TOKEN, "access_token": "a", "refresh_token": "r", "expires_in": 3600}),
    )
    if chat_events is not None:
        body = "".join(f"event: {t}\ndata: {json.dumps(d)}\n\n" for t, d in chat_events)
        page.route("**/api/chat", lambda r: r.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body))
    if signed_in:
        tokens = {"id_token": ID_TOKEN, "access_token": "a", "refresh_token": "r", "expiresAt": int(time.time() * 1000) + 3_600_000}
        page.add_init_script(f"sessionStorage.setItem('agentic.tokens', {json.dumps(json.dumps(tokens))});")
    return ctx, page


def _visible(page, selector: str) -> bool:
    return page.locator(selector).is_visible()


def test_signed_out_shows_only_the_sign_in_card(browser, server):
    ctx, page = _page(browser, server, signed_in=False)
    page.goto(server + "/")
    page.wait_for_selector("#loginBtn", state="visible")
    assert _visible(page, "#signedOut")
    for sel in ("#chatView", "#adminView", "#tabs", "#modelPicker", "#logoutBtn"):
        assert not _visible(page, sel), sel
    ctx.close()


def test_signed_in_hides_sign_in_card_and_admin_panel(browser, server):
    ctx, page = _page(browser, server, signed_in=True)
    page.goto(server + "/")
    page.wait_for_selector("#chatView", state="visible")
    assert not _visible(page, "#signedOut")
    assert not _visible(page, "#adminView"), "Upload documents panel must not show on the Chat tab"
    assert _visible(page, "#modelPicker") and _visible(page, "#logoutBtn") and _visible(page, "#adminTab")
    assert page.text_content("#userEmail") == "admin@example.com"

    page.click("#adminTab")
    page.wait_for_selector("#adminView", state="visible")
    assert not _visible(page, "#chatView") and not _visible(page, "#signedOut") and not _visible(page, "#modelPicker")

    page.click(".tab[data-view=chat]")
    page.wait_for_selector("#chatView", state="visible")
    assert not _visible(page, "#adminView")

    page.reload()  # tokens survive a reload in sessionStorage
    page.wait_for_selector("#chatView", state="visible")
    assert not _visible(page, "#signedOut") and not _visible(page, "#adminView")
    ctx.close()


def test_oauth_redirect_callback_signs_in_and_cleans_url(browser, server):
    ctx, page = _page(browser, server, signed_in=False)
    page.goto(server + "/")
    page.evaluate("sessionStorage.setItem('agentic.pkce', JSON.stringify({verifier: 'v'.repeat(64), state: 'st'}))")
    page.goto(server + "/?code=abc&state=st")
    page.wait_for_selector("#chatView", state="visible")
    assert not _visible(page, "#signedOut")
    assert "code=" not in page.url
    ctx.close()


def test_chat_error_shows_sanitized_message_without_placeholder(browser, server):
    events = [
        ("meta", {"conversationId": "c1", "modelId": "us.amazon.nova-micro-v1:0", "requestId": "r"}),
        ("error", {"message": "The tool configuration is not supported for this model.", "code": "ValidationException"}),
    ]
    ctx, page = _page(browser, server, signed_in=True, chat_events=events)
    page.goto(server + "/")
    page.wait_for_selector("#chatView", state="visible")
    page.fill("#messageInput", "hello")
    page.click("#sendBtn")
    page.wait_for_selector(".msg.error")
    text = page.text_content("#messages")
    assert "The tool configuration is not supported for this model. (ValidationException)" in text
    assert "(no text returned)" not in text
    assert page.locator(".msg.assistant").count() == 0
    ctx.close()


def test_successful_empty_answer_still_gets_placeholder(browser, server):
    events = [
        ("meta", {"conversationId": "c1", "modelId": "m", "requestId": "r"}),
        ("done", {"usage": {"inputTokens": 1, "outputTokens": 0}, "iterations": 1, "toolCalls": 0}),
    ]
    ctx, page = _page(browser, server, signed_in=True, chat_events=events)
    page.goto(server + "/")
    page.wait_for_selector("#chatView", state="visible")
    page.fill("#messageInput", "hello")
    page.click("#sendBtn")
    page.wait_for_selector(".msg.assistant .meta")
    assert page.text_content(".msg.assistant .body") == "(no text returned)"
    ctx.close()
