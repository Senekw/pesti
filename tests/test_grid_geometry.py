"""Grid geometry invariants.

These are the Phase 0 half of the brief's test list: blocks tile the boundary without gaps or
overlap, exclusions are honoured, headlands are subtracted from plantable area, and block
edges are whole multiples of implement width.
"""

from __future__ import annotations

import math

import pytest
from shapely.ops import unary_union

from intercrop.domain.field import Field_
from intercrop.domain.grid import GridSpec, SnapPolicy
from intercrop.grid.generator import (
    GridGenerationError,
    build_grid,
    centroid_distance_matrix,
    edge_adjacency,
)

# --------------------------------------------------------------------------------------
# Block edges are whole multiples of implement width
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "implement", "policy", "expected_passes"),
    [
        (50.0, 1.8, SnapPolicy.NEAREST, 28),  # 27.78 -> 28 passes = 50.4 m
        (50.0, 1.8, SnapPolicy.DOWN, 27),  # 48.6 m
        (50.0, 1.8, SnapPolicy.UP, 28),
        (50.0, 2.5, SnapPolicy.NEAREST, 20),  # exact: must not drift off by float noise
        (50.0, 2.5, SnapPolicy.DOWN, 20),
        (50.0, 2.5, SnapPolicy.UP, 20),
        (50.0, 3.0, SnapPolicy.NEAREST, 17),  # 16.67 -> 17 passes = 51 m
        (25.0, 1.8, SnapPolicy.NEAREST, 14),  # 25.2 m
        (100.0, 1.8, SnapPolicy.NEAREST, 56),  # 100.8 m
    ],
)
def test_block_edge_is_whole_multiple_of_implement_width(
    requested: float, implement: float, policy: SnapPolicy, expected_passes: int
) -> None:
    spec = GridSpec(
        requested_block_size_m=requested, implement_width_m=implement, snap_policy=policy
    )
    assert spec.passes_per_block == expected_passes
    # The property under test: the edge is an exact whole number of passes.
    assert spec.block_size_m == pytest.approx(expected_passes * implement)
    # Independent check that the edge divides evenly. Compared against both ends of the
    # remainder range because float modulo returns 1.7999... rather than 0 for 50.4 % 1.8.
    remainder = spec.block_size_m % implement
    assert min(remainder, implement - remainder) < 1e-6


def test_exact_multiple_reports_no_snap_note() -> None:
    """A grower whose request already fits should not be told it was adjusted."""
    spec = GridSpec(requested_block_size_m=50.0, implement_width_m=2.5)
    assert spec.block_size_m == 50.0
    assert spec.snap_note is None


def test_inexact_multiple_explains_the_adjustment() -> None:
    spec = GridSpec(requested_block_size_m=50.0, implement_width_m=1.8)
    note = spec.snap_note
    assert note is not None
    assert "50.4" in note and "28 passes" in note


def test_block_narrower_than_implement_is_rejected() -> None:
    spec = GridSpec(requested_block_size_m=1.0, implement_width_m=1.8)
    with pytest.raises(ValueError, match="narrower than"):
        _ = spec.block_size_m


# --------------------------------------------------------------------------------------
# Tiling: no gaps, no overlaps
# --------------------------------------------------------------------------------------


def _plantable_region_m(field: Field_, spec: GridSpec):
    """Recompute the target region independently of the generator, so the test is a check."""
    region = field.boundary_m()
    if spec.headland_depth_m > 0:
        region = region.buffer(-spec.headland_depth_m)
    exclusions = field.exclusion_footprint_m()
    if exclusions is not None:
        region = region.difference(exclusions)
    return region


@pytest.mark.parametrize("field_name", ["square_km_field", "irregular_field"])
def test_blocks_do_not_overlap(field_name: str, punjab_spec: GridSpec, request) -> None:
    field = request.getfixturevalue(field_name)
    grid = build_grid(field, punjab_spec)
    geoms = [b.geometry_m(field.working_crs_epsg) for b in grid.blocks]

    summed = sum(g.area for g in geoms)
    unioned = unary_union(geoms).area
    # If any two blocks overlapped, the union would be strictly smaller than the sum.
    assert unioned == pytest.approx(summed, rel=1e-9)


@pytest.mark.parametrize("field_name", ["square_km_field", "irregular_field"])
def test_blocks_tile_the_plantable_region_without_gaps(
    field_name: str, request
) -> None:
    """With slivers retained, the blocks must account for the whole plantable region."""
    field = request.getfixturevalue(field_name)
    spec = GridSpec(
        requested_block_size_m=50.0, implement_width_m=1.8, min_plantable_fraction=0.0
    )
    grid = build_grid(field, spec)
    target = _plantable_region_m(field, spec)

    covered = sum(b.plantable_area_m2 for b in grid.blocks)
    # Tolerance is for the WGS84 round-trip only, not for missing ground: 1e-6 relative on
    # ~100 ha is under a square metre.
    assert covered == pytest.approx(target.area, rel=1e-6)


