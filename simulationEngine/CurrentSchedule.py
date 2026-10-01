"""
Coil current as a function of time.

A schedule is a list of (time, current) points plus an interpolation rule:

    "step"    hold each value until the next point (switching is instantaneous)
    "linear"  ramp in a straight line between points

Before the first point the first value is held, and after the last point the last value is held,
so a schedule always defines a current for every time. To keep a coil off until t = 0.1 s, give it
the points (0, 0) and (0.1, 10).

The simulation evaluates the schedule at the start of each time step, which is also when the
force is computed, so the current and the force always describe the same instant.
"""

import bisect
import math

INTERPOLATIONS = ("step", "linear")


class CurrentSchedule:
    def __init__(self, points, interp: str = "step") -> None:
        """points: iterable of (time, current) pairs, strictly increasing in time."""
        if interp not in INTERPOLATIONS:
            raise ValueError(f"interp must be one of {', '.join(INTERPOLATIONS)}")
        pts = [(float(t), float(i)) for t, i in points]
        if not pts:
            raise ValueError("a schedule needs at least one point")
        if any(not (math.isfinite(t) and math.isfinite(i)) for t, i in pts):
            raise ValueError("schedule points must be finite numbers")
        if any(b[0] <= a[0] for a, b in zip(pts, pts[1:])):
            raise ValueError("schedule times must be strictly increasing")
        self.interp = interp
        self.times = [t for t, _ in pts]
        self.currents = [i for _, i in pts]

    @classmethod
    def constant(cls, current: float) -> "CurrentSchedule":
        return cls([(0.0, current)], "step")

    @property
    def points(self) -> list[tuple[float, float]]:
        return list(zip(self.times, self.currents))

    @property
    def peak(self) -> float:
        """Largest absolute current the schedule ever reaches."""
        return max(abs(i) for i in self.currents)

    def at(self, t: float) -> float:
        # The simulation clock accumulates dt, so 0.1 + 0.1 + ... can land a hair below a switch time
        # such as 0.8 and apply the change a whole step late. A tiny tolerance prevents that.
        t = t + 1e-9
        if t <= self.times[0]:
            return self.currents[0]
        if t >= self.times[-1]:
            return self.currents[-1]
        k = bisect.bisect_right(self.times, t) - 1          # the point at or before t
        if self.interp == "step":
            return self.currents[k]
        t0, t1 = self.times[k], self.times[k + 1]
        i0, i1 = self.currents[k], self.currents[k + 1]
        return i0 + (i1 - i0) * (t - t0) / (t1 - t0)

    def to_config(self) -> dict:
        return {"interp": self.interp, "points": [{"t": t, "current": i} for t, i in self.points]}

    @classmethod
    def from_config(cls, data: dict) -> "CurrentSchedule":
        return cls([(p["t"], p["current"]) for p in data["points"]], data.get("interp", "step"))
