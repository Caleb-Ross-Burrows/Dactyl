import math
import os
import time
import warnings

import femm

from Coil import Coil
from Payload import Payload
from Recorder import RunRecorder
from SimConfig import GROUP_MARGIN



class Simulation:
    # Clearance (m) used when grouping the payload geometry by rectangle select.
    # Nothing else may sit closer than this to the payload, or it would get grouped (and moved) too.
    GROUP_MARGIN = GROUP_MARGIN

    FEM_FILE = "run.fem"

    def __init__(self, time_step: float, record: bool = True, results_dir=None,
                 run_name: str | None = None, grid_cell: float | None = None,
                 min_z: float = -0.25, femm_path: str | None = None, config: dict | None = None) -> None:
        """
        min_z:       furthest back (m) the payload's rear face may travel. The open boundary is sized to
                     contain this range, and the run stops if the payload goes behind it.
        femm_path:   folder containing femm.exe, if FEMM is not installed in the default Wine location
        config:      the settings this run was built from, stored with the run so it can be reloaded in the viewer
        record:      save every step to disk so it can be opened in the web viewer (webViewer/server.py)
        results_dir: where to save runs (default: <repo>/results)
        run_name:    optional label added to the run's folder name
        grid_cell:   density-plot cell size in metres (default: chosen from the model geometry)
        """
        if time_step <= 0:
            raise ValueError("time_step must be greater than zero")

        self.time = 0
        self.dt = time_step
        self.recorder = RunRecorder(results_dir, run_name, grid_cell) if record else None
        if self.recorder is not None:
            self.recorder.config = config

        self.payload = None
        self.payload_label = None     

        self.min_z = min_z
        self.payload_extent = 0.0   # axial size of the payload, set by define_payload()

        # Open air label, placed by define_coils() once the coil size is known
        self.air_label = None
        self._abc_created = False
        self._large_motion_step_warned = False

        self.coils = None
        self.coil_currents = {}  # current applied to each coil in the latest step, A
        self.coil_z0 = []       # rear face of each coil, filled in by define_coils()
        # Define the 'finish line' in define_coils()
        self.max_z = None

        femm.openfemm(femmpath=femm_path) if femm_path else femm.openfemm()
        femm.main_minimize()
        femm.newdocument(0)  # Magnetics

        # Axisymmetric problem
        femm.mi_probdef(
            0,              # frequency, TODO play with this later
            "meters",
            "axi",
            1e-8
        )

        femm.mi_getmaterial("Air")


    def _set_air_label(self) -> None:
        # Safe to call repeatedly: adding a label on top of an existing one is a no-op in FEMM,
        # so this also restores the air label if FEMM drops it after a geometry edit.
        if self.air_label is None:
            return
        r, z = self.air_label
        femm.mi_addblocklabel(r, z)
        femm.mi_selectlabel(r, z)

        femm.mi_setblockprop(
            "Air",
            0,
            0,
            "",
            0,
            0,
            0
        )

        femm.mi_clearselected()


    def define_coil(self, coil: Coil, coil_id: str, z_coord: float) -> None:
        femm.mi_addcircprop(
            coil_id,
            coil.schedule.at(0.0),      # starting current; updated every step from the coil's schedule
            1       # FEMM: 0 = parallel, 1 = series. Only series applies the block's turn count (J = N*i/A)
        )

        # Enclosed region for the coil
        femm.mi_drawrectangle(
            coil.inner_radius,
            z_coord,
            coil.outer_radius,
            z_coord + coil.length
        )

        # coil block label
        femm.mi_addblocklabel(
            coil.inner_radius + 0.5*(coil.outer_radius - coil.inner_radius),
            z_coord + 0.5*coil.length
        )
        femm.mi_selectlabel(
            coil.inner_radius + 0.5*(coil.outer_radius - coil.inner_radius),
            z_coord + 0.5*coil.length
        )

        femm.mi_setblockprop(
            f"{coil.awg} AWG", # Must match the FEMM library material name.
            0,          # Automatic mesh, 0 means use size in next arg, 1 means automatic
            0,          # Maximum mesh size, 0 means FEMM defualt
            coil_id,    # Assign properties to coil of this ID
            0,          # Magnetization direction in degrees
            1,          # Group Number, useful for selecting multiple objects
            coil.turns
        )

        femm.mi_clearselected()


    def define_coils(self, coil_array: list[Coil], coil_spacing: float) -> None:
        if self.coils is not None:
            raise RuntimeError("Coils have already been defined for this simulation")
        if not coil_array:
            raise ValueError("coil_array must contain at least one coil")
        if coil_spacing <= 0:
            raise ValueError("coil_spacing must be greater than zero (coils would overlap)")

        # Load coils into sim
        self.coils = coil_array

        # Load each AWG material used by the coils from FEMM's material library.
        for awg in {coil.awg for coil in coil_array}:
            femm.mi_getmaterial(f"{awg} AWG")

        z_offset = 0
        for idx, coil in enumerate(coil_array):
            self.coil_z0.append(z_offset)
            self.define_coil(coil, "coil_" + str(idx), z_offset)
            z_offset += (coil.length + coil_spacing)

        self.max_z = z_offset - coil_spacing

        # The air label sits just outside the outermost coil radius, halfway along the coil stack.
        # The payload lives inside the coil bore, so it can never reach this point wherever it travels.
        max_outer = max(coil.outer_radius for coil in coil_array)
        self.air_label = (1.25 * max_outer, 0.5 * self.max_z)
        self._set_air_label()

    # The z_coord is the "furthest back" point of the payload
    def define_payload(self, payload: Payload, z_coord) -> None:
        if self.payload is not None:
            raise RuntimeError("A payload has already been defined for this simulation")
        if z_coord < self.min_z:
            raise ValueError(f"z_coord ({z_coord} m) is behind min_z ({self.min_z} m), the back limit of the model")

        # The payload must fit inside the coil bore, otherwise the geometries intersect
        if self.coils:
            payload_outer = payload.outer_radius if payload.type == "tube" else payload.radius
            min_bore = min(coil.inner_radius for coil in self.coils)
            if payload_outer + 2 * self.GROUP_MARGIN >= min_bore:
                raise ValueError(
                    f"Payload outer radius ({payload_outer} m) must be smaller than the "
                    f"coil inner radius ({min_bore} m) with at least {2 * self.GROUP_MARGIN} m clearance"
                )

        # Load payload into sim
        self.payload = payload
        self.payload.z = z_coord
        self.payload_extent = 2 * payload.radius if payload.type == "sphere" else payload.length

        femm.mi_getmaterial(payload.material)

        # Enclosed region for the payload
        match payload.type:
            case "sphere":
                femm.mi_drawarc(
                    0,
                    z_coord,
                    0,
                    z_coord + 2 * payload.radius,
                    180,
                    1
                )
                # Close the semicircle along the axis of revolution.
                femm.mi_addsegment(
                    0,
                    z_coord + 2 * payload.radius,
                    0,
                    z_coord
                )

                label_r = payload.radius / 2
                label_z = z_coord + payload.radius

                r_min, r_max = 0, payload.radius
                z_min, z_max = z_coord, z_coord + 2 * payload.radius

            case "cylinder":
                femm.mi_drawrectangle(
                    0,
                    z_coord,
                    payload.radius,
                    z_coord + payload.length
                )
                label_r = payload.radius / 2
                label_z = z_coord + payload.length / 2

                r_min, r_max = 0, payload.radius
                z_min, z_max = z_coord, z_coord + payload.length

            case "tube":
                femm.mi_drawrectangle(
                    payload.inner_radius,
                    z_coord,
                    payload.outer_radius,
                    z_coord + payload.length
                )
                label_r = (payload.inner_radius + payload.outer_radius) / 2
                label_z = z_coord + payload.length / 2

                r_min, r_max = payload.inner_radius, payload.outer_radius
                z_min, z_max = z_coord, z_coord + payload.length

            case _:
                raise ValueError(f"Unsupported payload type: {payload.type!r}")

        # Add and select a block label inside the payload's cross-section.
        femm.mi_addblocklabel(label_r, label_z)
        femm.mi_selectlabel(label_r, label_z)

        self.payload_label = (label_r, label_z)

        femm.mi_setblockprop(
            payload.material,
            0,
            0,
            "",
            0,
            2,
            0
        )

        femm.mi_clearselected()

        # Add payload vertices to group 2 as well
        m = self.GROUP_MARGIN
        femm.mi_clearselected()
        femm.mi_selectrectangle(r_min - m, z_min - m, r_max + m, z_max + m, 4)  # 4 = all entity types
        femm.mi_setgroup(2)
        femm.mi_clearselected()


    def _check_ready(self) -> None:
        if not self.coils:
            raise RuntimeError("Call define_coils() before running the simulation")
        if self.payload is None or self.payload_label is None:
            raise RuntimeError("Call define_payload() before running the simulation")


    def _make_boundary(self) -> None:
        """Open boundary: a sphere centred on the axis, sized to contain the coils and the payload's whole travel."""
        z_back = min(self.min_z, 0.0)
        z_front = self.max_z + self.payload_extent
        max_outer = max(coil.outer_radius for coil in self.coils)

        half_length = (z_front - z_back) / 2
        centre = (z_front + z_back) / 2
        # 1.75x the half-diagonal of the region of interest (about what FEMM's default picks)
        radius = 1.75 * math.hypot(half_length, max_outer)

        # mi_makeABC(shells, radius, centre r, centre z, boundary type: 0 = Dirichlet)
        femm.mi_makeABC(7, radius, 0, centre, 0)


    def run_single_step(self) -> None:
        self._check_ready()

        if not self._abc_created:
            # Boundary condition, only create it once or the shells get duplicated
            self._make_boundary()
            self._abc_created = True

        self._apply_currents()

        femm.mi_saveas(self.FEM_FILE)
        femm.mi_analyze()
        femm.mi_loadsolution()

        self.payload.force = self.get_payload_force()
        print(f"t = {self.time:.4f} s, z = {self.payload.z:.4f} m, F = {self.payload.force:.4f} N")

        self._record_step()

        self.time += self.dt


    def _apply_currents(self) -> None:
        """Set every coil's current to its scheduled value at the current time. Call before solving."""
        self.coil_currents = {}
        for idx, coil in enumerate(self.coils):
            coil_id = f"coil_{idx}"
            value = coil.schedule.at(self.time)
            # propnum 1 is the circuit's total current (verified against mo_getcircuitproperties)
            femm.mi_modifycircprop(coil_id, 1, value)
            self.coil_currents[coil_id] = value


    def get_circuit_properties(self) -> dict:
        """Voltage and flux linkage of every coil circuit. Call after mi_loadsolution()."""
        circuits = {}
        for idx in range(len(self.coils)):
            coil_id = f"coil_{idx}"
            # pyfemm talks to FEMM through files and an occasional read comes back empty, so retry
            for _ in range(3):
                try:
                    current, voltage, flux_linkage = femm.mo_getcircuitproperties(coil_id)
                except (TypeError, ValueError):
                    continue
                circuits[coil_id] = {"current": current, "voltage": voltage, "flux_linkage": flux_linkage}
                break
            else:
                warnings.warn(f"Could not read circuit properties of {coil_id} at t = {self.time:.4f} s")
        return circuits


    def _record_step(self) -> None:
        if self.recorder is None:
            return
        # A problem with recording must never take the simulation down with it
        try:
            solution_path = os.path.splitext(self.FEM_FILE)[0] + ".ans"
            self.recorder.record_step(self, solution_path, self.get_circuit_properties())
        except Exception as error:
            warnings.warn(f"Recording disabled, could not save step at t = {self.time:.4f} s: {error!r}")
            self.recorder.finish("error", f"Recording failed: {error!r}")
            self.recorder = None


    def update_payload(self,) -> None:
        old_z = self.payload.z

        # a = F/m
        acceleration = self.payload.force / self.payload.mass
        # v' = v + a * dt
        self.payload.v = self.payload.v + acceleration * self.dt
        # z' = z + v' * dt
        self.payload.z = self.payload.z + self.payload.v * self.dt

        dz = self.payload.z - old_z

        # A large displacement samples too little of the changing field per solve. This commonly
        # looks like field/force "vibration" in the animation, especially near coil and material edges.
        if (not self._large_motion_step_warned and self.coils
                and abs(dz) > 0.05 * min(coil.length for coil in self.coils)):
            warnings.warn(
                f"Payload moved {abs(dz):.4g} m in one {self.dt:g} s step (over 5% of the shortest coil length). "
                "The trajectory and B-field animation may skip rapid spatial changes; reduce the time step."
            )
            self._large_motion_step_warned = True

        # close the post-processor window before editing the model again
        femm.mo_close()

        if dz != 0:
            # Translate payload
            femm.mi_clearselected()
            femm.mi_selectgroup(2)
            femm.mi_movetranslate(0, dz)
            femm.mi_clearselected()

            r, z = self.payload_label
            self.payload_label = (r, z + dz)

        # Make sure the move didn't remove the open-air properties
        self._set_air_label()
    

    def get_payload_force(self, attempts: int = 5) -> float:
        label_r, label_z = self.payload_label

        # pyfemm talks to FEMM through files, and an occasional reply comes back empty (as []).
        # An empty reply can also mean the block was not selected, so re-select on every attempt.
        force_z = None
        for attempt in range(attempts):
            femm.mo_clearblock()
            femm.mo_selectblock(label_r, label_z)

            # 19 is the index number for force integral
            force_z = femm.mo_blockintegral(19)

            femm.mo_clearblock()

            if isinstance(force_z, (int, float)) and math.isfinite(force_z):
                return float(force_z)

            warnings.warn(
                f"FEMM returned {force_z!r} for the payload force at t = {self.time:.4f} s "
                f"(attempt {attempt + 1}/{attempts}), retrying"
            )
            time.sleep(0.2 * (attempt + 1))

        raise RuntimeError(
            f"Invalid force from FEMM ({force_z!r}) after {attempts} attempts. Check that the payload "
            f"label {self.payload_label} is inside the payload and that the solution loaded."
        )


    def run(self, max_steps: int = 1000, max_time: float | None = None) -> str:
        """Run until the payload passes the last coil or a limit is hit. Returns 'complete' or 'stopped'."""
        self._check_ready()

        status, message = "complete", ""
        steps = 0
        try:
            while self.payload.z < self.max_z:
                if steps >= max_steps:
                    message = f"Stopped after {max_steps} steps without reaching the end of the coils"
                    status = "stopped"
                    warnings.warn(message)
                    break
                if max_time is not None and self.time >= max_time:
                    message = f"Stopped at max_time = {max_time} s without reaching the end of the coils"
                    status = "stopped"
                    warnings.warn(message)
                    break

                self.run_single_step()
                self.update_payload()
                steps += 1

                # Past this point the payload would leave the region the open boundary was sized for
                if self.payload.z < self.min_z:
                    message = f"Payload moved behind min_z = {self.min_z} m, stopping the simulation"
                    status = "stopped"
                    warnings.warn(message)
                    break
        except KeyboardInterrupt:
            status, message = "interrupted", "Interrupted by the user"
            raise
        except Exception as error:
            status, message = "error", repr(error)
            raise
        finally:
            if self.recorder is not None:
                self.recorder.finish(status, message)
        return status


    def close(self) -> None:
        # Close any open post-processor window, then FEMM itself
        try:
            femm.mo_close()
        except Exception:
            pass
        femm.closefemm()
