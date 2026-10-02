

class Payload:
    def __init__(
            self,
            type: str,
            material: str,
            mass: float,    # in kgs, this includes non-magnetic mass of the payload
            *,
            radius: float | None = None,
            length: float | None = None,
            inner_radius: float | None = None,
            outer_radius: float | None = None,
    ):
        if mass <= 0:
            raise ValueError("mass must be greater than zero")

        self.z = 0
        self.v = 0
        self.force = 0
        self.mass = mass
        
        geometry_type = type.strip().lower()

        required = {
            "sphere": {"radius"},
            "cylinder": {"radius", "length"},
            "tube": {"inner_radius", "outer_radius", "length"},
        }

        if geometry_type not in required:
            raise ValueError(
                f"Unknown geometry type: {geometry_type!r}. "
                f"Choose from {', '.join(required)}."
            )

        dimensions = {
            "radius": radius,
            "length": length,
            "inner_radius": inner_radius,
            "outer_radius": outer_radius,
        }

        needed = required[geometry_type]
        missing = {name for name in needed if dimensions[name] is None}
        if missing:
            raise ValueError(
                f"{geometry_type} requires: {', '.join(sorted(missing))}"
            )

        unexpected = {
            name for name, value in dimensions.items()
            if value is not None and name not in needed
        }
        if unexpected:
            raise ValueError(
                f"{geometry_type} does not use: {', '.join(sorted(unexpected))}"
            )

        for name in needed:
            value = dimensions[name]
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")

        if geometry_type == "tube" and inner_radius >= outer_radius:
            raise ValueError("inner_radius must be smaller than outer_radius")

        self.type = geometry_type
        self.material = material
        for name in needed:
            setattr(self, name, dimensions[name])
