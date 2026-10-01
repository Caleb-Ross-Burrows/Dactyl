from Simulation import Simulation
from Coil import Coil

coil_array = []
for i in range (3):
    coil = Coil(0.1, 0.2, 100, 4, 10)
    coil_array.append(coil)

Sim = Simulation()

Sim.define_coils(coil_array=coil_array, coil_spacing=0.05)

Sim.run()


