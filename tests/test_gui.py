"""Drives the browser GUI with a real headless Chrome (skipped if unavailable)."""
import shutil
import threading

import pytest

pw = pytest.importorskip("playwright.sync_api")
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


@pytest.fixture
def gui(small_song, monkeypatch):
    monkeypatch.setenv("PLAINDAW_AUDIO_OUTPUT", "null")
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


def test_gui_ai_panel(gui):
    s, page, errors = gui
    assert page.locator("#aiquick button").count() >= 6
    page.fill("#aicmd", "compose lead --bars 2 --seed 3")
    page.click("#airun")
    page.wait_for_function("document.getElementById('aiout').textContent.includes('compose.lead')", timeout=30000)
    s.reload_if_changed()
    assert s.history(1)[-1]["op"] == "compose.lead"
    page.wait_for_selector("#history li .why")
    page.locator("#aiquick button", has_text="Listen").click()
    page.wait_for_function("document.getElementById('listen').textContent.includes('BPM')", timeout=60000)
    page.fill("#aicmd", "nonsense words")
    page.click("#airun")
    page.wait_for_selector("#aiout.err")
    assert not errors


def test_gui_live_transport(gui, monkeypatch):
    s, page, errors = gui
    from plaindaw import live
    page.wait_for_function("document.getElementById('t-info').textContent.length > 0", timeout=30000)
    x0 = page.evaluate("document.getElementById('playhead').getBoundingClientRect().x")
    page.click("#t-play")
    page.wait_for_timeout(1200)
    e = live.engine_for(s, create=False)
    assert e is not None and e.playing and e.output_kind in ("null", "portaudio")
    x1 = page.evaluate("document.getElementById('playhead').getBoundingClientRect().x")
    assert x1 > x0                                    # the playhead moves
    assert page.text_content("#t-pos").startswith("1.") or page.text_content("#t-pos").startswith("2.")
    page.click("#t-stop")
    page.wait_for_timeout(300)
    assert not e.playing
    # piano-roll keys preview notes on the clip's track
    page.locator(".clip", has_text="bass_notes").click()
    page.wait_for_timeout(200)
    box = page.locator("#rollcanvas").bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + 60)
    page.mouse.down()
    page.wait_for_timeout(250)
    assert any(t == "bass" for (t, k) in e.active)
    page.mouse.up()
    page.wait_for_timeout(250)
    assert not e.active
    assert errors == []
    e.close()
