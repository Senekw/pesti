"""Shared fixtures.

The two reference fields are the Phase 0 checkpoint cases: the brief's cold-start 1 km square
and an irregular polygon, because a generator that only ever sees a rectangle will encode
rectangle assumptions.
"""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, Polygon

from intercrop.domain.field import Exclusion, ExclusionKind, Field_
from intercrop.domain.grid import GridSpec
from intercrop.geometry import WGS84_EPSG, reproject, utm_epsg_for
from intercrop.grid.generator import synthesise_rectangle

# Ludhiana district, Punjab. Used because the brief's worked example is Punjab tomato.
PUNJAB_LON, PUNJAB_LAT = 75.85, 30.90


@pytest.fixture
def square_km_field() -> Field_:
    """The brief's cold start: 'I have a 1 km by 1 km farm'."""
    boundary = synthesise_rectangle(PUNJAB_LON, PUNJAB_LAT, 1000.0, 1000.0)
    return Field_.from_boundary(
        "Punjab 1 km square",
        boundary,
        region_code="IN-PB",
        boundary_source="grower_stated_dimensions",
    )


@pytest.fixture
def irregular_field() -> Field_:
    """An irregular parcel with a diagonal long axis, built in metres then reprojected.

    Shaped to exercise the parts a square cannot: a non-axis-aligned long axis, a concave
    notch, and boundary blocks that clip to triangles rather than rectangles.
    """
    epsg = utm_epsg_for(PUNJAB_LON, PUNJAB_LAT)
    from shapely.geometry import Point

    origin = reproject(Point(PUNJAB_LON, PUNJAB_LAT), WGS84_EPSG, epsg)
    ox, oy = origin.x, origin.y
    # Elongated, rotated roughly 30 degrees, with a bite out of one side.
    vertices_m = [
        (0.0, 0.0),
        (1200.0, 700.0),
        (1500.0, 1100.0),
        (1100.0, 1400.0),
        (700.0, 1000.0),
        (500.0, 1150.0),
        (150.0, 600.0),
    ]
    polygon = Polygon([(ox + x, oy + y) for x, y in vertices_m])
    boundary = dict(reproject(polygon, epsg, WGS84_EPSG).__geo_interface__)
    return Field_.from_boundary(
        "Irregular parcel",
        boundary,
        region_code="IN-PB",
        boundary_source="uploaded_geojson",
    )


@pytest.fixture
def field_with_exclusions(square_km_field: Field_) -> Field_:
    """The square, crossed by a road and an irrigation main, with a building in a corner."""
    epsg = square_km_field.working_crs_epsg
    boundary_m = square_km_field.boundary_m()
    min_x, min_y, max_x, max_y = boundary_m.bounds

    mid_y = (min_y + max_y) / 2.0
    road_m = LineString([(min_x, mid_y), (max_x, mid_y)])
    main_m = LineString(
        [((min_x + max_x) / 2.0, min_y), ((min_x + max_x) / 2.0, max_y)]
    )
    shed_m = Polygon(
        [
            (min_x + 40, min_y + 40),
            (min_x + 100, min_y + 40),
            (min_x + 100, min_y + 90),
            (min_x + 40, min_y + 90),
        ]
    )

    def to_wgs84(geom: object) -> dict:
        return dict(reproject(geom, epsg, WGS84_EPSG).__geo_interface__)  # type: ignore[arg-type]

    return square_km_field.model_copy(
        update={
            "exclusions": (
                Exclusion(
                    kind=ExclusionKind.ROAD, geometry=to_wgs84(road_m), width_m=6.0,
                    label="farm track",
                ),
                Exclusion(
                    kind=ExclusionKind.IRRIGATION_MAIN,
                    geometry=to_wgs84(main_m),
                    width_m=2.0,
                    standoff_m=1.5,
                    label="buried main with working standoff",
                ),
                Exclusion(
                    kind=ExclusionKind.BUILDING, geometry=to_wgs84(shed_m), label="pump shed"
                ),
            )
        }
    )


@pytest.fixture
def punjab_spec() -> GridSpec:
    """50 m requested, 1.8 m implement — the brief's stated machinery."""
    return GridSpec(requested_block_size_m=50.0, implement_width_m=1.8)
