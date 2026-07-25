"""Coordinate handling and the oriented grid frame.

The entity list in the brief does not mention coordinate reference systems, and it needs
to. Everything the planner computes is metric — block edges snapped to implement width,
headland buffers, hectares, and above all the distance-decay kernel that makes the
repellency model spatial rather than binary. None of that can be done in degrees: at
Punjab's latitude a degree of longitude is about 87 km and a degree of latitude about
111 km, so an axis-aligned "square" in lon/lat is a 27% -distorted rectangle on the ground.

So there are exactly three frames, and each one has one job:

WGS84 (EPSG:4326), lon/lat degrees
    Interchange only. GeoJSON in, GeoJSON out. Nothing is ever measured here.

Working CRS, metres
    Per-field projected CRS, chosen once from the boundary centroid and then stored on the
    :class:`~intercrop.domain.field.Field`. Every area, distance and buffer is computed
    here. Storing it rather than re-deriving it matters for reproducibility: a field that
    straddles a UTM zone boundary must not silently change projection between solves.

Grid frame, metres
    The working CRS rotated so +x runs along the field's long axis. Tessellation happens
    axis-aligned here and blocks are rotated back on the way out. This is what makes the
    grid describe passes a grower can actually drive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from pyproj import Transformer
from shapely import affinity
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry

WGS84_EPSG = 4326


def utm_epsg_for(lon: float, lat: float) -> int:
    """Pick the UTM zone containing ``(lon, lat)``.

    UTM is the pragmatic choice for a single farm: distortion is well under 1 m/km inside a
    zone, and metres are metres. It breaks down above ~84 degrees and for holdings wide
    enough to span zones — both are flagged rather than silently handled, see
    :func:`choose_working_crs`.
    """
    if not -180.0 <= lon <= 180.0 or not -90.0 <= lat <= 90.0:
        raise ValueError(f"({lon}, {lat}) is not a valid lon/lat pair")
    zone = math.floor((lon + 180.0) / 6.0) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


@dataclass(frozen=True)
class CrsChoice:
    epsg: int
    rationale: str
    warnings: tuple[str, ...] = ()


def choose_working_crs(boundary_wgs84: BaseGeometry) -> CrsChoice:
    """Choose and justify the metric CRS for a field boundary given in lon/lat."""
    centroid = boundary_wgs84.centroid
    lon, lat = float(centroid.x), float(centroid.y)
    epsg = utm_epsg_for(lon, lat)
    warnings: list[str] = []

    min_lon, _min_lat, max_lon, _max_lat = boundary_wgs84.bounds
    if utm_epsg_for(min_lon, lat) != utm_epsg_for(max_lon, lat):
        warnings.append(
            "Boundary spans more than one UTM zone. Distances near the far edge carry "
            "extra scale error; a local azimuthal-equidistant projection would be better "
            "and is not implemented yet."
        )
    if abs(lat) > 84.0:
        warnings.append("Latitude beyond UTM's valid band (84 degrees). Results unreliable.")

    return CrsChoice(
        epsg=epsg,
        rationale=(
            f"UTM zone {epsg - (32600 if lat >= 0 else 32700)}"
            f"{'N' if lat >= 0 else 'S'} (EPSG:{epsg}), from boundary centroid "
            f"({lon:.4f}, {lat:.4f})"
        ),
        warnings=tuple(warnings),
    )


def reproject(geom: BaseGeometry, from_epsg: int, to_epsg: int) -> BaseGeometry:
    """Reproject a shapely geometry between EPSG codes."""
    if from_epsg == to_epsg:
        return geom
    transformer = Transformer.from_crs(from_epsg, to_epsg, always_xy=True)
    from shapely.ops import transform as shapely_transform

    return shapely_transform(transformer.transform, geom)


def geojson_to_shapely(obj: dict[str, Any]) -> BaseGeometry:
    """Accept a GeoJSON geometry, Feature, or single-feature FeatureCollection."""
    kind = obj.get("type")
    if kind == "FeatureCollection":
        features = obj.get("features", [])
        if len(features) != 1:
            raise ValueError(
                f"expected exactly one feature for a field boundary, got {len(features)}. "
                "Multiple parcels should be uploaded as separate fields so each gets its "
                "own grid and headlands."
            )
        return geojson_to_shapely(features[0])
    if kind == "Feature":
        geometry = obj.get("geometry")
        if geometry is None:
            raise ValueError("GeoJSON Feature has no geometry")
        return shape(geometry)
    return shape(obj)


def shapely_to_geojson(geom: BaseGeometry) -> dict[str, Any]:
    return dict(mapping(geom))


# --------------------------------------------------------------------------------------
# Oriented grid frame
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LongAxis:
    azimuth_deg: float
    long_side_m: float
    short_side_m: float
    is_ambiguous: bool
    """True when the field is close enough to square that the long axis is arbitrary.

    This matters and is easy to miss. On the brief's own 1 km x 1 km cold-start case every
    side is the same length, so whichever edge ``minimum_rotated_rectangle`` happens to
    return sets the row direction for a 100 ha block — and the grower gets no say. When this
    is True the ``CONFIRM`` turn should ask for a row direction rather than silently
    committing to one, because on a square field the choice belongs to whoever knows the
    slope and the prevailing wind.
    """


def long_axis(geom: BaseGeometry, squareness_tolerance: float = 0.02) -> LongAxis:
    """Orientation of the longest edge of the minimum rotated rectangle.

    Azimuth is normalised to [0, 180) because a grid axis has no direction — running rows
    east-west and west-east give the same grid.
    """
    mrr = geom.minimum_rotated_rectangle
    coords = list(mrr.exterior.coords)[:-1] if hasattr(mrr, "exterior") else []
    if len(coords) < 2:
        raise ValueError(
            "cannot determine a long axis: boundary has no area (degenerate or a line)"
        )

    edges = [
        (math.hypot(x1 - x0, y1 - y0), math.degrees(math.atan2(y1 - y0, x1 - x0)))
        for (x0, y0), (x1, y1) in zip(coords, coords[1:] + coords[:1], strict=True)
    ]
    long_len, angle = max(edges, key=lambda e: e[0])
    short_len = min(length for length, _ in edges)

    # Wrap into [0, 180). The explicit near-180 case exists because a reprojection
    # round-trip leaves a square's edge at 179.9999..., which fmod leaves just under the
    # limit rather than snapping to the 0 it is equivalent to.
    azimuth = angle % 180.0
    if azimuth >= 180.0 - 1e-6:
        azimuth = 0.0

    ambiguous = long_len > 0 and (long_len - short_len) / long_len < squareness_tolerance
    return LongAxis(
        azimuth_deg=azimuth,
        long_side_m=long_len,
        short_side_m=short_len,
        is_ambiguous=ambiguous,
    )


def long_axis_azimuth_deg(geom: BaseGeometry) -> float:
    """Convenience wrapper returning just the azimuth."""
    return long_axis(geom).azimuth_deg


@dataclass(frozen=True)
class GridFrame:
    """Rotation between the working CRS and the axis-aligned tessellation frame.

    ``origin`` is held in working-CRS coordinates and rotation is always taken about it, so
    the frame is fully reproducible from two stored numbers. Re-deriving the rotation
    centre from a geometry's centroid would move the whole grid the moment an exclusion is
    edited, silently renumbering every block in an existing plan.
    """

    azimuth_deg: float
    origin: tuple[float, float]

    @classmethod
    def from_boundary(cls, boundary_m: BaseGeometry) -> GridFrame:
        azimuth = long_axis_azimuth_deg(boundary_m)
        min_x, min_y, _, _ = boundary_m.bounds
        return cls(azimuth_deg=azimuth, origin=(float(min_x), float(min_y)))

    def to_grid(self, geom: BaseGeometry) -> BaseGeometry:
        """Working CRS -> grid frame (long axis becomes +x)."""
        return affinity.rotate(geom, -self.azimuth_deg, origin=self.origin, use_radians=False)

    def to_world(self, geom: BaseGeometry) -> BaseGeometry:
        """Grid frame -> working CRS."""
        return affinity.rotate(geom, self.azimuth_deg, origin=self.origin, use_radians=False)
