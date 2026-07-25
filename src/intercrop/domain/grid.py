"""The management grid: the optimizer's decision units.

The brief's entity list has ``ManagementBlock`` but no grid that owns it, and that is a
gap worth closing before any solver code exists. A block only means something relative to
the tessellation that produced it — its size, the implement width it was snapped to, the
azimuth it was rotated to, the headland rule applied. A :class:`Plan` computed against a
50.4 m grid is meaningless if someone later regenerates the field at 25 m and the block
codes get reused. So :class:`ManagementGrid` is a versioned, content-hashed entity, and
plan revisions reference it by hash.
"""

from __future__ import annotations

import enum
import hashlib
import json
import math
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator
from shapely.geometry.base import BaseGeometry

from intercrop.domain._types import Fraction, GeoJson, Id, Metres, as_shapely, new_id

GRID_GENERATOR_VERSION = "0.1.0"


class SnapPolicy(enum.StrEnum):
    """How to reconcile a requested block size with the implement working width.

    Block edge length must be a whole multiple of the working width or the grid describes
    passes the grower cannot drive. A grower asking for "50 m blocks" with a 1.8 m implement
    cannot have them: 50 / 1.8 = 27.8. They get 48.6 m (27 passes) or 50.4 m (28 passes).
    Which one is a real decision with real consequences, so it is explicit rather than
    rounded silently.
    """

    NEAREST = "nearest"
    """Minimise |n*w - requested|. Default: closest to what they asked for."""
    DOWN = "down"
    """Never exceed the request. Use when block size is capped by a sprayer boom pass."""
    UP = "up"
    """Never undercut the request. Use when fewer, larger blocks matter for solve time."""


