"""Coordinate handling: CRS choice, metric fidelity, and the grid frame round-trip."""

from __future__ import annotations

import math

import pytest
from shapely.geometry import Point, Polygon

from intercrop.geometry import (
    WGS84_EPSG,
    GridFrame,
    choose_working_crs,
    geojson_to_shapely,
    long_axis,
    reproject,
    utm_epsg_for,
)
from intercrop.grid.generator import synthesise_rectangle

PUNJAB_LON, PUNJAB_LAT = 75.85, 30.90


def test_utm_zone_selection() -> None:
    assert utm_epsg_for(PUNJAB_LON, PUNJAB_LAT) == 32643  # zone 43N
    assert utm_epsg_for(-1.0, 51.5) == 32630  # zone 30N, UK
    assert utm_epsg_for(-47.9, -15.8) == 32723  # zone 23S, Brasilia
    assert utm_epsg_for(-179.0, 0.0) == 32601
    assert utm_epsg_for(179.0, 0.0) == 32660


def test_invalid_lonlat_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a valid lon/lat"):
        utm_epsg_for(200.0, 0.0)


def test_working_crs_is_chosen_and_justified() -> None:
    boundary = geojson_to_shapely(
        synthesise_rectangle(PUNJAB_LON, PUNJAB_LAT, 1000.0, 1000.0)
    )
    choice = choose_working_crs(boundary)
    assert choice.epsg == 32643
    assert "UTM zone 43N" in choice.rationale
    assert choice.warnings == ()


def test_zone_straddling_boundary_is_warned_about_not_silently_projected() -> None:
    """A holding wide enough to cross a UTM zone gets told, rather than quietly distorted."""
    wide = Polygon([(73.0, 30.0), (79.0, 30.0), (79.0, 31.0), (73.0, 31.0)])
    choice = choose_working_crs(wide)
    assert any("more than one UTM zone" in w for w in choice.warnings)


def test_extreme_latitude_is_warned_about() -> None:
    polar = Polygon([(10.0, 86.0), (11.0, 86.0), (11.0, 86.5), (10.0, 86.5)])
    assert any("beyond UTM" in w for w in choose_working_crs(polar).warnings)


# --------------------------------------------------------------------------------------
# Metric fidelity: the reason the working CRS exists
# --------------------------------------------------------------------------------------


def test_synthesised_square_really_is_square_on_the_ground() -> None:
    """A 1 km x 1 km field built in degrees would be 27% out of square at this latitude."""
    boundary = synthesise_rectangle(PUNJAB_LON, PUNJAB_LAT, 1000.0, 1000.0)
    in_metres = reproject(geojson_to_shapely(boundary), WGS84_EPSG, 32643)

    assert in_metres.area == pytest.approx(1_000_000.0, rel=1e-6)
    axis = long_axis(in_metres)
    assert axis.long_side_m == pytest.approx(1000.0, rel=1e-6)
    assert axis.short_side_m == pytest.approx(1000.0, rel=1e-6)


def test_a_degree_box_is_not_square_which_is_the_whole_point() -> None:
    """Documents the error being avoided: equal degree extents are unequal ground distances."""
    lon_span = Point(75.85, 30.90).distance(Point(75.86, 30.90))
    assert lon_span == pytest.approx(0.01)  # equal in degrees

    a = reproject(Point(75.85, 30.90), WGS84_EPSG, 32643)
    b = reproject(Point(75.86, 30.90), WGS84_EPSG, 32643)
    c = reproject(Point(75.85, 30.91), WGS84_EPSG, 32643)
    east_west_m = a.distance(b)
    north_south_m = a.distance(c)
    # About 956 m versus 1106 m: a 15% discrepancy for the same degree extent.
    assert north_south_m / east_west_m > 1.1


def test_synthesised_rectangle_honours_its_dimensions() -> None:
    boundary = synthesise_rectangle(PUNJAB_LON, PUNJAB_LAT, 1500.0, 600.0)
    in_metres = reproject(geojson_to_shapely(boundary), WGS84_EPSG, 32643)
    axis = long_axis(in_metres)
    assert axis.long_side_m == pytest.approx(1500.0, rel=1e-5)
    assert axis.short_side_m == pytest.approx(600.0, rel=1e-5)
    assert not axis.is_ambiguous


def test_synthesised_rectangle_can_be_rotated() -> None:
    boundary = synthesise_rectangle(PUNJAB_LON, PUNJAB_LAT, 1500.0, 600.0, azimuth_deg=30.0)
    in_metres = reproject(geojson_to_shapely(boundary), WGS84_EPSG, 32643)
    assert long_axis(in_metres).azimuth_deg == pytest.approx(30.0, abs=0.5)


# --------------------------------------------------------------------------------------
# Long axis
# --------------------------------------------------------------------------------------


def test_square_long_axis_is_flagged_ambiguous() -> None:
    square = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    assert long_axis(square).is_ambiguous


def test_elongated_long_axis_is_not_ambiguous_and_points_the_right_way() -> None:
    horizontal = Polygon([(0, 0), (500, 0), (500, 100), (0, 100)])
    axis = long_axis(horizontal)
    assert not axis.is_ambiguous
    assert axis.azimuth_deg == pytest.approx(0.0, abs=1e-6)
    assert axis.long_side_m == pytest.approx(500.0)

    diagonal = Polygon([(0, 0), (300, 300), (280, 320), (-20, 20)])
    assert long_axis(diagonal).azimuth_deg == pytest.approx(45.0, abs=1.0)


