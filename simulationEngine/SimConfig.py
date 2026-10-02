"""
Simulation settings: defaults, validation and a geometry summary.

Shared by the web viewer's server (to check a form before launching a run and to draw a preview)
and by run_config.py (to build the simulation). It deliberately does not import femm, so it works
anywhere the viewer server runs.

A config is a plain JSON-able dict:

    {
      "name": "",                 label added to the run's folder name
      "dt": 0.02,                 time step, s
      "max_steps": 500,           stop after this many steps
      "max_time": null,           optional: stop after this much simulated time, s
      "min_z": -0.25,             furthest back the payload's rear face may go, m
      "start_z": 0.0,             where the payload's rear face starts, m
      "coil_spacing": 0.05,       gap between coils, m
      "coils": [ {"inner_radius": 0.1, "length": 0.2, "turns": 500, "awg": 10, "current": 10,
                  "schedule": null}, ... ],
      "payload": {"type": "cylinder", "material": "Pure Iron", "mass": 2, "radius": 0.05, "length": 0.1}
    }

A coil's "schedule" is optional. Without it the coil carries "current" for the whole run. With it, the
current follows the schedule instead (see CurrentSchedule.py):

    {"interp": "step" | "linear", "points": [{"t": 0, "current": 0}, {"t": 0.1, "current": 10}]}

All distances are metres, times seconds, currents amps, mass kilograms.
"""

import math
import os
import re
import warnings
from pathlib import Path

from Coil import Coil
from CurrentSchedule import CurrentSchedule, INTERPOLATIONS
from Payload import Payload

# Nothing else may sit closer than this to the payload (see Simulation.GROUP_MARGIN)
GROUP_MARGIN = 1e-4

MAX_COILS = 20
MAX_SCHEDULE_POINTS = 200
MAX_CURRENT = 10000
PAYLOAD_TYPES = {
    "sphere": ("radius",),
    "cylinder": ("radius", "length"),
    "tube": ("inner_radius", "outer_radius", "length"),
}
PAYLOAD_DIMENSIONS = ("radius", "length", "inner_radius", "outer_radius")

DEFAULT_CONFIG = {
    "name": "",
    "dt": 0.02,
    "max_steps": 500,
    "max_time": None,
    "min_z": -0.25,
    "start_z": 0.0,
    "coil_spacing": 0.05,
    "coils": [
        {"inner_radius": 0.1, "length": 0.2, "turns": 500, "awg": 10, "current": 10, "schedule": None},
        {"inner_radius": 0.1, "length": 0.2, "turns": 500, "awg": 10, "current": 10, "schedule": None},
        {"inner_radius": 0.1, "length": 0.2, "turns": 500, "awg": 10, "current": 10, "schedule": None},
    ],
    "payload": {"type": "cylinder", "material": "Pure Iron", "mass": 2, "radius": 0.05, "length": 0.1},
}


class ConfigError(ValueError):
    """Invalid settings. `errors` is a list of {"field": "coils[1].turns", "message": "..."}."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))


# --------------------------------------------------------------- FEMM library

def _library_candidates():
    override = os.environ.get("DACTYL_FEMM_PATH")
    if override:
        yield Path(override)
    home = Path.home()
    yield home / ".wine/drive_c/femm42/bin"
    yield home / ".wine/drive_c/Program Files/femm42/bin"
    yield home / ".wine/drive_c/Program Files (x86)/femm42/bin"
    yield Path("C:/femm42/bin")


_library_cache: dict = {}


def femm_library():
    """
    Names of the materials in FEMM's library (matlib.dat) as {"materials": [...], "awg": [...]},
    or None if the library can't be found (checks that need it are then skipped).
    """
    for folder in _library_candidates():
        path = folder / "matlib.dat"
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        cached = _library_cache.get(str(path))
        if cached and cached[0] == stamp:
            return cached[1]
        text = path.read_text(errors="ignore")
        names = []
        for name in re.findall(r'<BlockName>\s*=\s*"([^"]*)"', text):
            if name not in names:
                names.append(name)
        awg = sorted({int(m.group(1)) for n in names if (m := re.fullmatch(r"(\d+) AWG", n))})
        library = {"materials": names, "awg": awg}
        _library_cache[str(path)] = (stamp, library)
        return library
    return None


# ----------------------------------------------------------------- validation

def _check_schedule(check, field, raw):
    """Validate a coil's current schedule. Returns the cleaned dict, or None after recording errors."""
    if not isinstance(raw, dict):
        check.error(field, "must be an object with 'interp' and 'points'")
        return None
    n_before = len(check.errors)
    interp = raw.get("interp", "step")
    if interp not in INTERPOLATIONS:
        check.error(f"{field}.interp", f"must be one of {', '.join(INTERPOLATIONS)}")
    raw_points = raw.get("points")
    points = []
    if not isinstance(raw_points, list) or not raw_points:
        check.error(f"{field}.points", "add at least one point")
    elif len(raw_points) > MAX_SCHEDULE_POINTS:
        check.error(f"{field}.points", f"at most {MAX_SCHEDULE_POINTS} points are supported")
    else:
        previous = None
        for j, p in enumerate(raw_points):
            if not isinstance(p, dict):
                check.error(f"{field}.points[{j}]", "must be an object with 't' and 'current'")
                continue
            t = check.number(f"{field}.points[{j}].t", p.get("t"), low=0, high=1e6)
            i = check.number(f"{field}.points[{j}].current", p.get("current"), low=-MAX_CURRENT, high=MAX_CURRENT)
            if t is not None and previous is not None and t <= previous:
                check.error(f"{field}.points[{j}].t", f"must be later than the previous point ({previous:g} s)")
            if t is not None:
                previous = t
            if t is not None and i is not None:
                points.append({"t": t, "current": i})
    if len(check.errors) > n_before:
        return None
    return {"interp": interp, "points": points}


