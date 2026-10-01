# Dactyl
A way to use FEMM for time dependent simulations.

The goal of this project is to model a moving, ferromagnetic payload inside a series of coils. This project was inspired by the wave of mass driver hype that has been stirring the past several years. A mass driver is a piece of proposed lunar infrastructure which could potentially launch fuel/resources to orbiting spacecraft from the lunar surface, all with simple solar power and electromagnets.

It works by running a static simulation for a certain time step and using simple Euler integration to advance the payload position based on the force calculated by FEMM. At the moment this system neglects a lot of time dependent considerations (things like back-emf or the response of the power supply) however it is still a useful tool for modelling and optimizing mass driver geometry.

Installation:


## Running simulations

The easiest way is the browser. Start the viewer and open the **New simulation** panel at the top of the page:

```
.venv/bin/python webViewer/server.py --open        # http://127.0.0.1:8000
```

* Set the coils (any number, each with its own radius, length, turns, wire size and current), the payload (cylinder,
  sphere or tube, its material and mass), the time step, start position and limits.
* The form is checked as you type, using the same rules the simulation uses, and a preview shows the layout.
  Materials come from FEMM's own library, and wire sizes are limited to the ones that library has.
* **Current over time.** Each coil has a **Schedule** button. Tick *Follow a schedule* and give a list of (time, current)
  points, joined either by *hold* (the current switches instantly at each point) or *ramp* (straight lines). Before the
  first point the first current is held, and after the last point the last one is held, so to switch a coil on at
  0.1 s use (0 s, 0 A) and (0.1 s, 10 A). Currents may be negative. Without a schedule a coil carries its constant
  current for the whole run. A plot under the layout shows every coil's current over time.
* **Run simulation** starts it. The viewer switches to the new run as soon as its first step is recorded and
  follows it live. **Stop** ends it cleanly; the steps recorded so far are kept.
* **Load settings from selected run** copies a past run's settings back into the form, to tweak and re-run.
  The form also remembers what you last entered.

Only one simulation can run at a time, because they all drive the same FEMM instance. Don't start one from a
terminal while another is running.

From a terminal, the same settings can be used with:

```
.venv/bin/python simulationEngine/pyFemmSim.py                  # default settings (SimConfig.DEFAULT_CONFIG)
.venv/bin/python simulationEngine/pyFemmSim.py settings.json    # your own, same format as the form
```

A coil's schedule in a settings file looks like `"schedule": {"interp": "step", "points": [{"t": 0, "current": 0}, {"t": 0.1, "current": 10}]}`
next to the coil's other fields (`interp` is `step` or `linear`).

The current is looked up once at the start of each time step and held for that step, together with the force
calculation, so keep the time step smaller than the features of the schedule (the form warns if points are closer
together than the time step). The current each coil carried is recorded as `coil_N_current` and can be plotted.

The server runs simulations with `.venv/bin/python` (override with `--python`). If FEMM isn't in the default
Wine location, set `DACTYL_FEMM_PATH` to the folder containing `femm.exe`.

## Viewing results

Every simulation records itself to `results/<run id>/` (turn this off with `Simulation(dt, record=False)`).
Open the viewer as above to look at any run, finished or in progress.

* **Flux density |B|** - an animated density plot of the axisymmetric cross-section (mirrored about the axis),
  with the coils and the payload drawn on top. Play/pause, scrub, step with the arrow keys and change the colour range.
* **Playback speed** - *Real time* plays the simulated clock against the wall clock (1x, or slowed down / sped up),
  skipping frames if the display can't keep up. *Steps per second* plays every recorded step at a fixed rate instead.
* **Parameters** - tick any recorded quantity (force, velocity, position, kinetic energy, work done, |B| in the
  payload, coil flux linkage and resistive drop, ...) to get a plot of it, against time, position or step. Click a plot to
  jump to that step; the marker follows the animation.
* **Live runs** - a run that is still in progress can be opened and followed ("follow live").

Options: `--port 9000`, `--results <dir>`, `--python <interpreter>`. The server only listens on 127.0.0.1 and refuses
requests from other origins.
The page loads Plotly from `webViewer/static/vendor/plotly.min.js` if it exists, otherwise from the Plotly CDN.
To work offline, save https://cdn.plot.ly/plotly-2.35.2.min.js there.

`Simulation(dt, run_name="...", results_dir=..., grid_cell=..., min_z=...)` sets a label for the run folder, the output
location, the density-plot resolution and how far back the payload may travel.
