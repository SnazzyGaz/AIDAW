"""Drives the browser GUI with a real headless Chrome (skipped if unavailable)."""
import shutil
import threading

import pytest

pw = pytest.importorskip("playwright.sync_api")
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


@pytest.fixture
def gui(small_song):
    if not CHROME:
        pytest.skip("no Chrome/Chromium")
    from plaindaw.rpc import make_server
    srv = make_server(small_song, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        page = b.new_page(viewport={"width": 1500, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
        page.wait_for_selector(".clip")
        yield small_song, page, errors
        b.close()
    srv.shutdown()


def test_gui_edits_go_through_op_layer(gui):
    s, page, errors = gui
    # timeline shows both tracks' clips
    assert page.locator(".row").count() == 2
    # piano roll: select the bass clip and add a note by clicking the grid
    page.locator(".clip", has_text="bass_notes").click()
    page.wait_for_timeout(200)
    n0 = len(s.doc["patterns"]["bass_notes"]["notes"])
    box = page.locator("#rollcanvas").bounding_box()
    page.mouse.click(box["x"] + 44 + 48 * 2 + 5, box["y"] + 30)
    page.wait_for_timeout(600)
    s.reload_if_changed()
    assert len(s.doc["patterns"]["bass_notes"]["notes"]) == n0 + 1
    assert s.history(1)[-1]["op"] == "pattern.add_notes"
    # mixer: mute the drums
    page.locator(".strip", has_text="Drums").locator("button", has_text="M").click()
    page.wait_for_timeout(500)
    assert next(t for t in s.doc["tracks"] if t["id"] == "drums")["mix"].get("mute") is True
    # undo button
    page.click("#undo")
    page.wait_for_timeout(500)
    assert not next(t for t in s.doc["tracks"] if t["id"] == "drums")["mix"].get("mute")
    # history panel lists the edits
    assert page.locator("#history li").count() >= 6
    # an edit made elsewhere (e.g. by the AI) shows up live via SSE
    s.apply("track.add", {"name": "From AI", "type": "audio"}, author="ai")
    page.wait_for_function("document.querySelectorAll('.row').length === 3", timeout=5000)
    # render + analysis
    page.click("#render")
    page.wait_for_function("document.getElementById('analysis').textContent.includes('LUFS')", timeout=60000)
    assert "gui-render.mp3" in page.get_attribute("#player", "src")
    assert errors == []
