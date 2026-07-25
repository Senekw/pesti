"""Serve the demo sheet.

Standard library only, on purpose: the demo must run from the same environment the tests
run in, with no extra install step and nothing to pin.

    python web/server.py            # http://127.0.0.1:8765
    python web/server.py --port 9000
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from pydantic import ValidationError  # noqa: E402

from intercrop.grid.generator import GridGenerationError  # noqa: E402
from intercrop.parameters.store import MissingParameter  # noqa: E402
from web.service import DemoInputError, build_plate, parameter_register  # noqa: E402

HERE = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    server_version = "intercrop-demo/0.1"
    PAGES: ClassVar[dict[str, str]] = {
        "/": "landing.html", "/chat": "chat.html", "/sheet": "index.html",
    }

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in self.PAGES:
            self._send(200, (HERE / self.PAGES[path]).read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/parameters":
            self._json(200, parameter_register())
        else:
            self._json(404, {"error": f"no route {path}"})

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/plate":
            self._json(404, {"error": f"no route {self.path}"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._json(400, {"error": f"malformed request: {exc}"})
            return

        try:
            self._json(200, build_plate(payload))
        except (GridGenerationError, DemoInputError, MissingParameter) as exc:
            # These messages are written to be read by a grower. Pass them straight through.
            self._json(400, {"error": str(exc).strip("'"), "kind": type(exc).__name__})
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"]) or "input"
            self._json(400, {"error": f"{where}: {first['msg']}", "kind": "ValidationError"})
        except ValueError as exc:
            self._json(400, {"error": str(exc), "kind": "ValueError"})
        except Exception:  # pragma: no cover - last resort, keeps the sheet honest
            traceback.print_exc()
            self._json(500, {"error": "the generator failed; see the server log",
                             "kind": "Unhandled"})

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("  %s\n" % (fmt % args))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Intercrop field sheet: {url}   (ctrl-c to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
