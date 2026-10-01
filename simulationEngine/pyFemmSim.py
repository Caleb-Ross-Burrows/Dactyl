from Simulation import Simulation
from Coil import Coil
from Payload import Payload

coil_array = []
for i in range (3):
    coil = Coil(
        inner_radius=0.1,
        length=0.2,
        turns=500,
        awg=10,
        current=10,
    )
    coil_array.append(coil)

payload = Payload(
    type="cylinder",
    material="Pure Iron",
    mass=10,
    radius=0.08,
    length=0.1,
)

sim = Simulation(0.1)

sim.define_coils(coil_array=coil_array, coil_spacing=0.05)
sim.define_payload(payload=payload, z_coord=0)

sim.run()