class GridSpec(BaseModel):
    """Everything needed to reproduce a tessellation, and nothing derived from the field.

    Kept separate from :class:`ManagementGrid` so the same spec can be applied to several
    fields and so the block-size sensitivity sweep is a loop over specs.
    """

    model_config = ConfigDict(frozen=True)

    requested_block_size_m: Metres = Field(
        default=50.0,
        description="What the grower or planner asked for, before snapping.",
    )
    implement_width_m: Metres = Field(
        description="Working width of the widest implement that must traverse the block. "
        "Drives both block-edge snapping and headland depth."
    )
    snap_policy: SnapPolicy = SnapPolicy.NEAREST
    headland_multiple: float = Field(
        default=2.0,
        ge=0.0,
        description="Headland depth as a multiple of implement width. 2x is the common "
        "rule of thumb for a turning headland; 0 disables (irrigated beds with external "
        "turning room).",
    )
    min_plantable_fraction: Fraction = Field(
        default=0.25,
        description="Boundary blocks retaining less than this fraction of nominal area "
        "after headland and exclusion subtraction are dropped. Slivers are not farmable "
        "and they distort per-block pressure averages.",
    )
    azimuth_override_deg: float | None = Field(
        default=None,
        ge=0.0,
        lt=180.0,
        description="Force a row direction instead of using the boundary's long axis. "
        "Real growers override this for slope, contour, prevailing wind, or an existing "
        "irrigation layout.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passes_per_block(self) -> int:
        """Whole implement passes across one block edge."""
        ratio = self.requested_block_size_m / self.implement_width_m
        if ratio < 1.0:
            raise ValueError(
                f"requested block size {self.requested_block_size_m} m is narrower than "
                f"the {self.implement_width_m} m implement; a block must fit at least one pass"
            )
        # The tolerance absorbs float noise, so a request that is an exact multiple
        # (50.0 / 2.5) cannot snap to 19 passes because the ratio came out 19.9999...
        low = math.floor(ratio + 1e-9)
        high = math.ceil(ratio - 1e-9)
        match self.snap_policy:
            case SnapPolicy.DOWN:
                return max(low, 1)
            case SnapPolicy.UP:
                return max(high, 1)
            case SnapPolicy.NEAREST:
                if low == high:
                    return max(low, 1)
                return low if (ratio - low) <= (high - ratio) else high

    @computed_field  # type: ignore[prop-decorator]
    @property
    def block_size_m(self) -> float:
        """Snapped block edge length: a whole number of implement passes."""
        return self.passes_per_block * self.implement_width_m

    @computed_field  # type: ignore[prop-decorator]
    @property
    def headland_depth_m(self) -> float:
        return self.headland_multiple * self.implement_width_m

    @property
    def snap_note(self) -> str | None:
        """Grower-readable note when the block size had to move. None if it landed exactly."""
        delta = self.block_size_m - self.requested_block_size_m
        if abs(delta) < 1e-9:
            return None
        return (
            f"Block size adjusted from {self.requested_block_size_m:g} m to "
            f"{self.block_size_m:g} m ({self.passes_per_block} passes at "
            f"{self.implement_width_m:g} m). A {self.requested_block_size_m:g} m block is "
            f"{self.requested_block_size_m / self.implement_width_m:.2f} passes wide, which "
            "would leave a part-width pass in every block."
        )


class RotationEntry(BaseModel):
    """One prior season on one block.

    Carries ``crop_family`` rather than only a variety reference because the rotation
    validators are family-level: the *Verticillium* and *Fusarium* rules are "no Solanaceae
    after Solanaceae", not "no Roma after Roma". A variety-only history cannot express that.
    """

    model_config = ConfigDict(frozen=True)

    season_year: int
    crop_family: str
    crop_name: str | None = None
    variety_id: Id | None = None
    notes: str | None = None


class ManagementBlock(BaseModel):
    """One decision unit. The solver assigns a crop and a row pattern to this and no finer.

    ``nominal_area_m2`` is the untrimmed block; ``plantable_area_m2`` is what survives
    headlands and exclusions. Contracted-area floors must be checked against the plantable
    figure or the plan promises hectares that do not exist.
    """

    model_config = ConfigDict(frozen=True)

    id: Id = Field(default_factory=new_id)
    code: str = Field(description="Stable human-facing label, e.g. 'R03C12'.")
    row_index: int = Field(ge=0, description="Index across the grid's short axis.")
    col_index: int = Field(ge=0, description="Index along the grid's long axis.")
    geometry: GeoJson = Field(description="Plantable footprint, WGS84 lon/lat.")
    centroid_lonlat: tuple[float, float]
    centroid_xy_m: tuple[float, float] = Field(
        description="Centroid in the field's working CRS. This is what the distance-decay "
        "kernel operates on, so it is stored rather than recomputed per evaluation."
    )
    nominal_area_m2: float = Field(gt=0.0)
    plantable_area_m2: float = Field(gt=0.0)
    soil: object | None = Field(
        default=None, description="Per-block soil override; None inherits the field summary."
    )
    rotation_history: tuple[RotationEntry, ...] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def plantable_fraction(self) -> float:
        return self.plantable_area_m2 / self.nominal_area_m2

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_partial(self) -> bool:
        """True when headlands or exclusions trimmed this block."""
        return self.plantable_fraction < 0.999

    @property
    def plantable_area_ha(self) -> float:
        return self.plantable_area_m2 / 10_000.0

    def geometry_m(self, epsg: int) -> BaseGeometry:
        from intercrop.geometry import WGS84_EPSG, reproject

        return reproject(as_shapely(self.geometry), WGS84_EPSG, epsg)


class ManagementGrid(BaseModel):
    """A tessellation of one field into blocks, versioned and content-hashed.

    The hash covers the spec, the frame, and every block's code and plantable area — the
    things a plan's validity depends on. It deliberately excludes ``generated_at`` and the
    UUIDs, so regenerating an unchanged field yields the same hash and existing plan
    revisions stay verifiable.
    """

    model_config = ConfigDict(frozen=True)

    id: Id = Field(default_factory=new_id)
    field_id: Id
    spec: GridSpec
    azimuth_deg: float = Field(
        ge=0.0, lt=180.0, description="Grid long-axis azimuth actually used."
    )
    frame_origin_xy_m: tuple[float, float] = Field(
        description="Rotation centre in the working CRS. Stored so the frame is exactly "
        "reproducible and block indices are stable under later exclusion edits."
    )
    azimuth_source: str = Field(
        description="'boundary_long_axis' or 'grower_override', for the CONFIRM step."
    )
    blocks: tuple[ManagementBlock, ...]
    dropped_sliver_count: int = Field(
        default=0,
        ge=0,
        description="Candidate blocks discarded below min_plantable_fraction. Reported, "
        "never silently swallowed — a high count means the block size fits the field badly.",
    )
    generator_version: str = GRID_GENERATOR_VERSION
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _codes_unique(self) -> ManagementGrid:
        codes = [b.code for b in self.blocks]
        if len(set(codes)) != len(codes):
            raise ValueError("block codes must be unique within a grid")
        return self

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def plantable_area_ha(self) -> float:
        return sum(b.plantable_area_m2 for b in self.blocks) / 10_000.0

    @property
    def nominal_area_ha(self) -> float:
        return sum(b.nominal_area_m2 for b in self.blocks) / 10_000.0

    def by_code(self, code: str) -> ManagementBlock:
        for block in self.blocks:
            if block.code == code:
                return block
        raise KeyError(code)

    def content_hash(self) -> str:
        payload = {
            "generator_version": self.generator_version,
            "spec": self.spec.model_dump(mode="json"),
            "azimuth_deg": round(self.azimuth_deg, 9),
            "frame_origin_xy_m": [round(v, 6) for v in self.frame_origin_xy_m],
            "blocks": [
                {
                    "code": b.code,
                    "row": b.row_index,
                    "col": b.col_index,
                    "plantable_area_m2": round(b.plantable_area_m2, 4),
                    "centroid_xy_m": [round(v, 4) for v in b.centroid_xy_m],
                }
                for b in sorted(self.blocks, key=lambda b: b.code)
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
