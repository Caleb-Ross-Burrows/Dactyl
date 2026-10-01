"""
Saves a simulation run to disk so the web viewer (viewer/server.py) can display it.

results/<run id>/
    meta.json        geometry, grid definition, status        (rewritten atomically)
    history.jsonl    one JSON object per time step            (append only)
    frames/NNNNNN.npy  |B| on a regular (r, z) grid, float32  (one per step, written atomically)

Everything is written as the run progresses, so a run can be watched live and a run
that crashes part way still leaves its completed steps behind.
"""

import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

import numpy as np

import FieldExtract

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class RunRecorder:
    def __init__(self, results_dir=None, name: str | None = None, grid_cell: float | None = None) -> None:
        """
        results_dir: where runs are stored (default: <repo>/results)
        name:        optional label appended to the run id
        grid_cell:   size in metres of one density-plot cell (default: chosen from the geometry)
        """
        root = Path(results_dir) if results_dir else DEFAULT_RESULTS_DIR
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = "-" + re.sub(r"[^\w.-]+", "_", name) if name else ""
        self.run_id = stamp + suffix
        self.dir = root / self.run_id     # created on the first recorded step

        self.grid_cell = grid_cell
        self.config: dict | None = None     # the settings the run was built from, set by Simulation
        self.meta: dict | None = None
        self._grid: tuple[np.ndarray, np.ndarray] | None = None
        self._history = None
        self._steps = 0
        self._work = 0.0
        self._last_z: float | None = None
        self._last_force = 0.0

    # ------------------------------------------------------------------ setup

    def _start(self, sim) -> None:
        self.dir.mkdir(parents=True, exist_ok=False)
        (self.dir / "frames").mkdir()
        self._history = open(self.dir / "history.jsonl", "a", buffering=1)

        coils = []
        for idx, coil in enumerate(sim.coils):
            coils.append({
                "id": f"coil_{idx}",
                "inner_radius": coil.inner_radius,
                "outer_radius": coil.outer_radius,
                "length": coil.length,
                "z0": sim.coil_z0[idx],
                "turns": coil.turns,
                "current": coil.current,
                "schedule": coil.schedule.to_config(),
                "awg": coil.awg,
            })

        payload = sim.payload
        payload_meta = {"type": payload.type, "material": payload.material, "mass": payload.mass}
        for dim in ("radius", "length", "inner_radius", "outer_radius"):
            if hasattr(payload, dim):
                payload_meta[dim] = getattr(payload, dim)

        # Viewing window: everything near the coils, with some room around the payload's travel
        max_outer = max(c["outer_radius"] for c in coils)
        payload_extent = 2 * payload_meta["radius"] if payload.type == "sphere" else payload_meta["length"]
        r_max = 3 * max_outer
        pad = 0.15 * sim.max_z + 0.05
        z_min = min(-pad, sim.min_z - 0.05)
        z_max = sim.max_z + payload_extent + pad

        cell = self.grid_cell or max(0.004, (z_max - z_min) / 320)
        n_r = int(math.ceil(r_max / cell)) + 1
        n_z = int(math.ceil((z_max - z_min) / cell)) + 1
        r_axis = np.linspace(0, r_max, n_r)
        z_axis = np.linspace(z_min, z_max, n_z)
        self._grid = (r_axis, z_axis)

        self.meta = {
            "id": self.run_id,
            "created": datetime.now().isoformat(timespec="seconds"),
            "status": "running",
            "message": "",
            "dt": sim.dt,
            "steps": 0,
            "units": "meters",
            "coils": coils,
            "payload": payload_meta,
            "max_z": sim.max_z,
            "config": self.config,
            "min_z": sim.min_z,
            "grid": {
                "n_r": n_r, "n_z": n_z,
                "r_max": float(r_axis[-1]), "z_min": float(z_axis[0]), "z_max": float(z_axis[-1]),
                "dtype": "float32",
            },
        }
        self._write_meta()

    def _write_meta(self) -> None:
        _atomic_write(self.dir / "meta.json", json.dumps(self.meta, indent=1).encode())

    # -------------------------------------------------------------- recording

    def record_step(self, sim, solution_path, circuits: dict) -> None:
        """Call after a step has been solved and before the payload is moved."""
        if self.meta is None:
            self._start(sim)

        payload = sim.payload
        z = float(payload.z)
        force = float(payload.force)

        # Work done by the force so far (trapezoid rule on F over z), a check on the energy balance
        if self._last_z is not None:
            self._work += 0.5 * (force + self._last_force) * (z - self._last_z)
        self._last_z, self._last_force = z, force

        mesh = FieldExtract.read_solution(solution_path)
        frame = FieldExtract.sample_density(mesh, *self._grid)
        finite = frame[np.isfinite(frame)]

        row = {
            "step": self._steps,
            "t": float(sim.time),
            "z": z,
            "v": float(payload.v),
            "a": force / payload.mass,
            "force": force,
            "kinetic_energy": 0.5 * payload.mass * float(payload.v) ** 2,
            "work_done": self._work,
            "B_max": float(finite.max()) if finite.size else 0.0,
            # Used by the viewer to pick a colour range that ignores the singular coil corners
            "B_p995": float(np.percentile(finite, 99.5)) if finite.size else 0.0,
        }
        stats = FieldExtract.region_stats(mesh, *sim.payload_label)
        if stats:
            row["B_payload_mean"] = stats["mean"]
            row["B_payload_max"] = stats["max"]
        for coil_id, commanded in sim.coil_currents.items():
            props = circuits.get(coil_id)
            # The current FEMM reports back is the one it solved with; fall back to the scheduled value
            row[f"{coil_id}_current"] = props["current"] if props else commanded
            if props:
                row[f"{coil_id}_flux_linkage"] = props["flux_linkage"]
                row[f"{coil_id}_voltage"] = props["voltage"]

        frame_path = self.dir / "frames" / f"{self._steps:06d}.npy"
        tmp = frame_path.with_name(frame_path.name + ".tmp")
        with open(tmp, "wb") as f:
            np.save(f, frame)
        os.replace(tmp, frame_path)

        # History is written last: a row only exists once its frame is on disk
        self._history.write(json.dumps(row) + "\n")
        self._history.flush()
        self._steps += 1

        self.meta["steps"] = self._steps
        self._write_meta()

    def finish(self, status: str, message: str = "") -> None:
        """status: 'complete', 'stopped', 'interrupted' or 'error'."""
        if self._history is not None and not self._history.closed:
            self._history.close()
        if self.meta is not None:
            self.meta["status"] = status
            self.meta["message"] = message
            self.meta["finished"] = datetime.now().isoformat(timespec="seconds")
            self._write_meta()
