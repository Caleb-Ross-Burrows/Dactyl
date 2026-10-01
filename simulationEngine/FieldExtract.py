"""
Read a FEMM solution file (.ans) and turn it into plottable flux-density data.

Why parse the file instead of calling femm.mo_getb()?
Every FEMM call goes through a file-based link (and Wine on Linux), so sampling a
grid of a few thousand points would take far longer than the solve itself.

Field convention (verified against femm.mo_getb on an axisymmetric model):
the potential stored per node in the .ans file is the flux  Phi = 2*pi*r*A,  so

    Br = -(1/(2*pi*r)) dPhi/dz
    Bz =  (1/(2*pi*r)) dPhi/dr

Only DC (frequency = 0) axisymmetric magnetics problems are supported.
"""

from dataclasses import dataclass

import numpy as np
from matplotlib.tri import LinearTriInterpolator, Triangulation


@dataclass
class Mesh:
    """A solved FEMM mesh with the flux density already computed."""
    triangulation: Triangulation
    labels: np.ndarray       # block label index of each element
    area: np.ndarray         # (r, z) area of each element, m^2
    b_elem: np.ndarray       # |B| of each element, T
    b_node: np.ndarray       # |B| averaged onto the nodes, T

    def label_at(self, r: float, z: float) -> int | None:
        """Block label index of the element containing (r, z), or None if outside the mesh."""
        element = int(self.triangulation.get_trifinder()(r, z))
        return None if element < 0 else int(self.labels[element])


def read_solution(path) -> Mesh:
    with open(path, "r", errors="ignore") as f:
        lines = f.read().splitlines()

    header = {}
    solution_start = None
    for idx, line in enumerate(lines):
        line = line.strip()
        if line == "[Solution]":
            solution_start = idx
            break
        if line.startswith("[") and "=" in line:
            key, _, value = line.partition("=")
            header[key.strip("[] ")] = value.strip().strip('"')

    if solution_start is None:
        raise ValueError(f"{path}: no [Solution] section (did the analysis finish?)")
    if header.get("ProblemType", "").lower() != "axisymmetric":
        raise ValueError(f"{path}: only axisymmetric problems are supported")
    if float(header.get("Frequency", 0) or 0) != 0:
        raise ValueError(f"{path}: only DC (frequency = 0) problems are supported")

    n_nodes = int(lines[solution_start + 1])
    node_start = solution_start + 2
    nodes = np.array(
        [line.split()[:3] for line in lines[node_start:node_start + n_nodes]],
        dtype=float,
    )

    count_line = node_start + n_nodes
    n_elements = int(lines[count_line])
    elements = np.array(
        [line.split()[:4] for line in lines[count_line + 1:count_line + 1 + n_elements]],
        dtype=int,
    )
    if len(nodes) != n_nodes or len(elements) != n_elements:
        raise ValueError(f"{path}: solution section is truncated")

    r, z, phi = nodes[:, 0], nodes[:, 1], nodes[:, 2]
    corners = elements[:, :3]

    # Gradient of Phi over each linear triangle
    r0, r1, r2 = (r[corners[:, k]] for k in range(3))
    z0, z1, z2 = (z[corners[:, k]] for k in range(3))
    p0, p1, p2 = (phi[corners[:, k]] for k in range(3))

    twice_area = (r1 - r0) * (z2 - z0) - (r2 - r0) * (z1 - z0)
    safe = np.where(twice_area == 0, np.nan, twice_area)
    dphi_dr = ((z2 - z0) * (p1 - p0) - (z1 - z0) * (p2 - p0)) / safe
    dphi_dz = (-(r2 - r0) * (p1 - p0) + (r1 - r0) * (p2 - p0)) / safe

    r_centroid = np.maximum((r0 + r1 + r2) / 3, 1e-9)
    b_r = -dphi_dz / (2 * np.pi * r_centroid)
    b_z = dphi_dr / (2 * np.pi * r_centroid)
    b_elem = np.nan_to_num(np.hypot(b_r, b_z))
    area = np.abs(twice_area) / 2

    # Area-weighted average onto the nodes, so the plot is smooth instead of faceted
    weighted = np.zeros(n_nodes)
    weights = np.zeros(n_nodes)
    for k in range(3):
        weighted += np.bincount(corners[:, k], weights=b_elem * area, minlength=n_nodes)
        weights += np.bincount(corners[:, k], weights=area, minlength=n_nodes)
    b_node = np.divide(weighted, weights, out=np.zeros(n_nodes), where=weights > 0)

    return Mesh(
        triangulation=Triangulation(r, z, corners),
        labels=elements[:, 3],
        area=area,
        b_elem=b_elem,
        b_node=b_node,
    )


def sample_density(mesh: Mesh, r_axis: np.ndarray, z_axis: np.ndarray) -> np.ndarray:
    """|B| in tesla on the regular grid r_axis x z_axis, shape (len(r), len(z)). NaN outside the mesh."""
    interpolator = LinearTriInterpolator(mesh.triangulation, mesh.b_node)
    r_grid, z_grid = np.meshgrid(r_axis, z_axis, indexing="ij")
    values = interpolator(r_grid, z_grid)
    return np.ma.filled(values.astype(float), np.nan).astype(np.float32)


def region_stats(mesh: Mesh, r: float, z: float) -> dict:
    """Area-weighted mean and max |B| over the block whose label sits at (r, z)."""
    label = mesh.label_at(r, z)
    if label is None:
        return {}
    in_block = mesh.labels == label
    area = mesh.area[in_block]
    b = mesh.b_elem[in_block]
    return {
        "mean": float(np.sum(b * area) / np.sum(area)) if np.sum(area) > 0 else 0.0,
        "max": float(b.max()) if b.size else 0.0,
    }
