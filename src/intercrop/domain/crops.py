"""Crops, varieties, planting calendars, and within-block row patterns."""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from intercrop.domain._types import Fraction, Id, Metres, new_id


class CropRole(enum.StrEnum):
    """Why a crop is in the layout. Drives which constraints and objectives apply to it."""

    MAIN = "main"
    """Carries the yield or contract obligation."""
    COMPANION = "companion"
    """Grown for an interaction effect (repellency, masking), interplanted with the main crop."""
    TRAP = "trap"
    """Grown to draw a pest away from the main crop. Usually perimeter, often destroyed
    rather than harvested — so it must not be counted toward saleable yield."""
    INSECTARY = "insectary"
    """Floral resource for natural enemies. A different mechanism from repellency, with a
    different spatial range, and kept separate throughout."""
    COVER = "cover"
    """Soil or rotation function, no direct pest role."""


class Crop(BaseModel):
    """A species.

    Interaction coefficients and rotation rules live at this level, not at variety level.
    ``family`` is load-bearing: the *Verticillium*/*Fusarium* rotation rule is
    "no Solanaceae after Solanaceae", which a variety identifier alone cannot express.
    """

    model_config = ConfigDict(frozen=True)

    id: Id = Field(default_factory=new_id)
    slug: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    common_name: str
    scientific_name: str
    family: str = Field(description="Botanical family, e.g. 'Solanaceae', 'Amaryllidaceae'.")
    is_legume: bool = Field(
        default=False,
        description="Legumes fix N (changes the N budget) and are suppressed by allium "
        "allelopathy (changes what may be adjacent).",
    )
    n_fixation_kg_ha: float | None = Field(
        default=None, ge=0.0, description="Only meaningful for legumes."
    )


