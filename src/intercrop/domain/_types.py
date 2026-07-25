"""Shared annotated scalars and the GeoJSON field type."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from pydantic import AfterValidator, Field, PlainSerializer
from shapely.geometry.base import BaseGeometry

from intercrop.geometry import geojson_to_shapely


def _valid_geojson(value: dict[str, Any]) -> dict[str, Any]:
    geom = geojson_to_shapely(value)
    if geom.is_empty:
        raise ValueError("geometry is empty")
    if not geom.is_valid:
        raise ValueError(
            "geometry is invalid (self-intersecting or malformed ring). Run it through "
            "shapely.make_valid before storing rather than letting it poison area sums."
        )
    return value


GeoJson = Annotated[dict[str, Any], AfterValidator(_valid_geojson)]
"""A GeoJSON geometry mapping. Always WGS84 lon/lat — see :mod:`intercrop.geometry`."""

Metres = Annotated[float, Field(gt=0.0)]
Hectares = Annotated[float, Field(ge=0.0)]
Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
CelsiusDay = Annotated[float, Field(ge=0.0)]
"""Accumulated growing degree days."""

Id = Annotated[
    uuid.UUID,
    PlainSerializer(str, return_type=str, when_used="json"),
]


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def as_shapely(value: dict[str, Any]) -> BaseGeometry:
    return geojson_to_shapely(value)
