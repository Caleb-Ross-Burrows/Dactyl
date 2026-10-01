"""
Local web viewer for Dactyl simulation runs.

    python webViewer/server.py                 # http://127.0.0.1:8000
    python webViewer/server.py --port 9000 --results path/to/results

Only needs numpy (already required by the simulation). The server binds to
127.0.0.1, so it is not reachable from other machines.

API
    GET  /api/runs                  list of runs (newest first)
    GET  /api/runs/<id>             meta + every history column
    GET  /api/runs/<id>/frame/<n>   |B| grid for step n, raw little-endian float32, shape (n_r, n_z)

    GET  /api/setup                 default settings, FEMM material library, limits
    POST /api/validate              check settings (JSON body); returns errors, warnings and the geometry layout
    POST /api/simulations           validate and start a simulation (one at a time)
    GET  /api/simulations/current   state, log and run id of the latest simulation started from here
    POST /api/simulations/stop      interrupt the running simulation
"""

import argparse
import collections
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
ENGINE_DIR = REPO / "simulationEngine"
STATIC_DIR = ROOT / "static"
DEFAULT_RESULTS = REPO / "results"

sys.path.insert(0, str(ENGINE_DIR))
import SimConfig            # noqa: E402  (no FEMM needed to validate settings)

MAX_BODY = 100_000

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


class JobManager:
    """
    Starts simulations as child processes (simulationEngine/run_config.py) and tracks the latest one.
    Only one runs at a time: every simulation drives the same FEMM instance through the same files.
    """

    LOG_LINES = 80
    INTERRUPT_GRACE = 20       # seconds between asking nicely, terminating and killing
    RUN_ID_MARKER = "DACTYL_RUN_ID="

    def __init__(self, python: str, results_dir: Path) -> None:
        self.python = python
        self.results_dir = results_dir
        self._lock = threading.Lock()
        self._job: dict | None = None
        self._proc: subprocess.Popen | None = None

    def start(self, config: dict) -> dict:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RuntimeError("A simulation is already running")

            env = dict(os.environ, DACTYL_RESULTS=str(self.results_dir), PYTHONUNBUFFERED="1")
            proc = subprocess.Popen(
                [self.python, "-u", str(ENGINE_DIR / "run_config.py"), "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(REPO), env=env, text=True, bufsize=1,
                start_new_session=True,     # Ctrl+C in this terminal must not hit the child directly
            )
            try:
                proc.stdin.write(json.dumps(config))
                proc.stdin.close()
            except OSError:
                pass

            self._proc = proc
            self._job = {
                "state": "running", "started": time.time(), "run_id": None, "returncode": None,
                "stopping": False, "name": config.get("name", ""), "log": collections.deque(maxlen=self.LOG_LINES),
            }
            job = self._job
            reader = threading.Thread(target=self._read_output, args=(proc, job), daemon=True)
            reader.start()
            threading.Thread(target=self._watch, args=(proc, job, reader), daemon=True).start()
            return self._snapshot(job)

    def _read_output(self, proc: subprocess.Popen, job: dict) -> None:
        # Not used to detect the end of the job: the FEMM process the simulation starts inherits this
        # pipe and can keep it open after the simulation itself has exited.
        for line in proc.stdout:
            line = line.rstrip("\n")
            with self._lock:
                if line.startswith(self.RUN_ID_MARKER):
                    job["run_id"] = line[len(self.RUN_ID_MARKER):].strip()
                else:
                    job["log"].append(line)

    def _watch(self, proc: subprocess.Popen, job: dict, reader: threading.Thread) -> None:
        code = proc.wait()
        reader.join(timeout=1)          # let the last lines of output arrive
        self._close_dangling_run(job)
        with self._lock:
            job["returncode"] = code
            job["state"] = "finished"
            job["finished"] = time.time()

    def _close_dangling_run(self, job: dict) -> None:
        """If the process died without finishing its run (killed), don't leave the run marked as running."""
        run_id = job.get("run_id")
        meta_path = self.results_dir / run_id / "meta.json" if run_id else None
        try:
            meta = json.loads(meta_path.read_text())
            if meta.get("status") == "running":
                meta["status"] = "interrupted"
                meta["message"] = "The simulation process ended before the run was finished"
                tmp = meta_path.with_name("meta.json.tmp")
                tmp.write_text(json.dumps(meta, indent=1))
                os.replace(tmp, meta_path)
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    @staticmethod
    def _snapshot(job: dict | None) -> dict:
        if job is None:
            return {"state": "idle"}
        out = {k: v for k, v in job.items() if k != "log"}
        out["log"] = list(job["log"])
        out["elapsed"] = (job.get("finished") or time.time()) - job["started"]
        return out

    def status(self) -> dict:
        with self._lock:
            return self._snapshot(self._job)

    def stop(self) -> bool:
        """Ask the running simulation to stop. Escalates to terminate, then kill, if it does not."""
        with self._lock:
            proc, job = self._proc, self._job
            if proc is None or proc.poll() is not None or job is None:
                return False
            job["stopping"] = True
        try:
            proc.send_signal(signal.SIGINT)          # run_config turns this into a clean 'interrupted' run
        except ProcessLookupError:
            return False
        threading.Thread(target=self._escalate, args=(proc,), daemon=True).start()
        return True

    def _escalate(self, proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=self.INTERRUPT_GRACE)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=self.INTERRUPT_GRACE)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            # Kill the whole session, including the FEMM it started, so the next run starts clean
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def shutdown(self) -> None:
        if self.stop():
            proc = self._proc
            try:
                proc.wait(timeout=self.INTERRUPT_GRACE)
            except subprocess.TimeoutExpired:
                pass