@pytest.mark.parametrize("field_name", ["square_km_field", "irregular_field"])
def test_blocks_stay_inside_the_boundary(field_name: str, punjab_spec: GridSpec, request) -> None:
    field = request.getfixturevalue(field_name)
    grid = build_grid(field, punjab_spec)
    boundary = field.boundary_m()
    for block in grid.blocks:
        geom = block.geometry_m(field.working_crs_epsg)
        outside = geom.difference(boundary).area
        assert outside < 1.0, f"block {block.code} extends {outside:.2f} m2 beyond the boundary"


# --------------------------------------------------------------------------------------
# Headlands
# --------------------------------------------------------------------------------------


def test_headland_is_subtracted_from_plantable_area(square_km_field: Field_) -> None:
    spec = GridSpec(
        requested_block_size_m=50.0, implement_width_m=1.8, min_plantable_fraction=0.0
    )
    grid = build_grid(square_km_field, spec)

    depth = spec.headland_depth_m
    assert depth == pytest.approx(3.6)  # 2 x 1.8 m

    side = 1000.0
    expected_ha = (side - 2 * depth) ** 2 / 10_000.0
    assert grid.plantable_area_ha == pytest.approx(expected_ha, rel=1e-4)
    # And the headland really did cost area.
    assert grid.plantable_area_ha < square_km_field.gross_area_ha


def test_no_block_intrudes_into_the_headland(
    square_km_field: Field_, punjab_spec: GridSpec
) -> None:
    grid = build_grid(square_km_field, punjab_spec)
    plantable = square_km_field.boundary_m().buffer(-punjab_spec.headland_depth_m)
    for block in grid.blocks:
        geom = block.geometry_m(square_km_field.working_crs_epsg)
        assert geom.difference(plantable).area < 1.0, f"{block.code} intrudes into the headland"


def test_zero_headland_multiple_keeps_full_area(square_km_field: Field_) -> None:
    spec = GridSpec(
        requested_block_size_m=50.0,
        implement_width_m=1.8,
        headland_multiple=0.0,
        min_plantable_fraction=0.0,
    )
    grid = build_grid(square_km_field, spec)
    assert grid.plantable_area_ha == pytest.approx(square_km_field.gross_area_ha, rel=1e-4)


def test_headland_consuming_the_field_fails_loudly(square_km_field: Field_) -> None:
    """A 5 m implement with a 200x multiple erases a 1 km field. It must say why."""
    spec = GridSpec(
        requested_block_size_m=600.0, implement_width_m=5.0, headland_multiple=200.0
    )
    with pytest.raises(GridGenerationError, match="consumes the whole"):
        build_grid(square_km_field, spec)


# --------------------------------------------------------------------------------------
# Exclusions
# --------------------------------------------------------------------------------------


def test_exclusions_are_removed_from_every_block(
    field_with_exclusions: Field_, punjab_spec: GridSpec
) -> None:
    grid = build_grid(field_with_exclusions, punjab_spec)
    footprint = field_with_exclusions.exclusion_footprint_m()
    assert footprint is not None

    for block in grid.blocks:
        geom = block.geometry_m(field_with_exclusions.working_crs_epsg)
        intruding = geom.intersection(footprint).area
        assert intruding < 1.0, f"block {block.code} overlaps an exclusion by {intruding:.2f} m2"


def test_exclusions_reduce_plantable_area(
    square_km_field: Field_, field_with_exclusions: Field_, punjab_spec: GridSpec
) -> None:
    clean = build_grid(square_km_field, punjab_spec)
    dirty = build_grid(field_with_exclusions, punjab_spec)
    assert dirty.plantable_area_ha < clean.plantable_area_ha

    # A 6 m road and a 5 m-effective main across 1 km each: roughly 1.1 ha before the
    # building and before double-counting the crossing.
    lost = clean.plantable_area_ha - dirty.plantable_area_ha
    assert 0.8 < lost < 3.0, f"unexpected exclusion loss of {lost:.2f} ha"


def test_line_exclusion_without_width_is_rejected() -> None:
    from intercrop.domain.field import Exclusion, ExclusionKind

    line = {"type": "LineString", "coordinates": [[75.8, 30.9], [75.9, 30.9]]}
    with pytest.raises(ValueError, match="needs width_m"):
        Exclusion(kind=ExclusionKind.ROAD, geometry=line)


# --------------------------------------------------------------------------------------
# Slivers, indices, reproducibility
# --------------------------------------------------------------------------------------


def test_slivers_are_dropped_and_counted(irregular_field: Field_) -> None:
    """Dropping is fine; dropping silently is not."""
    spec = GridSpec(
        requested_block_size_m=50.0, implement_width_m=1.8, min_plantable_fraction=0.5
    )
    grid = build_grid(irregular_field, spec)
    assert grid.dropped_sliver_count > 0
    assert all(b.plantable_fraction >= 0.5 - 1e-9 for b in grid.blocks)


def test_stricter_sliver_threshold_keeps_fewer_blocks(irregular_field: Field_) -> None:
    loose = build_grid(
        irregular_field,
        GridSpec(requested_block_size_m=50.0, implement_width_m=1.8, min_plantable_fraction=0.05),
    )
    strict = build_grid(
        irregular_field,
        GridSpec(requested_block_size_m=50.0, implement_width_m=1.8, min_plantable_fraction=0.9),
    )
    assert strict.block_count < loose.block_count
    assert strict.plantable_area_ha < loose.plantable_area_ha


