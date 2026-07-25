"""Field boundary and the things inside it that cannot be planted."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shapely.geometry.base import BaseGeometry

from intercrop.domain._types import GeoJson, Id, Metres, as_shapely, new_id
from intercrop.geometry import WGS84_EPSG, choose_working_crs, reproject


class ExclusionKind(enum.StrEnum):
    ROAD = "road"
    IRRIGATION_MAIN = "irrigation_main"
    DRAINAGE = "drainage"
    BUILDING = "building"
    WATERCOURSE = "watercourse"
    """Often carries a legally mandated no-spray buffer. Width is not cosmetic here."""
    POWER_LINE = "power_line"
    UNPLANTABLE = "unplantable"
    """Rock, salinity scald, permanent wet spot. Grower-declared."""
    OTHER = "other"


class Exclusion(BaseModel):
    """Something inside the boundary that comes out of plantable area.

    Geometry may be a line (a road or pipe centreline, which is how these are actually
    drawn) or a polygon. ``width_m`` buffers a line into a polygon; a polygon may still
    take a buffer where an operational standoff applies — you cannot plant right up to a
    building wall and expect to get a harvester past it.
    """

    model_config = ConfigDict(frozen=True)

    id: Id = Field(default_factory=new_id)
    kind: ExclusionKind
    geometry: GeoJson
    width_m: Metres | None = Field(
        default=None,
        description="Total width for line geometries; buffered half-width each side.",
    )
    standoff_m: float = Field(
        default=0.0,
        ge=0.0,
        description="Extra operational or regulatory buffer beyond the feature itself.",
    )
    label: str | None = None

    @model_validator(mode="after")
    def _lines_need_width(self) -> Exclusion:
        geom = as_shapely(self.geometry)
        if geom.geom_type in ("LineString", "MultiLineString") and self.width_m is None:
            raise ValueError(
                f"{self.kind} is a line geometry and needs width_m; a zero-width road "
                "subtracts no area and would silently overstate the plantable field"
            )
        return self

    def footprint(self, epsg: int) -> BaseGeometry:
        """The area this exclusion actually removes, in the given projected CRS."""
        geom = reproject(as_shapely(self.geometry), WGS84_EPSG, epsg)
        buffer = self.standoff_m
        if self.width_m is not None:
            buffer += self.width_m / 2.0
        return geom.buffer(buffer) if buffer > 0 else geom


class SoilSummary(BaseModel):
    """Coarse soil description. Deliberately shallow at Phase 0.

    Enough to drive the N budget and irrigation-suitability validators in Phase 1 without
    pretending we have a soil survey we have not ingested.
    """

    model_config = ConfigDict(frozen=True)

    texture: str | None = Field(default=None, description="e.g. 'sandy loam'")
    ph: float | None = Field(default=None, ge=2.0, le=11.0)
    organic_matter_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    drainage_class: str | None = None


class Field_(BaseModel):
    """A single contiguous parcel with one boundary and one grid.

    Named ``Field_`` in code because ``pydantic.Field`` owns the short name; exported as
    ``Field`` is deliberately *not* done, to keep the collision from biting downstream.
    """

    model_config = ConfigDict(frozen=True)

    id: Id = Field(default_factory=new_id)
    name: str
    boundary: GeoJson = Field(description="Field boundary, WGS84 lon/lat.")
    exclusions: tuple[Exclusion, ...] = ()
    working_crs_epsg: int = Field(
        description="Projected metric CRS for this field. Stored, never re-derived, so "
        "distances stay stable across solves."
    )
    crs_rationale: str
    crs_warnings: tuple[str, ...] = ()
    region_code: str | None = Field(
        default=None,
        description="ISO 3166-2 where known, e.g. 'IN-PB'. Keys climate normals, pest "
        "complex, and parameter validity checks.",
    )
    soil: SoilSummary | None = None
    boundary_source: str = Field(
        default="grower_stated_dimensions",
        description="How the boundary was obtained: uploaded file, drawn, or synthesised "
        "from stated dimensions. A synthesised square must never be presented as surveyed.",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_boundary(
        cls,
        name: str,
        boundary: GeoJson,
        *,
        exclusions: tuple[Exclusion, ...] = (),
        region_code: str | None = None,
        soil: SoilSummary | None = None,
        boundary_source: str = "grower_stated_dimensions",
    ) -> Field_:
        """Build a field, choosing and recording the working CRS from the boundary."""
        choice = choose_working_crs(as_shapely(boundary))
        return cls(
            name=name,
            boundary=boundary,
            exclusions=exclusions,
            working_crs_epsg=choice.epsg,
            crs_rationale=choice.rationale,
            crs_warnings=choice.warnings,
            region_code=region_code,
            soil=soil,
            boundary_source=boundary_source,
        )

    def boundary_m(self) -> BaseGeometry:
        """Boundary in the working CRS, metres."""
        return reproject(as_shapely(self.boundary), WGS84_EPSG, self.working_crs_epsg)

    def exclusion_footprint_m(self) -> BaseGeometry | None:
        """Union of all exclusion footprints in the working CRS, or None if there are none."""
        if not self.exclusions:
            return None
        from shapely.ops import unary_union

        return unary_union([e.footprint(self.working_crs_epsg) for e in self.exclusions])

    @property
    def gross_area_ha(self) -> float:
        """Area inside the boundary, before headlands or exclusions."""
        return self.boundary_m().area / 10_000.0