class PlantingWindow(BaseModel):
    """When a variety can go in the ground, in one region.

    Modelled as a value object owned by a variety rather than a standalone entity, which
    is a deliberate departure from the brief's entity list: a window has no identity of its
    own and is never referenced independently. It is always "this variety, in this region".

    Day-of-year rather than dates so it survives across seasons. Autumn-planted crops wrap
    past the new year, which is why ``wraps_year_end`` exists instead of assuming
    ``earliest <= latest``.
    """

    model_config = ConfigDict(frozen=True)

    region_code: str
    earliest_doy: int = Field(ge=1, le=366)
    latest_doy: int = Field(ge=1, le=366)
    gdd_to_maturity: float | None = Field(default=None, ge=0.0)
    days_to_harvest: int | None = Field(default=None, gt=0)
    requires_frost_free: bool = Field(
        default=False,
        description="True for tomato: planting is gated on last-frost date, not calendar.",
    )
    parameter_key: str | None = Field(
        default=None,
        description="Parameter-set key this window came from, so a plan can trace the date "
        "back to an extension calendar rather than asserting it.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def wraps_year_end(self) -> bool:
        return self.latest_doy < self.earliest_doy

    def contains(self, doy: int) -> bool:
        if self.wraps_year_end:
            return doy >= self.earliest_doy or doy <= self.latest_doy
        return self.earliest_doy <= doy <= self.latest_doy


class CropVariety(BaseModel):
    """A named cultivar with its own maturity and spacing requirements."""

    model_config = ConfigDict(frozen=True)

    id: Id = Field(default_factory=new_id)
    crop_id: Id
    name: str
    row_spacing_m: Metres
    in_row_spacing_m: Metres
    planting_windows: tuple[PlantingWindow, ...] = ()
    base_temp_c: float | None = Field(
        default=None,
        description="Degree-day base for this crop's development. Distinct from the pest's "
        "base temperature — conflating them is a classic phenology bug.",
    )
    expected_yield_t_ha: float | None = Field(default=None, ge=0.0)
    parameter_key_prefix: str | None = None

    def window_for(self, region_code: str) -> PlantingWindow | None:
        for window in self.planting_windows:
            if window.region_code == region_code:
                return window
        return None

    @property
    def plants_per_ha(self) -> float:
        return 10_000.0 / (self.row_spacing_m * self.in_row_spacing_m)


class PatternGeometry(enum.StrEnum):
    SOLID = "solid"
    """One crop, full block."""
    ALTERNATING_BANDS = "alternating_bands"
    """Repeating bands, e.g. 4 tomato rows : 2 garlic rows across the block."""
    BORDER_ONLY = "border_only"
    """Companion confined to a strip around the block perimeter."""
    TRAP_PERIMETER = "trap_perimeter"
    """Like border, but the strip crop is a trap crop, not harvested."""
    INSECTARY_STRIP = "insectary_strip"
    """A single floral strip, typically one edge, sized for natural-enemy provisioning."""


class PatternComponent(BaseModel):
    """One crop's share of a row pattern."""

    model_config = ConfigDict(frozen=True)

    crop_slug: str
    role: CropRole
    n_rows: int = Field(gt=0, description="Rows of this component per repeating band.")
    row_spacing_m: Metres
    in_row_spacing_m: Metres

    @computed_field  # type: ignore[prop-decorator]
    @property
    def band_width_m(self) -> float:
        return self.n_rows * self.row_spacing_m


class RowPatternTemplate(BaseModel):
    """A within-block planting pattern, chosen per block by the solver.

    This is where the brief's "never solve row-by-row" rule is made structural: the solver
    picks a template per block and the template carries enough geometry for the pressure
    model to reason about *intra*-block distance, without any row ever becoming a decision
    variable.

    Storing only a ratio like "4:2" would be a mistake. The repellency effect decays with
    distance, so the pressure model needs to know how far a tomato row actually sits from
    the nearest garlic row. A 4:2 pattern at 1.5 m tomato spacing puts the worst-off tomato
    row 3 m from garlic; at 0.9 m spacing it is 1.8 m. Same ratio, materially different
    protection.
    """

    model_config = ConfigDict(frozen=True)

    id: Id = Field(default_factory=new_id)
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9_:]*$")
    name: str
    geometry: PatternGeometry
    components: tuple[PatternComponent, ...] = Field(min_length=1)
    border_depth_m: float | None = Field(
        default=None,
        gt=0.0,
        description="Strip depth for BORDER_ONLY / TRAP_PERIMETER / INSECTARY_STRIP.",
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _geometry_matches_components(self) -> RowPatternTemplate:
        strip_kinds = {
            PatternGeometry.BORDER_ONLY,
            PatternGeometry.TRAP_PERIMETER,
            PatternGeometry.INSECTARY_STRIP,
        }
        if self.geometry is PatternGeometry.SOLID and len(self.components) != 1:
            raise ValueError("SOLID pattern must have exactly one component")
        if self.geometry is PatternGeometry.ALTERNATING_BANDS and len(self.components) < 2:
            raise ValueError("ALTERNATING_BANDS needs at least two components to alternate")
        if self.geometry in strip_kinds and self.border_depth_m is None:
            raise ValueError(f"{self.geometry} requires border_depth_m")
        if self.geometry in strip_kinds and len(self.components) != 2:
            raise ValueError(
                f"{self.geometry} expects exactly two components: the interior crop and "
                "the strip crop"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def repeat_width_m(self) -> float:
        """Width of one full repeating band unit."""
        return sum(c.band_width_m for c in self.components)

    def area_fraction(self, block_size_m: float) -> dict[str, float]:
        """Fraction of block area held by each component crop.

        Band patterns are proportional to band width. Strip patterns depend on block size,
        because a 3 m border round a 50 m block is 22% of it and round a 100 m block is 12%
        — a template's economics are not scale-free, which is exactly why block size and
        template choice have to be solved together.
        """
        if self.geometry is PatternGeometry.SOLID:
            return {self.components[0].crop_slug: 1.0}

        if self.geometry is PatternGeometry.ALTERNATING_BANDS:
            total = self.repeat_width_m
            return {c.crop_slug: c.band_width_m / total for c in self.components}

        depth = self.border_depth_m or 0.0
        if depth * 2 >= block_size_m:
            raise ValueError(
                f"border depth {depth} m cannot fit twice inside a {block_size_m} m block"
            )
        interior, strip = self.components[0], self.components[1]
        if self.geometry is PatternGeometry.INSECTARY_STRIP:
            strip_area = depth * block_size_m  # one edge only
        else:
            interior_side = block_size_m - 2 * depth
            strip_area = block_size_m**2 - interior_side**2
        strip_fraction = strip_area / block_size_m**2
        return {interior.crop_slug: 1.0 - strip_fraction, strip.crop_slug: strip_fraction}

    def mean_distance_to_component_m(self, from_slug: str, to_slug: str) -> float:
        """Mean distance from a plant of ``from_slug`` to the nearest ``to_slug`` row.

        This is the scalar the intra-block term of the distance-decay kernel consumes.

        For alternating bands the closed form is band_width/4: a band of width *W* flanked
        on both sides by the other component has mean nearest-edge distance *W*/4, since a
        point uniformly placed in the band is on average *W*/4 from whichever side is closer.
        Along-row variation is ignored, which is sound because bands run the full block
        length — 50 m of run against a few metres of width.

        For strip patterns the interior mean distance is derived from the interior square's
        mean distance to its own boundary, which for a square of side *s* is *s*/6.

        Phase 1 must not treat this as the whole story: the *inter*-block term, where a
        block of solid tomato is protected by garlic in a neighbouring block, is a separate
        contribution computed from block centroid distances.
        """
        by_slug = {c.crop_slug: c for c in self.components}
        if from_slug not in by_slug:
            raise KeyError(f"{from_slug!r} is not in pattern {self.code!r}")
        if to_slug not in by_slug:
            return float("inf")
        if from_slug == to_slug:
            return 0.0

        if self.geometry is PatternGeometry.SOLID:
            return float("inf")
        if self.geometry is PatternGeometry.ALTERNATING_BANDS:
            return by_slug[from_slug].band_width_m / 4.0
        raise NotImplementedError(
            "strip-pattern mean distance depends on block size; call "
            "mean_distance_to_component_in_block_m instead"
        )

    def mean_distance_to_component_in_block_m(
        self, from_slug: str, to_slug: str, block_size_m: float
    ) -> float:
        """Block-size-aware variant, required for the strip geometries."""
        strip_kinds = {
            PatternGeometry.BORDER_ONLY,
            PatternGeometry.TRAP_PERIMETER,
            PatternGeometry.INSECTARY_STRIP,
        }
        if self.geometry not in strip_kinds:
            return self.mean_distance_to_component_m(from_slug, to_slug)

        by_slug = {c.crop_slug: c for c in self.components}
        if to_slug not in by_slug:
            return float("inf")
        if from_slug == to_slug:
            return 0.0
        interior_slug = self.components[0].crop_slug
        depth = self.border_depth_m or 0.0
        if from_slug == interior_slug:
            interior_side = block_size_m - 2 * depth
            if self.geometry is PatternGeometry.INSECTARY_STRIP:
                # Strip on one edge: mean perpendicular distance across the interior.
                return (block_size_m - depth) / 2.0
            return interior_side / 6.0
        return depth / 4.0

    @property
    def main_crop_slugs(self) -> tuple[str, ...]:
        return tuple(c.crop_slug for c in self.components if c.role is CropRole.MAIN)

    @property
    def yields_saleable(self) -> Fraction:
        """Fraction of the pattern's area producing a saleable crop.

        Trap crops are frequently destroyed at peak infestation, so counting their area
        toward a contracted-area floor would let the optimizer satisfy a tomato contract
        with a crop nobody buys.
        """
        non_saleable = {CropRole.TRAP, CropRole.INSECTARY, CropRole.COVER}
        total = self.repeat_width_m
        saleable = sum(c.band_width_m for c in self.components if c.role not in non_saleable)
        return saleable / total if total else 0.0