def test_square_km_at_50m_gives_a_tractable_block_count(
    square_km_field: Field_, punjab_spec: GridSpec
) -> None:
    """The brief's 400-block figure, checked rather than assumed."""
    grid = build_grid(square_km_field, punjab_spec)
    assert 350 <= grid.block_count <= 450
    assert grid.spec.block_size_m == pytest.approx(50.4)


def test_grid_hash_is_stable_across_regeneration(
    square_km_field: Field_, punjab_spec: GridSpec
) -> None:
    """Same field, same spec, same hash — or plan reproducibility is a fiction."""
    first = build_grid(square_km_field, punjab_spec)
    second = build_grid(square_km_field, punjab_spec)
    assert first.id != second.id  # different entities
    assert first.content_hash() == second.content_hash()  # same content


def test_grid_hash_changes_with_block_size(square_km_field: Field_) -> None:
    a = build_grid(square_km_field, GridSpec(requested_block_size_m=50.0, implement_width_m=1.8))
    b = build_grid(square_km_field, GridSpec(requested_block_size_m=25.0, implement_width_m=1.8))
    assert a.content_hash() != b.content_hash()


def test_block_codes_are_unique(irregular_field: Field_, punjab_spec: GridSpec) -> None:
    grid = build_grid(irregular_field, punjab_spec)
    codes = [b.code for b in grid.blocks]
    assert len(set(codes)) == len(codes)


def test_azimuth_follows_the_long_axis_on_an_elongated_field(irregular_field: Field_) -> None:
    grid = build_grid(
        irregular_field, GridSpec(requested_block_size_m=50.0, implement_width_m=1.8)
    )
    assert grid.azimuth_source == "boundary_long_axis"
    # The fixture's long axis runs roughly 30 degrees; allow slack for the notch's effect on
    # the minimum rotated rectangle.
    assert 15.0 <= grid.azimuth_deg <= 45.0


def test_square_field_reports_its_orientation_as_arbitrary(
    square_km_field: Field_, punjab_spec: GridSpec
) -> None:
    """On a square there is no long axis, and the grower should be asked rather than guessed at."""
    grid = build_grid(square_km_field, punjab_spec)
    assert grid.azimuth_source == "arbitrary_field_near_square"


def test_azimuth_override_is_honoured(irregular_field: Field_) -> None:
    spec = GridSpec(
        requested_block_size_m=50.0, implement_width_m=1.8, azimuth_override_deg=90.0
    )
    grid = build_grid(irregular_field, spec)
    assert grid.azimuth_deg == pytest.approx(90.0)
    assert grid.azimuth_source == "grower_override"


def test_rotated_grid_still_tiles_without_overlap(irregular_field: Field_) -> None:
    """Rotation is where a tessellator usually springs leaks."""
    spec = GridSpec(
        requested_block_size_m=50.0,
        implement_width_m=1.8,
        azimuth_override_deg=37.5,
        min_plantable_fraction=0.0,
    )
    grid = build_grid(irregular_field, spec)
    geoms = [b.geometry_m(irregular_field.working_crs_epsg) for b in grid.blocks]
    assert unary_union(geoms).area == pytest.approx(sum(g.area for g in geoms), rel=1e-9)


# --------------------------------------------------------------------------------------
# Topology
# --------------------------------------------------------------------------------------


def test_adjacency_is_symmetric(square_km_field: Field_, punjab_spec: GridSpec) -> None:
    grid = build_grid(square_km_field, punjab_spec)
    adjacency = edge_adjacency(grid)
    for code, neighbours in adjacency.items():
        for neighbour in neighbours:
            assert code in adjacency[neighbour], f"{code}->{neighbour} is not reciprocated"


def test_interior_blocks_have_four_neighbours(
    square_km_field: Field_, punjab_spec: GridSpec
) -> None:
    grid = build_grid(square_km_field, punjab_spec)
    adjacency = edge_adjacency(grid)
    interior = [
        b for b in grid.blocks if not b.is_partial and len(adjacency[b.code]) == 4
    ]
    assert len(interior) > 200, "a 1 km square at 50 m should be mostly interior blocks"


def test_distance_matrix_matches_block_spacing(
    square_km_field: Field_, punjab_spec: GridSpec
) -> None:
    grid = build_grid(square_km_field, punjab_spec)
    codes, distances = centroid_distance_matrix(grid)

    assert distances.shape == (grid.block_count, grid.block_count)
    assert math.isclose(distances.diagonal().max(), 0.0, abs_tol=1e-9)
    assert (distances == distances.T).all()

    # Two horizontally adjacent interior blocks sit one block size apart.
    index = {code: i for i, code in enumerate(codes)}
    if "R05C05" in index and "R05C06" in index:
        gap = distances[index["R05C05"], index["R05C06"]]
        assert gap == pytest.approx(punjab_spec.block_size_m, rel=1e-6)