def test_azimuth_is_normalised_below_180() -> None:
    """A grid axis has no direction; 179.9999 and 0 are the same grid."""
    for angle in (0.0, 45.0, 90.0, 135.0, 179.5):
        polygon = Polygon([(0, 0), (500, 0), (500, 100), (0, 100)])
        from shapely import affinity

        rotated = affinity.rotate(polygon, angle, origin=(0, 0))
        result = long_axis(rotated).azimuth_deg
        assert 0.0 <= result < 180.0


def test_degenerate_geometry_cannot_have_a_long_axis() -> None:
    from shapely.geometry import LineString

    with pytest.raises(ValueError, match="no area"):
        long_axis(LineString([(0, 0), (100, 0)]))


# --------------------------------------------------------------------------------------
# Grid frame
# --------------------------------------------------------------------------------------


def test_grid_frame_round_trip_is_lossless() -> None:
    polygon = Polygon([(10, 20), (300, 180), (280, 260), (-10, 90)])
    frame = GridFrame(azimuth_deg=32.5, origin=(10.0, 20.0))
    restored = frame.to_world(frame.to_grid(polygon))
    assert restored.equals_exact(polygon, tolerance=1e-9) or restored.symmetric_difference(
        polygon
    ).area < 1e-9


def test_grid_frame_aligns_the_long_axis_to_x() -> None:
    polygon = Polygon([(0, 0), (500, 289), (475, 332), (-25, 43)])
    frame = GridFrame.from_boundary(polygon)
    rotated = frame.to_grid(polygon)
    assert long_axis(rotated).azimuth_deg == pytest.approx(0.0, abs=0.5)


def test_grid_frame_preserves_area() -> None:
    polygon = Polygon([(0, 0), (500, 289), (475, 332), (-25, 43)])
    frame = GridFrame(azimuth_deg=41.2, origin=(0.0, 0.0))
    assert frame.to_grid(polygon).area == pytest.approx(polygon.area, rel=1e-12)


def test_frame_origin_is_stored_not_derived() -> None:
    """Deriving the rotation centre from a centroid would renumber every block when an
    exclusion is edited."""
    polygon = Polygon([(0, 0), (500, 0), (500, 100), (0, 100)])
    frame = GridFrame.from_boundary(polygon)
    assert frame.origin == (0.0, 0.0)  # min corner, not the centroid at (250, 50)


# --------------------------------------------------------------------------------------
# GeoJSON parsing
# --------------------------------------------------------------------------------------


def test_accepts_geometry_feature_and_single_feature_collection() -> None:
    geometry = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
    }
    bare = geojson_to_shapely(geometry)
    feature = geojson_to_shapely({"type": "Feature", "properties": {}, "geometry": geometry})
    collection = geojson_to_shapely(
        {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {}, "geometry": geometry}],
        }
    )
    assert bare.area == feature.area == collection.area


def test_multi_feature_collection_is_rejected_with_guidance() -> None:
    geometry = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
    }
    feature = {"type": "Feature", "properties": {}, "geometry": geometry}
    with pytest.raises(ValueError, match="separate fields"):
        geojson_to_shapely({"type": "FeatureCollection", "features": [feature, feature]})


def test_self_intersecting_boundary_is_rejected() -> None:
    """An invalid ring would otherwise poison every area sum downstream."""
    from intercrop.domain.field import Field_

    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]],
    }
    with pytest.raises(ValueError, match="invalid"):
        Field_.from_boundary("bowtie", bowtie)


def test_field_records_its_crs_choice_and_boundary_source() -> None:
    from intercrop.domain.field import Field_

    boundary = synthesise_rectangle(PUNJAB_LON, PUNJAB_LAT, 1000.0, 1000.0)
    field = Field_.from_boundary("test", boundary, region_code="IN-PB")
    assert field.working_crs_epsg == 32643
    assert "EPSG:32643" in field.crs_rationale
    # A shape derived from stated dimensions must never look surveyed.
    assert field.boundary_source == "grower_stated_dimensions"
    assert field.gross_area_ha == pytest.approx(100.0, rel=1e-5)


def test_exclusion_line_is_buffered_by_half_width_plus_standoff() -> None:
    from intercrop.domain.field import Exclusion, ExclusionKind

    start = reproject(Point(PUNJAB_LON, PUNJAB_LAT), WGS84_EPSG, 32643)
    end = Point(start.x + 1000.0, start.y)
    from shapely.geometry import LineString

    line_m = LineString([(start.x, start.y), (end.x, end.y)])
    line_wgs84 = dict(reproject(line_m, 32643, WGS84_EPSG).__geo_interface__)

    exclusion = Exclusion(
        kind=ExclusionKind.IRRIGATION_MAIN, geometry=line_wgs84, width_m=2.0, standoff_m=1.5
    )
    footprint = exclusion.footprint(32643)
    # 1000 m long, buffered 2.5 m each side, plus semicircular caps.
    expected = 1000.0 * 5.0 + math.pi * 2.5**2
    assert footprint.area == pytest.approx(expected, rel=0.01)
