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
class RegionField:
    """Per-material interpolation mesh; interface nodes are not shared with other blocks."""
    triangulation: Triangulation
    b_r_node: np.ndarray
    b_z_node: np.ndarray


@dataclass
class Mesh:
    """A solved FEMM mesh with the flux density already computed."""
    triangulation: Triangulation
    labels: np.ndarray       # block label index of each element
    area: np.ndarray         # (r, z) area of each element, m^2
    b_r_elem: np.ndarray     # signed radial flux density of each element, T
    b_z_elem: np.ndarray     # signed axial flux density of each element, T
    b_elem: np.ndarray       # |B| of each element, T
    regions: dict[int, RegionField]

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

    # Phi = 2*pi*r*A_theta = pi*Bz*r^2 on the axis for a locally uniform axial field.
    # Linear triangles touching r=0 cannot represent this quadratic behavior: dividing their
    # constant dPhi/dr by a centroid radius overestimates Bz. Estimate Bz from Phi/(pi*r^2)
    # at the off-axis vertices instead. FEMM uses its own axis-element treatment; this limit
    # matches mo_getb much more closely than the naive gradient/r-centroid expression.
    axis_elements = np.any(np.abs(np.stack((r0, r1, r2), axis=1)) <= 1e-12, axis=1)
    axis_bz_sum = np.zeros(len(elements))
    axis_bz_count = np.zeros(len(elements))
    for k in range(3):
        rk = (r0, r1, r2)[k]
        pk = (p0, p1, p2)[k]
        valid = axis_elements & (rk > 1e-12)
        axis_bz_sum[valid] += pk[valid] / (np.pi * rk[valid] ** 2)
        axis_bz_count[valid] += 1
    has_axis_limit = axis_bz_count > 0
    b_z[has_axis_limit] = axis_bz_sum[has_axis_limit] / axis_bz_count[has_axis_limit]

    b_r = np.nan_to_num(b_r)
    b_z = np.nan_to_num(b_z)
    b_elem = np.hypot(b_r, b_z)
    area = np.abs(twice_area) / 2

    block_labels = elements[:, 3]
    regions = {}

    # Interpolate each FEMM block/material on its own mesh. Sharing nodal averages across
    # iron/air boundaries smooths a real B discontinuity, and remeshing moves that blend,
    # which appears as frame-to-frame field vibration. Separate meshes are one-sided at interfaces.
    for block in np.unique(block_labels):
        selected = np.flatnonzero(block_labels == block)
        old_nodes, inverse = np.unique(corners[selected].ravel(), return_inverse=True)
        local_triangles = inverse.reshape(-1, 3)
        local_r, local_z = r[old_nodes], z[old_nodes]
        n_local = len(old_nodes)

        weighted_r = np.zeros(n_local)
        weighted_z = np.zeros(n_local)
        weights = np.zeros(n_local)
        for k in range(3):
            weighted_r += np.bincount(local_triangles[:, k], weights=b_r[selected] * area[selected], minlength=n_local)
            weighted_z += np.bincount(local_triangles[:, k], weights=b_z[selected] * area[selected], minlength=n_local)
            weights += np.bincount(local_triangles[:, k], weights=area[selected], minlength=n_local)
        local_br = np.divide(weighted_r, weights, out=np.zeros(n_local), where=weights > 0)
        local_bz = np.divide(weighted_z, weights, out=np.zeros(n_local), where=weights > 0)
        local_br[np.abs(local_r) <= 1e-12] = 0.0       # axisymmetric regularity: Br(0,z) = 0
        regions[int(block)] = RegionField(
            triangulation=Triangulation(local_r, local_z, local_triangles),
            b_r_node=local_br,
            b_z_node=local_bz,
        )

    return Mesh(
        triangulation=Triangulation(r, z, corners),
        labels=block_labels,
        area=area,
        b_r_elem=b_r,
        b_z_elem=b_z,
        b_elem=b_elem,
        regions=regions,
    )


def sample_density(mesh: Mesh, r_axis: np.ndarray, z_axis: np.ndarray) -> np.ndarray:
    """|B| in tesla on the regular grid r_axis x z_axis, shape (len(r), len(z)). NaN outside the mesh."""
    r_grid, z_grid = np.meshgrid(r_axis, z_axis, indexing="ij")
    br = np.full(r_grid.shape, np.nan, dtype=float)
    bz = np.full(z_grid.shape, np.nan, dtype=float)
    for region in mesh.regions.values():
        br_region = np.ma.filled(
            LinearTriInterpolator(region.triangulation, region.b_r_node)(r_grid, z_grid).astype(float), np.nan
        )
        bz_region = np.ma.filled(
            LinearTriInterpolator(region.triangulation, region.b_z_node)(r_grid, z_grid).astype(float), np.nan
        )
        # Blocks are disjoint; at grid points lying exactly on an interface, keep the first block's side.
        mask = np.isnan(br) & np.isfinite(br_region) & np.isfinite(bz_region)
        br[mask] = br_region[mask]
        bz[mask] = bz_region[mask]
    return np.hypot(br, bz).astype(np.float32)


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
