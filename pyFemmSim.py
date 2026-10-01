import femm
import numpy as np
import matplotlib.pyplot as plt

# ----------------------------
# Start FEMM
# ----------------------------
femm.openfemm()
# femm.main_minimize()
femm.newdocument(0)  # Magnetics

# Axisymmetric problem
femm.mi_probdef(
    0,              # frequency
    "millimeters",
    "axi",
    1e-8
)

# ----------------------------
# Materials
# ----------------------------

femm.mi_getmaterial("Air")

# ----------------------------
# Circuit definition
# ----------------------------

CURRENT = 100  # amps

femm.mi_addcircprop(
    "coil",
    CURRENT,
    1
)

# ----------------------------
# Simple coil geometry
#
# r = radial direction
# z = axis direction
# ----------------------------

r_inner = 10
r_outer = 20
z_start = -10
z_end = 10

# coil rectangle
femm.mi_drawrectangle(
    r_inner,
    z_start,
    r_outer,
    z_end
)

# coil block label
femm.mi_addblocklabel(15, 0)

femm.mi_selectlabel(15, 0)

femm.mi_setblockprop(
    "Air",
    0,
    0,
    "coil",
    0,
    1,
    1000      # turns
)

femm.mi_clearselected()

# ----------------------------
# Outer air region
# ----------------------------

femm.mi_addblocklabel(50, 0)

femm.mi_selectlabel(50, 0)

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

# ----------------------------
# Boundary
# ----------------------------

femm.mi_makeABC()

# ----------------------------
# Solve
# ----------------------------
femm.mi_saveas("test.fem")
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

# ----------------------------
# Cleanup
# ----------------------------

femm.closefemm()