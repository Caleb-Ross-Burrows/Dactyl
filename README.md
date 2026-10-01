# Dactyl
A way to use FEMM for time dependent simulations.

The goal of this project is to model a moving, ferromagnetic payload inside a series of coils. This project was inspired by the wave of mss driver hype that has been stirring the past several years. A mass driver is a piece of proposed lunar infrastructure which could potentially launch fuel/resources to orbiting spacecraft from the lunar surface, all with simple solar power and electromagnets.

It works by running a static simulation for a certain time step and using simple Euler integration to advance the payload position based on the force calculated by FEMM. At the moment this system neglects a lot of time dependent considerations (things like back-emf or the response of the power supply) however it is still a useful tool for modelling and optimizing mass driver geometry.

Installation:


## Viewing results

Every simulation records itself to `results/<run id>/` (turn this off with `Simulation(dt, record=False)`).
Start the viewer and open it in a browser:

```
.venv/bin/python viewer/server.py --open        # http://127.0.0.1:8000
```

* **Flux density |B|** - an animated density plot of the axisymmetric cross-section (mirrored about the axis),
  with the coils and the payload drawn on top. Play/pause, scrub, step with the arrow keys, change the speed
  and the colour range.
* **Parameters** - tick any recorded quantity (force, velocity, position, kinetic energy, work done, |B| in the
  payload, coil flux linkage and voltage, ...) to get a plot of it, against time, position or step. Click a plot to
  jump to that step; the marker follows the animation.
* **Live runs** - a run that is still in progress can be opened and followed ("follow live").

Options: `--port 9000`, `--results <dir>`. The server only listens on 127.0.0.1.
The page loads Plotly from `viewer/static/vendor/plotly.min.js` if it exists, otherwise from the Plotly CDN.
To work offline, save https://cdn.plot.ly/plotly-2.35.2.min.js there.

`Simulation(dt, run_name="...", results_dir=..., grid_cell=...)` sets a label for the run folder, the output
location and the density-plot resolution.
