"""
Run a simulation from a settings file (see SimConfig.py for the format).

    python simulationEngine/run_config.py                  # the default settings
    python simulationEngine/run_config.py settings.json    # settings from a file
    python simulationEngine/run_config.py -                # settings as JSON on stdin (used by the web viewer)

The viewer's "New simulation" panel runs exactly this, so a run started from a terminal and a run
started from the browser behave the same.

Environment:
    DACTYL_FEMM_PATH   folder containing femm.exe, if FEMM is not in the default Wine location
    DACTYL_RESULTS     where runs are saved (default: <repo>/results)
"""

import json
import os
import signal
import sys

import SimConfig

RUN_ID_MARKER = "DACTYL_RUN_ID="     # printed so whoever launched can find the run


def load_config(argument):
    if argument is None:
        return json.loads(json.dumps(SimConfig.DEFAULT_CONFIG))
    if argument == "-":
        return json.load(sys.stdin)
    with open(argument) as f:
        return json.load(f)


def run(raw_config) -> str:
    """Validate the settings, run the simulation and return how it ended."""
    config, warnings_, _geometry = SimConfig.validate(raw_config)
    for w in warnings_:
        print(f"warning: {w['field']}: {w['message']}", flush=True)

    # Imported here so that validation errors are reported without needing FEMM at all
    from Coil import Coil
    from CurrentSchedule import CurrentSchedule
    from Payload import Payload
    from Simulation import Simulation

    coils = []
    for c in config["coils"]:
        schedule = CurrentSchedule.from_config(c["schedule"]) if c.get("schedule") else None
        coils.append(Coil(**{k: v for k, v in c.items() if k != "schedule"}, schedule=schedule))
    payload = Payload(**config["payload"])

    sim = Simulation(
        config["dt"],
        run_name=config["name"] or None,
        results_dir=os.environ.get("DACTYL_RESULTS") or None,
        min_z=config["min_z"],
        femm_path=os.environ.get("DACTYL_FEMM_PATH") or None,
        config=config,
    )
    print(f"{RUN_ID_MARKER}{sim.recorder.run_id}", flush=True)

    status = "error"
    try:
        sim.define_coils(coil_array=coils, coil_spacing=config["coil_spacing"])
        sim.define_payload(payload=payload, z_coord=config["start_z"])
        status = sim.run(max_steps=config["max_steps"], max_time=config["max_time"])
    except KeyboardInterrupt:
        status = "interrupted"
    finally:
        try:
            sim.close()
        except Exception as error:      # FEMM may already be gone, e.g. after an interrupt
            print(f"warning: could not close FEMM cleanly: {error!r}", flush=True)
    return status


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # A process started from a background shell inherits "ignore SIGINT", which would make it unstoppable,
    # so install the handlers explicitly. Both signals end the run cleanly (marked 'interrupted', FEMM closed).
    signal.signal(signal.SIGINT, _raise_interrupt)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        raw = load_config(argv[0] if argv else None)
        status = run(raw)
    except SimConfig.ConfigError as error:
        for e in error.errors:
            print(f"invalid settings: {e['field']}: {e['message']}", file=sys.stderr, flush=True)
        return 2
    print(f"finished: {status}", flush=True)
    return 0 if status in ("complete", "stopped") else 1       # stopping at a step limit is normal


if __name__ == "__main__":
    sys.exit(main())
