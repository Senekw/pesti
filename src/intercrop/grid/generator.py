"""Tessellate a field into management blocks.

Pipeline, in order, and the order matters:

1. Project the boundary into the field's stored working CRS. Everything after this is metres.
2. Erode the boundary inward by the headland depth. Headlands come out *before* tessellation,
   not after, so a boundary block's plantable area is right the first time.
3. Subtract exclusion footprints.
4. Rotate into the grid frame so the field's long axis is +x.
5. Lay an axis-aligned lattice of snapped-size cells over the rotated bounds.
6. Intersect each cell with the plantable region, drop slivers, rotate survivors back.
7. Reproject to WGS84 for storage.

Row and column indices are assigned in the *grid frame*, so column index runs along the long
axis. That is what makes block codes correspond to something a grower can drive down.
"""

from __future__ import annotations

import math

import numpy as np
from shapely import affinity
from shapely.geometry import Point, Polygon, box

from intercrop.domain.field import Field_
from intercrop.domain.grid import (
    GRID_GENERATOR_VERSION,
    GridSpec,
    ManagementBlock,
    ManagementGrid,
)
from intercrop.geometry import WGS84_EPSG, GridFrame, long_axis, reproject, utm_epsg_for


class GridGenerationError(Exception):
    """The field and spec cannot produce a usable grid. Always explains why."""


def build_grid(field: Field_, spec: GridSpec) -> ManagementGrid:
    """Generate the management grid for ``field`` under ``spec``."""
    boundary_m = field.boundary_m()
    if boundary_m.is_empty or boundary_m.area <= 0:
        raise GridGenerationError(f"field {field.name!r} has no plantable area in its boundary")

    # --- headlands -------------------------------------------------------------------
    headland_depth = spec.headland_depth_m
    plantable = boundary_m.buffer(-headland_depth) if headland_depth > 0 else boundary_m
    if plantable.is_empty or plantable.area <= 0:
        raise GridGenerationError(
            f"a {headland_depth:g} m headland ({spec.headland_multiple:g} x the "
            f"{spec.implement_width_m:g} m implement) consumes the whole of "
            f"{field.name!r} ({boundary_m.area / 10_000:.2f} ha). Either the field is too "
            "narrow for this machinery or the headland multiple is too large."
        )

    # --- exclusions ------------------------------------------------------------------
    exclusions = field.exclusion_footprint_m()
    if exclusions is not None:
        plantable = plantable.difference(exclusions)
    if plantable.is_empty or plantable.area <= 0:
        raise GridGenerationError(
            f"exclusions remove all plantable area from {field.name!r}"
        )

    # --- grid frame ------------------------------------------------------------------
    axis = long_axis(boundary_m)
    if spec.azimuth_override_deg is not None:
        azimuth = spec.azimuth_override_deg
        azimuth_source = "grower_override"
    elif axis.is_ambiguous:
        # A square field has no long axis. Picking whichever edge shapely returned would
        # silently set row direction for the whole farm, so the source string says so and
        # the CONFIRM turn is expected to ask.
        azimuth = axis.azimuth_deg
        azimuth_source = "arbitrary_field_near_square"
    else:
        azimuth = axis.azimuth_deg
        azimuth_source = "boundary_long_axis"

    min_x, min_y, _, _ = boundary_m.bounds
    frame = GridFrame(azimuth_deg=azimuth, origin=(float(min_x), float(min_y)))
    plantable_grid = frame.to_grid(plantable)

    # --- lattice ---------------------------------------------------------------------
    size = spec.block_size_m
    gx0, gy0, gx1, gy1 = plantable_grid.bounds
    n_cols = max(1, math.ceil((gx1 - gx0) / size))
    n_rows = max(1, math.ceil((gy1 - gy0) / size))

    if n_cols * n_rows > 250_000:
        raise GridGenerationError(
            f"{n_cols} x {n_rows} = {n_cols * n_rows} candidate blocks at {size:g} m is "
            "beyond what the optimizer can handle. Increase the block size."
        )

    nominal_area = size * size
    min_area = spec.min_plantable_fraction * nominal_area

    blocks: list[ManagementBlock] = []
    dropped = 0
    # Column index runs along +x, which is the field's long axis in this frame.
    for row in range(n_rows):
        for col in range(n_cols):
            cell = box(
                gx0 + col * size,
                gy0 + row * size,
                gx0 + (col + 1) * size,
                gy0 + (row + 1) * size,
            )
            piece = cell.intersection(plantable_grid)
            if piece.is_empty or piece.area < min_area:
                if not piece.is_empty:
                    dropped += 1
                continue

            piece_world = frame.to_world(piece)
            centroid = piece_world.centroid
            piece_wgs84 = reproject(piece_world, field.working_crs_epsg, WGS84_EPSG)
            centroid_wgs84 = reproject(centroid, field.working_crs_epsg, WGS84_EPSG)

            blocks.append(
                ManagementBlock(
                    code=f"R{row:02d}C{col:02d}",
                    row_index=row,
                    col_index=col,
                    geometry=dict(piece_wgs84.__geo_interface__),
                    centroid_lonlat=(float(centroid_wgs84.x), float(centroid_wgs84.y)),
                    centroid_xy_m=(float(centroid.x), float(centroid.y)),
                    nominal_area_m2=nominal_area,
                    plantable_area_m2=float(piece.area),
                )
            )

    if not blocks:
        raise GridGenerationError(
            f"no block retained at least {spec.min_plantable_fraction:.0%} of a "
            f"{size:g} m cell. The block size is too large for this field's shape."
        )

    return ManagementGrid(
        field_id=field.id,
        spec=spec,
        azimuth_deg=azimuth,
        frame_origin_xy_m=frame.origin,
        azimuth_source=azimuth_source,
        blocks=tuple(blocks),
        dropped_sliver_count=dropped,
        generator_version=GRID_GENERATOR_VERSION,
    )