def default_python() -> str:
    """The project's virtual environment has pyfemm; fall back to the interpreter running this server."""
    venv = REPO / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


class Handler(BaseHTTPRequestHandler):
    store: RunStore
    jobs: JobManager
    allowed_hosts: set

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

    def _host_ok(self) -> bool:
        """Refuse requests whose Host is not this server's own address (DNS-rebinding protection)."""
        if self.headers.get("Host", "") not in self.allowed_hosts:
            self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
            return False
        return True

    def _read_json(self):
        """The JSON body of a POST, or None after sending an error response."""
        if "application/json" not in self.headers.get("Content-Type", ""):
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
            return None
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc not in self.allowed_hosts:
            self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
            return None
        if length < 0 or length > MAX_BODY:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
            return None
        try:
            return json.loads(self.rfile.read(length) or b"null")
        except json.JSONDecodeError as error:
            self._error(HTTPStatus.BAD_REQUEST, f"invalid JSON: {error}")
            return None

    # ----------------------------------------------------------------- routes

    def do_POST(self) -> None:
        try:
            if not self._host_ok():
                return
            parts = unquote(urlparse(self.path).path).strip("/").split("/")
            if parts[:1] != ["api"]:
                return self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
            parts = parts[1:]

            if parts == ["simulations", "stop"]:
                if self.headers.get("Content-Length", "0") not in ("", "0"):
                    self.rfile.read(min(int(self.headers["Content-Length"]), MAX_BODY))
                return self._json({"ok": True, "stopping": self.jobs.stop()})

            if parts not in (["validate"], ["simulations"]):
                return self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
            body = self._read_json()
            if body is None:
                return
            try:
                config, warnings_, geometry = SimConfig.validate(body)
            except SimConfig.ConfigError as error:
                status = HTTPStatus.OK if parts == ["validate"] else HTTPStatus.UNPROCESSABLE_ENTITY
                return self._json({"ok": False, "errors": error.errors}, status)

            if parts == ["validate"]:
                return self._json({"ok": True, "config": config, "warnings": warnings_, "geometry": geometry})
            try:
                job = self.jobs.start(config)
            except RuntimeError as error:
                return self._error(HTTPStatus.CONFLICT, str(error))
            except OSError as error:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"could not start the simulation: {error}")
            self._json({"ok": True, "job": job, "warnings": warnings_}, HTTPStatus.ACCEPTED)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, repr(error))

    def do_GET(self) -> None:
        try:
            if not self._host_ok():
                return
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

        if parts == ["setup"]:
            return self._json({
                "defaults": SimConfig.DEFAULT_CONFIG,
                "library": SimConfig.femm_library(),
                "payload_types": {k: list(v) for k, v in SimConfig.PAYLOAD_TYPES.items()},
                "max_coils": SimConfig.MAX_COILS,
            })

        if parts == ["simulations", "current"]:
            return self._json(self.jobs.status())

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
    parser.add_argument("--python", default=default_python(), help="interpreter that has pyfemm, used to run simulations")
    parser.add_argument("--open", action="store_true", help="open the viewer in the default browser")
    args = parser.parse_args()

    Handler.store = RunStore(args.results)
    Handler.jobs = JobManager(args.python, args.results.resolve())
    Handler.allowed_hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Dactyl viewer: {url}   (results: {args.results.resolve()})   Ctrl+C to stop")
    print(f"Simulations run with: {args.python}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        Handler.jobs.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
