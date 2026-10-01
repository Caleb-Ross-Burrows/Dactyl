import warnings
import math

# Coils will be assumed to be made of copper wire of a specified AWG size
# Distance units are all meters, current units are amps
class Coil:
    def __init__(self, inner_radius: float, length: float, turns: int, awg: int, current: float):
        self.inner_radius = inner_radius
        self.length = length
        self.turns = turns
        self.awg = awg
        self.current = current

        self.wire_diameter = 0.127 * 92**((36 - self.awg) / 39) / 1000
        self.wire_area = 0.25 * math.pi * self.wire_diameter**2

        num_of_layers = math.floor(self.turns * self.wire_diameter / self.length)

        if num_of_layers < 1:
            warnings.warn(
                "Coil is sparsely wound, results may be innacurate. For complete coil: turns * wire diameter should be greater than or equal to coil length"
                )

        self.outer_radius = max(self.wire_diameter * num_of_layers, self.wire_diameter)

        # If coil is sparsely wound, approximate current density by multiplying standard current density 
        # by ratio of total wire x-section area vs total coil x-section area
        wire_current_density = self.current / self.wire_area
        self.current_density = (
            wire_current_density if num_of_layers >= 1
            else wire_current_density * (self.turns * self.wire_area) / (self.length*(self.outer_radius - self.inner_radius))
        )

        