# --------------------------------------------------------------------------------------
# Topology, for the pressure model and the solver's adjacency terms
# --------------------------------------------------------------------------------------


def centroid_distance_matrix(grid: ManagementGrid) -> tuple[tuple[str, ...], np.ndarray]:
    """Block-to-block centroid distances in metres.

    Held in memory rather than persisted. For 400 blocks this is a 400x400 float array —
    1.3 MB, built in milliseconds — and materialising 160,000 pair rows in a database to
    avoid recomputing that would be a poor trade.
    """
    codes = tuple(b.code for b in grid.blocks)
    coords = np.array([b.centroid_xy_m for b in grid.blocks], dtype=float)
    deltas = coords[:, None, :] - coords[None, :, :]
    return codes, np.sqrt((deltas**2).sum(axis=-1))


def edge_adjacency(grid: ManagementGrid, tolerance_m: float = 0.5) -> dict[str, tuple[str, ...]]:
    """Blocks sharing an edge, keyed by block code.

    Index-based rather than geometric: in the grid frame, neighbours are the four cells at
    +/-1 row or column. Cheaper and more robust than testing polygon touching, which gets
    unreliable once boundary blocks have been clipped to irregular shapes and may share only
    a corner or a floating-point sliver.

    ``tolerance_m`` is accepted for interface stability with a future geometric
    implementation and is currently unused.
    """
    del tolerance_m
    by_index = {(b.row_index, b.col_index): b.code for b in grid.blocks}
    out: dict[str, tuple[str, ...]] = {}
    for block in grid.blocks:
        neighbours = [
            by_index[(block.row_index + dr, block.col_index + dc)]
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1))
            if (block.row_index + dr, block.col_index + dc) in by_index
        ]
        out[block.code] = tuple(neighbours)
    return out


# --------------------------------------------------------------------------------------
# Synthesised boundaries for cold start
# --------------------------------------------------------------------------------------


def synthesise_rectangle(
    centre_lon: float, centre_lat: float, length_m: float, width_m: float, azimuth_deg: float = 0.0
) -> dict[str, object]:
    """Build a rectangular boundary from stated dimensions, as GeoJSON in WGS84.

    This is the "1 km by 1 km" cold-start path. The result is a *placeholder*: callers must
    keep :attr:`~intercrop.domain.spec.StatedDimensions.is_synthesised` set and present it as
    a shape derived from the grower's numbers, never as their field.

    Constructed in the local UTM zone so the sides really are the stated lengths, then
    reprojected. Building it directly in degrees would produce a rectangle 27% out of square
    at Punjab's latitude.
    """
    epsg = utm_epsg_for(centre_lon, centre_lat)
    centre_m = reproject(Point(centre_lon, centre_lat), WGS84_EPSG, epsg)
    half_l, half_w = length_m / 2.0, width_m / 2.0
    rect = Polygon(
        [
            (centre_m.x - half_l, centre_m.y - half_w),
            (centre_m.x + half_l, centre_m.y - half_w),
            (centre_m.x + half_l, centre_m.y + half_w),
            (centre_m.x - half_l, centre_m.y + half_w),
        ]
    )
    if azimuth_deg:
        rect = affinity.rotate(rect, azimuth_deg, origin=(centre_m.x, centre_m.y))
    return dict(reproject(rect, epsg, WGS84_EPSG).__geo_interface__)


def grid_summary(field: Field_, grid: ManagementGrid) -> dict[str, object]:
    """Human-facing numbers for the CONFIRM and PRESENT turns."""
    gross_ha = field.gross_area_ha
    plantable_ha = grid.plantable_area_ha
    exclusions = field.exclusion_footprint_m()
    exclusion_ha = 0.0
    if exclusions is not None:
        exclusion_ha = exclusions.intersection(field.boundary_m()).area / 10_000.0
    return {
        "gross_area_ha": round(gross_ha, 3),
        "plantable_area_ha": round(plantable_ha, 3),
        "lost_to_headland_and_exclusion_ha": round(gross_ha - plantable_ha, 3),
        "exclusion_area_ha": round(exclusion_ha, 3),
        "block_count": grid.block_count,
        "block_size_m": grid.spec.block_size_m,
        "passes_per_block": grid.spec.passes_per_block,
        "headland_depth_m": grid.spec.headland_depth_m,
        "dropped_sliver_count": grid.dropped_sliver_count,
        "partial_block_count": sum(1 for b in grid.blocks if b.is_partial),
        "azimuth_deg": round(grid.azimuth_deg, 2),
        "azimuth_source": grid.azimuth_source,
        "snap_note": grid.spec.snap_note,
        "grid_content_hash": grid.content_hash(),
    }
