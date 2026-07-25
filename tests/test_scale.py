"""Scale guards.

The brief asks for the 1 km² case to solve in a time a human will sit through, recorded in CI
and failing the build on regression. Phase 2's optimizer does not exist yet, so what is
guarded here is everything upstream of it: grid generation, adjacency, and the distance
matrix at each candidate block size.

Thresholds are set generously — roughly 5x the observed time on a development machine — so
they catch an algorithmic regression (an accidental O(n²) polygon test, a per-block
reprojection) rather than flapping on CI noise. Observed times on the reference machine are
recorded in each test so the headroom is visible and a future tightening is informed.
"""

from __future__ import annotations

import time

import pytest

from intercrop.domain.field import Field_
from intercrop.domain.grid import GridSpec
from intercrop.grid.generator import (
    build_grid,
    centroid_distance_matrix,
    edge_adjacency,
    synthesise_rectangle,
)

PUNJAB_LON, PUNJAB_LAT = 75.85, 30.90


@pytest.fixture(scope="module")
def punjab_square() -> Field_:
    return Field_.from_boundary(
        "Punjab 1 km square",
        synthesise_rectangle(PUNJAB_LON, PUNJAB_LAT, 1000.0, 1000.0),
        region_code="IN-PB",
    )


@pytest.mark.slow
@pytest.mark.parametrize(
    ("requested_m", "expected_blocks", "budget_s"),
    [
        # observed on the reference machine: 0.07 s / 0.28 s / 1.1 s / 4.4 s
        (100.0, 100, 2.0),
        (50.0, 400, 3.0),
        (25.0, 1599, 8.0),
        (12.5, 6241, 30.0),
    ],
)
def test_grid_generation_stays_within_budget(
    punjab_square: Field_, requested_m: float, expected_blocks: int, budget_s: float
) -> None:
    started = time.perf_counter()
    grid = build_grid(
        punjab_square, GridSpec(requested_block_size_m=requested_m, implement_width_m=1.8)
    )
    elapsed = time.perf_counter() - started

    assert grid.block_count == expected_blocks, (
        "block count changed; if this is intentional update the expectation, but check it is "
        "not a tessellation regression first"
    )
    assert elapsed < budget_s, (
        f"grid generation at {requested_m} m took {elapsed:.2f} s against a {budget_s} s "
        "budget. Suspect an accidental per-block reprojection or polygon scan."
    )


@pytest.mark.slow
def test_topology_at_the_default_block_size_is_cheap(punjab_square: Field_) -> None:
    """Adjacency and the distance matrix must not become the bottleneck at 400 blocks."""
    grid = build_grid(
        punjab_square, GridSpec(requested_block_size_m=50.0, implement_width_m=1.8)
    )

    started = time.perf_counter()
    adjacency = edge_adjacency(grid)
    codes, distances = centroid_distance_matrix(grid)
    elapsed = time.perf_counter() - started

    assert len(adjacency) == grid.block_count
    assert distances.shape == (400, 400)
    assert len(codes) == 400
    # Observed well under 0.05 s: index-based adjacency plus a vectorised distance matrix.
    assert elapsed < 1.0, f"topology took {elapsed:.3f} s for 400 blocks"


@pytest.mark.slow
def test_block_count_scales_roughly_as_inverse_square_of_block_size(
    punjab_square: Field_,
) -> None:
    """Sanity check on the sweep's shape: halving the edge should roughly quadruple blocks.

    Guards against a tessellation bug that silently drops or duplicates rows — the kind of
    thing a timing threshold would never notice.
    """
    counts = {}
    for requested in (100.0, 50.0, 25.0):
        grid = build_grid(
            punjab_square,
            GridSpec(requested_block_size_m=requested, implement_width_m=1.8),
        )
        counts[requested] = grid.block_count

    assert 3.5 < counts[50.0] / counts[100.0] < 4.5
    assert 3.5 < counts[25.0] / counts[50.0] < 4.5