class _Checker:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, field, message):
        self.errors.append({"field": field, "message": message})

    def warn(self, field, message):
        self.warnings.append({"field": field, "message": message})

    def number(self, field, value, *, low=None, high=None, low_open=False, integer=False, optional=False, default=None):
        """Return the value as a float (or int), or None after recording an error."""
        if value is None or value == "":
            if optional:
                return None
            if default is not None:
                return default
            self.error(field, "is required")
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.error(field, "must be a number")
            return None
        if not math.isfinite(value):
            self.error(field, "must be a finite number")
            return None
        if integer:
            if value != int(value):
                self.error(field, "must be a whole number")
                return None
            value = int(value)
        if low is not None and (value <= low if low_open else value < low):
            self.error(field, f"must be {'greater than' if low_open else 'at least'} {low:g}")
            return None
        if high is not None and value > high:
            self.error(field, f"must be at most {high:g}")
            return None
        return value


def validate(raw, library="auto"):
    """
    Check a config. Returns (config, warnings, geometry) for a valid config and raises ConfigError
    otherwise. `config` is cleaned up and complete (unused payload dimensions dropped, defaults filled in),
    `geometry` has everything needed to draw the layout.
    """
    if library == "auto":
        library = femm_library()
    check = _Checker()

    if not isinstance(raw, dict):
        raise ConfigError([{"field": "config", "message": "must be an object"}])

    name = raw.get("name") or ""
    if not isinstance(name, str) or len(name) > 40:
        check.error("name", "must be text of at most 40 characters")
        name = ""

    dt = check.number("dt", raw.get("dt"), low=0, high=10, low_open=True)
    max_steps = check.number("max_steps", raw.get("max_steps", DEFAULT_CONFIG["max_steps"]), low=1, high=10000, integer=True)
    max_time = check.number("max_time", raw.get("max_time"), low=0, low_open=True, optional=True)
    min_z = check.number("min_z", raw.get("min_z", DEFAULT_CONFIG["min_z"]), low=-10, high=10)
    start_z = check.number("start_z", raw.get("start_z", DEFAULT_CONFIG["start_z"]), low=-10, high=10)
    coil_spacing = check.number("coil_spacing", raw.get("coil_spacing"), low=0, high=5, low_open=True)

    if min_z is not None and start_z is not None and start_z < min_z:
        check.error("start_z", f"must not be behind min_z ({min_z:g})")

    # ---- coils
    coil_objects, coil_configs = [], []
    raw_coils = raw.get("coils")
    if not isinstance(raw_coils, list) or not raw_coils:
        check.error("coils", "add at least one coil")
        raw_coils = []
    elif len(raw_coils) > MAX_COILS:
        check.error("coils", f"at most {MAX_COILS} coils are supported")
        raw_coils = []

    valid_awg = set(library["awg"]) if library and library["awg"] else None
    for i, rc in enumerate(raw_coils):
        prefix = f"coils[{i}]"
        if not isinstance(rc, dict):
            check.error(prefix, "must be an object")
            continue
        n_before = len(check.errors)
        inner = check.number(f"{prefix}.inner_radius", rc.get("inner_radius"), low=0, high=5, low_open=True)
        length = check.number(f"{prefix}.length", rc.get("length"), low=0, high=5, low_open=True)
        turns = check.number(f"{prefix}.turns", rc.get("turns"), low=1, high=100000, integer=True)
        awg = check.number(f"{prefix}.awg", rc.get("awg"), integer=True)
        raw_schedule = rc.get("schedule")
        schedule_config = _check_schedule(check, f"{prefix}.schedule", raw_schedule) if raw_schedule is not None else None
        # With a schedule the constant current is not needed (it is kept, but not used)
        current = check.number(f"{prefix}.current", rc.get("current"), low=-MAX_CURRENT, high=MAX_CURRENT,
                               optional=raw_schedule is not None)
        if current is None and schedule_config is not None:
            current = schedule_config["points"][0]["current"]
        if awg is not None and valid_awg is not None and awg not in valid_awg:
            check.error(f"{prefix}.awg", f"FEMM's library only has {', '.join(map(str, sorted(valid_awg)))} AWG")
        elif awg is not None and valid_awg is None and not 0 <= awg <= 40:
            check.error(f"{prefix}.awg", "must be between 0 and 40")
        if len(check.errors) > n_before:
            continue

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            schedule = CurrentSchedule.from_config(schedule_config) if schedule_config else None
            coil = Coil(inner_radius=inner, length=length, turns=turns, awg=awg, current=current, schedule=schedule)
        for w in caught:
            check.warn(prefix, str(w.message))
        density = coil.peak_current / (coil.wire_area * 1e6)
        if density > 10:
            check.warn(prefix, f"{density:.0f} A/mm² in the wire is well above what real wire can carry continuously (about 5-10)")
        if coil.wire_diameter > length:
            check.warn(prefix, f"the wire ({coil.wire_diameter * 1000:.1f} mm) is thicker than the coil is long ({length * 1000:.0f} mm), so it is modelled as one turn per layer")
        coil_objects.append(coil)
        coil_configs.append({"inner_radius": inner, "length": length, "turns": turns, "awg": awg, "current": current,
                             "schedule": schedule_config})

    # ---- payload
    payload_config, payload_object = None, None
    raw_payload = raw.get("payload")
    if not isinstance(raw_payload, dict):
        check.error("payload", "is required")
    else:
        kind = str(raw_payload.get("type", "")).strip().lower()
        if kind not in PAYLOAD_TYPES:
            check.error("payload.type", f"must be one of {', '.join(PAYLOAD_TYPES)}")
        n_before = len(check.errors)
        material = raw_payload.get("material")
        if not isinstance(material, str) or not material.strip():
            check.error("payload.material", "is required")
        elif library and material not in library["materials"]:
            check.error("payload.material", "is not in FEMM's material library")
        mass = check.number("payload.mass", raw_payload.get("mass"), low=0, high=1e5, low_open=True)
        dims = {}
        if kind in PAYLOAD_TYPES:
            for dim in PAYLOAD_TYPES[kind]:
                dims[dim] = check.number(f"payload.{dim}", raw_payload.get(dim), low=0, high=5, low_open=True)
            if kind == "tube" and None not in (dims["inner_radius"], dims["outer_radius"]) \
                    and dims["inner_radius"] >= dims["outer_radius"]:
                check.error("payload.inner_radius", "must be smaller than the outer radius")
        if len(check.errors) == n_before and kind in PAYLOAD_TYPES:
            payload_object = Payload(type=kind, material=material, mass=mass, **dims)
            payload_config = {"type": kind, "material": material, "mass": mass, **dims}

            outer = dims.get("outer_radius", dims.get("radius"))
            if coil_objects:
                bore = min(c.inner_radius for c in coil_objects)
                if outer + 2 * GROUP_MARGIN >= bore:
                    check.error(
                        "payload.radius" if kind != "tube" else "payload.outer_radius",
                        f"must be smaller than the smallest coil bore ({bore:g} m) by at least {2 * GROUP_MARGIN:g} m",
                    )

    if check.errors:
        raise ConfigError(check.errors)

    config = {
        "name": name.strip(),
        "dt": dt,
        "max_steps": max_steps,
        "max_time": max_time,
        "min_z": min_z,
        "start_z": start_z,
        "coil_spacing": coil_spacing,
        "coils": coil_configs,
        "payload": payload_config,
    }

    # ---- geometry summary, laid out the same way Simulation.define_coils() does
    coils_geometry, z = [], 0.0
    for idx, (cfg, coil) in enumerate(zip(coil_configs, coil_objects)):
        coils_geometry.append({
            "id": f"coil_{idx}",
            "inner_radius": coil.inner_radius,
            "outer_radius": coil.outer_radius,
            "length": coil.length,
            "z0": z,
            "turns": coil.turns,
            "current": coil.current,
            "schedule": coil.schedule.to_config(),
            "awg": coil.awg,
        })
        z += coil.length + coil_spacing
    max_z = z - coil_spacing

    payload_meta = dict(payload_config)
    extent = 2 * payload_object.radius if payload_object.type == "sphere" else payload_object.length

    # Schedule points the run will never reach are probably a mistake
    run_time = dt * max_steps if max_time is None else min(dt * max_steps, max_time)
    for i, cfg in enumerate(coil_configs):
        if not cfg["schedule"]:
            continue
        times = [p["t"] for p in cfg["schedule"]["points"]]
        if times[-1] > run_time:
            check.warn(f"coils[{i}].schedule", f"has points after the end of the run ({run_time:g} s at most), which will never be used")
        gaps = [b - a for a, b in zip(times, times[1:])]
        if gaps and min(gaps) < dt * (1 - 1e-9):
            check.warn(f"coils[{i}].schedule",
                       f"has points closer together ({min(gaps):g} s) than the time step ({dt:g} s). The current is only sampled "
                       "once per step, so short pulses and sharp corners will be missed or distorted")

    # Payload overlapping a coil ring is impossible (checked above), but an unreasonable layout deserves a hint
    if start_z + extent > max_z + coil_spacing * 20:
        check.warn("start_z", "the payload starts far beyond the last coil")

    geometry = {
        "coils": coils_geometry,
        "payload": payload_meta,
        "start_z": start_z,
        "min_z": min_z,
        "max_z": max_z,
        "payload_extent": extent,
        "run_time": run_time,
    }
    return config, check.warnings, geometry
