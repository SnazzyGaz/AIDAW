"""JSON-RPC 2.0 front end.

stdio: one JSON request per line in, one JSON response per line out.
HTTP : POST /rpc (single or batch), GET /events (Server-Sent Events: live change feed),
       GET / (browser GUI), GET /renders/<file> (rendered audio).
Method names are op names (track.add, mix.set, ...) and queries (project.summary,
render, analyze, history.undo, ...). Pass "_as": "ai"|"human" in params to tag the author.
"""
from __future__ import annotations

import json
import mimetypes
import queue
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .model import ModelError
from .ops import OpError
from .session import METHODS_DOC, NeedsConfirm, PermissionDenied, Session

GUI_DIR = Path(__file__).parent / "gui"


def _error(id_, code, msg, data=None):
    e = {"code": code, "message": msg}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": e}


def handle(session: Session, req) -> dict | None:
    if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
        return _error(req.get("id") if isinstance(req, dict) else None, -32600, "invalid request")
    id_ = req.get("id")
    method = req["method"]
    params = req.get("params") or {}
    if not isinstance(params, dict):
        return _error(id_, -32602, "params must be an object")
    author = params.pop("_as", None)
    try:
        if method == "rpc.discover":
            from .ops import op_catalog
            res = {"ops": op_catalog(), "queries": METHODS_DOC}
        else:
            res = session.call(method, params, author=author)
    except PermissionDenied as e:
        return None if id_ is None else _error(id_, -32001, str(e))
    except NeedsConfirm as e:
        return None if id_ is None else _error(id_, -32002, str(e))
    except (OpError, ModelError, ValueError, KeyError, FileNotFoundError) as e:
        msg = str(e)
        code = -32601 if msg.startswith("unknown method") else -32602
        return None if id_ is None else _error(id_, code, msg if not isinstance(e, KeyError) else f"missing {e}")
    except Exception as e:  # noqa: BLE001
        return None if id_ is None else _error(id_, -32603, f"internal error: {e}", traceback.format_exc()[-1500:])
    if id_ is None:
        return None
    return {"jsonrpc": "2.0", "id": id_, "result": res}


def handle_raw(session: Session, text: str) -> str | None:
    try:
        req = json.loads(text)
    except ValueError:
        return json.dumps(_error(None, -32700, "parse error"))
    if isinstance(req, list):
        if not req:
            return json.dumps(_error(None, -32600, "empty batch"))
        out = [r for r in (handle(session, x) for x in req) if r is not None]
        return json.dumps(out, ensure_ascii=False) if out else None
    r = handle(session, req)
    return json.dumps(r, ensure_ascii=False) if r is not None else None


def serve_stdio(session: Session, inp=None, out=None) -> int:
    inp = inp or sys.stdin
    out = out or sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        r = handle_raw(session, line)
        if r is not None:
            out.write(r + "\n")
            out.flush()
    return 0


class _Hub:
    def __init__(self):
        self.qs: list[queue.Queue] = []
        self.lock = threading.Lock()

    def publish(self, ev):
        with self.lock:
            for q in self.qs:
                q.put(ev)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.qs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.qs:
                self.qs.remove(q)


def make_server(session: Session, host="127.0.0.1", port=8765) -> ThreadingHTTPServer:
    hub = _Hub()
    session.listeners.append(hub.publish)

    def watcher():  # pick up edits from other processes (CLI, MCP, text editor)
        import time
        while True:
            time.sleep(0.5)
            try:
                session.reload_if_changed()
            except Exception:
                pass

    threading.Thread(target=watcher, daemon=True).start()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/rpc":
                return self._send(404, b'{"error":"not found"}')
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n > 50_000_000:
                return self._send(413, b'{"error":"too large"}')
            body = self.rfile.read(n).decode("utf-8", errors="replace")
            r = handle_raw(session, body)
            self._send(200, (r or "").encode("utf-8"))

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q = hub.subscribe()
                try:
                    self.wfile.write(f"data: {json.dumps({'type': 'hello', 'version': session.version})}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            ev = q.get(timeout=15)
                            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    hub.unsubscribe(q)
                return
            if path.startswith("/renders/"):
                f = (session.dir / "renders" / path[len("/renders/"):]).resolve()
                if not str(f).startswith(str((session.dir / "renders").resolve())) or not f.is_file():
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/":
                path = "/index.html"
            f = (GUI_DIR / path.lstrip("/")).resolve()
            if not str(f).startswith(str(GUI_DIR.resolve())) or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            self._send(200, f.read_bytes(), (mimetypes.guess_type(f.name)[0] or "text/plain") + "; charset=utf-8")

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    return srv


def serve_http(session: Session, host="127.0.0.1", port=8765, open_browser=False) -> int:
    from . import live
    srv = make_server(session, host, port)
    url = f"http://{host}:{srv.server_address[1]}/"
    live.write_server_file(session, host, srv.server_address[1])
    eng = live.engine_for(session)          # starts rendering the timeline now, so Play is instant
    print(f"AIDAW serving {session.dir} at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        live.remove_server_file(session)
        eng.close()
    return 0
