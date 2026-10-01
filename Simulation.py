import femm
import  matplotlib as plt
import numpy as np 

from Coil import Coil



class Simulation:
    def __init__(self) -> None:
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
        femm.mi_addblocklabel(0, -0.001)
        femm.mi_selectlabel(0, -0.001)

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
            "AWG" + str(coil.awg), # Material
            0,          # Automatic mesh, 0 means use size in next arg, 1 means automatic
            0,          # Maximum mesh size, 0 means FEMM defualt
            coil_id,    # Assign properties to coil of this ID
            0,          # Magnetization direction in degrees
            1,          # Group Number, useful for selecting multiple objects
            1000        # turns
        )

        femm.mi_clearselected()


    def define_coils(self, coil_array: list[Coil], coil_spacing: float) -> None:
        # For now assume that all coils have the same gauge
        femm.mi_addmaterial("AWG" + str(coil_array[0].awg))

        z_offset = 0
        for idx, coil in enumerate(coil_array):
            self.define_coil(coil, "coil_" + str(idx), z_offset)
            z_offset += (coil.length + coil_spacing)

    def run(self):
        # Boundary condition
        femm.mi_makeABC()

        femm.mi_saveas("run.fem")
        femm.mi_analyze()
        femm.mi_loadsolution()

        # ----------------------------
        # Sample B field
        # along centerline r=0
        # ----------------------------

        z_vals = np.linspace(-50, 50, 200)
        b_vals = []

        for z in z_vals:
            Br, Bz = femm.mo_getb(0, z)
            b_vals.append(Bz)

        # ----------------------------
        # Plot
        # ----------------------------

        plt.figure(figsize=(8, 5))
        plt.plot(z_vals, b_vals)
        plt.xlabel("Axial Position (mm)")
        plt.ylabel("Bz (Tesla)")
        plt.title("Magnetic Field Along Coil Axis")
        plt.grid(True)
        plt.show()
