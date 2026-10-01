import femm
import  matplotlib.pyplot as plt
import numpy as np 

from Coil import Coil
from Payload import Payload



class Simulation:
    def __init__(self, time_step: float) -> None:
        self.time = 0
        self.dt = time_step

        self.payload = None
        self.payload_label = None     

        self.coils = None
        # Define the 'finish line' in define_coils()
        self.max_z = None

        femm.openfemm()
        # femm.main_minimize()
        femm.newdocument(0)  # Magnetics

        # Axisymmetric problem
        femm.mi_probdef(
            0,              # frequency, TODO play with this later
            "meters",
            "axi",
            1e-8
        )

        femm.mi_getmaterial("Air")

        # The payload should never end up in -z so it should be fine to initialize air like this
        femm.mi_addblocklabel(0.001, -0.001)
        femm.mi_selectlabel(0.001, -0.001)

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
            coil.current,
            0       # 0 for Series, 1 for parallel
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
        # Load coils into sim
        self.coils = coil_array

        # Load each AWG material used by the coils from FEMM's material library.
        for awg in {coil.awg for coil in coil_array}:
            femm.mi_getmaterial(f"{awg} AWG")

        z_offset = 0
        for idx, coil in enumerate(coil_array):
            self.define_coil(coil, "coil_" + str(idx), z_offset)
            z_offset += (coil.length + coil_spacing)

        self.max_z = z_offset - coil_spacing

    # The z_coord is the "furthest back" point of the payload
    def define_payload(self, payload: Payload, z_coord) -> None:
        # Load payload into sim
        self.payload = payload

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
        m = 1e-4
        femm.mi_clearselected()
        femm.mi_selectrectangle(r_min - m, z_min - m, r_max + m, z_max + m, 4)  # 4 = all entity types
        femm.mi_setgroup(2)
        femm.mi_clearselected()

    def run_single_step(self) -> None:
        if self.time == 0:
            # Boundary condition
            femm.mi_makeABC()

        femm.mi_saveas("run.fem")
        femm.mi_analyze()
        femm.mi_loadsolution()

        self.payload.force = self.get_payload_force()
        print(self.payload.force)

        self.time += self.dt


    def update_payload(self,) -> None:
        old_z = self.payload.z

        # a = F/m
        self.payload.a = self.payload.force / self.payload.mass
        # v' = v + a * dt
        self.payload.v = self.payload.v + self.payload.a * self.dt
        # z' = z + v' * dt
        self.payload.z = self.payload.z + self.payload.v * self.dt

        dz = self.payload.z - old_z
        print(dz)
        # Translate payload
        femm.mo_close()
        femm.mi_selectgroup(2)
        femm.mi_movetranslate(0, dz)
        femm.mi_clearselected()

        r, z = self.payload_label
        self.payload_label = (r, z + dz)
    

    def get_payload_force(self) -> float:
        label_r, label_z = self.payload_label

        femm.mo_clearblock()
        femm.mo_selectblock(label_r, label_z)

        # 19 is the index number for force integral
        force_z = femm.mo_blockintegral(19)

        femm.mo_clearblock()
        return force_z


    def run(self):
        while self.payload.z < self.max_z:
            self.run_single_step()
            self.update_payload()