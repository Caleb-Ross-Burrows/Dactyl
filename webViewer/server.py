"""
Local web viewer for Dactyl simulation runs.

    python webViewer/server.py                 # http://127.0.0.1:8000
    python webViewer/server.py --port 9000 --results path/to/results

Only needs numpy (already required by the simulation). The server binds to
127.0.0.1, so it is not reachable from other machines.

API
    GET /api/runs                  list of runs (newest first)
    GET /api/runs/<id>             meta + every history column
    GET /api/runs/<id>/frame/<n>   |B| grid for step n, raw little-endian float32, shape (n_r, n_z)
"""

import argparse
import json
import mimetypes
import re
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_RESULTS = ROOT.parent / "results"

RUN_ID = re.compile(r"[\w.-]+")


class RunStore:
    """Reads runs from the results directory. Cheap to call repeatedly while a run is in progress."""

    def __init__(self, results_dir: Path) -> None:
        self.root = results_dir
        self._history_cache: dict[str, tuple[tuple[float, int], dict]] = {}
        self._lock = threading.Lock()

    def run_dir(self, run_id: str) -> Path | None:
        if not RUN_ID.fullmatch(run_id) or run_id in (".", ".."):
            return None
        path = self.root / run_id
        return path if (path / "meta.json").is_file() else None

    @staticmethod
    def _read_json(path: Path):
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def list_runs(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        runs = []
        for path in self.root.iterdir():
            meta = self._read_json(path / "meta.json") if path.is_dir() else None
            if meta:
                runs.append(meta)
        runs.sort(key=lambda m: m.get("created", ""), reverse=True)
        return runs

    def columns(self, run_id: str, run_dir: Path) -> dict:
        """History as {column: [values]}. Re-parsed only when the file has changed."""
        path = run_dir / "history.jsonl"
        try:
            stat = path.stat()
        except OSError:
            return {}
        signature = (stat.st_mtime, stat.st_size)

        with self._lock:
            cached = self._history_cache.get(run_id)
            if cached and cached[0] == signature:
                return cached[1]

        rows = []
        with open(path) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    break       # a line that is still being written
        keys = list(rows[0].keys()) if rows else []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        columns = {key: [row.get(key) for row in rows] for key in keys}

        with self._lock:
            self._history_cache[run_id] = (signature, columns)
        return columns

    def frame_bytes(self, run_dir: Path, step: int) -> bytes | None:
        path = run_dir / "frames" / f"{step:06d}.npy"
        try:
            return np.load(path).astype("<f4").tobytes()
        except (OSError, ValueError):
            return None


class Handler(BaseHTTPRequestHandler):
    store: RunStore

    def log_message(self, fmt, *args):          # keep the console quiet, errors still show
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    # ---------------------------------------------------------------- helpers

    def _send(self, status, body: bytes, content_type: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status=HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _error(self, status, message: str) -> None:
        self._json({"error": message}, status)

    # ----------------------------------------------------------------- routes

    def do_GET(self) -> None:
        try:
            path = unquote(urlparse(self.path).path)
            if path.startswith("/api/"):
                self._api(path[len("/api/"):].strip("/").split("/"))
            else:
                self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:      # never let one bad request kill the server thread
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, repr(error))

    def _api(self, parts: list[str]) -> None:
        if parts == ["runs"]:
            return self._json(self.store.list_runs())

        if len(parts) >= 2 and parts[0] == "runs":
            run_dir = self.store.run_dir(parts[1])
            if run_dir is None:
                return self._error(HTTPStatus.NOT_FOUND, "unknown run")

            if len(parts) == 2:
                meta = self.store._read_json(run_dir / "meta.json")
                if meta is None:
                    return self._error(HTTPStatus.NOT_FOUND, "unreadable meta.json")
                return self._json({"meta": meta, "columns": self.store.columns(parts[1], run_dir)})

            if len(parts) == 4 and parts[2] == "frame" and parts[3].isdigit():
                data = self.store.frame_bytes(run_dir, int(parts[3]))
                if data is None:
                    return self._error(HTTPStatus.NOT_FOUND, "no such frame")
                # Frames never change once written, so the browser may keep them
                return self._send(HTTPStatus.OK, data, "application/octet-stream", cache="max-age=3600")

        self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")

    def _static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        if relative.startswith("static/"):
            relative = relative[len("static/"):]
        target = (STATIC_DIR / relative).resolve()
        # Refuse anything that escapes the static directory (e.g. ../ in the URL)
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), content_type)


def main() -> None:
    parser = argparse.ArgumentParser(description="Web viewer for Dactyl simulation runs")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS, help="directory containing the saved runs")
    parser.add_argument("--open", action="store_true", help="open the viewer in the default browser")
    args = parser.parse_args()

    Handler.store = RunStore(args.results)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Dactyl viewer: {url}   (results: {args.results.resolve()})   Ctrl+C to stop")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
