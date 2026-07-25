"""``FarmSpec``: the structured product of the intake conversation.

Every value the grower did not personally state is wrapped in
:class:`~intercrop.provenance.Sourced`, which is what makes the ``CONFIRM`` state
implementable rather than aspirational — the spec can enumerate its own assumptions instead
of relying on the agent to remember which ones it made.

``CLARIFY`` is likewise driven off the model: :meth:`FarmSpec.missing_required` reports what
is genuinely absent, and the ranking metadata in :data:`OBJECTIVE_SENSITIVITY` says which
gaps are worth a question. Phase 3 replaces those hand-set ranks with a real sensitivity
sweep; they are marked as placeholders so nobody mistakes them for measured values.
"""

from __future__ import annotations

import enum
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from intercrop.domain._types import GeoJson, Hectares, Id, Metres, new_id
from intercrop.domain.crops import CropRole
from intercrop.provenance import Provenance, Sourced


class UserRole(enum.StrEnum):
    """Who we are talking to. Changes tone, defaults, and how much is shown.

    Flagged in the brief as something to ask rather than assume. A researcher wants the
    Pareto front and the parameter provenance; a grower wants the map and the dates.
    """

    GROWER = "grower"
    AGRONOMIST = "agronomist"
    RESEARCHER = "researcher"
    UNKNOWN = "unknown"


class IrrigationType(enum.StrEnum):
    """Constrains layout heavily, which is why the brief lists it as ask-first.

    Drip fixes lateral positions, so companion bands must align to existing laterals and a
    border strip may have no water at all. Flood needs contiguous basins of uniform crop and
    effectively forbids fine interleaving — a 4:2 band pattern with two crops of different
    water demand in one basin is not irrigable.
    """

    DRIP = "drip"
    FURROW = "furrow"
    FLOOD_BASIN = "flood_basin"
    SPRINKLER = "sprinkler"
    RAINFED = "rainfed"
    UNKNOWN = "unknown"


class CertificationStatus(enum.StrEnum):
    """Determines which interventions are legal, so it cannot be defaulted quietly."""

    CONVENTIONAL = "conventional"
    ORGANIC_CERTIFIED = "organic_certified"
    IN_CONVERSION = "in_conversion"
    UNKNOWN = "unknown"


class Location(BaseModel):
    model_config = ConfigDict(frozen=True)

    lon: float = Field(ge=-180.0, le=180.0)
    lat: float = Field(ge=-90.0, le=90.0)
    region_code: str | None = Field(default=None, description="ISO 3166-2, e.g. 'IN-PB'.")
    place_name: str | None = None
    precision: Literal["exact", "district", "region", "country"] = "region"
    """How precisely the location is known. A country-level fix cannot support frost dates,
    and the CLARIFY step should say so rather than silently use a national average."""


# --------------------------------------------------------------------------------------
# Boundary intent
# --------------------------------------------------------------------------------------


class StatedDimensions(BaseModel):
    """"A 1 km by 1 km farm."

    A rectangle is the cold-start *default* here, never an assumption baked into the model.
    :attr:`is_synthesised` stays True through the whole pipeline so the presented map can
    say "this is a placeholder shape derived from the dimensions you gave me", which is the
    difference between a defensible default and a lie about the field.
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["stated_dimensions"] = "stated_dimensions"
    length_m: Metres
    width_m: Metres
    is_synthesised: Literal[True] = True


class StatedArea(BaseModel):
    """"About 100 hectares" with no shape. Even less is known than for dimensions."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["stated_area"] = "stated_area"
    area_ha: Hectares
    assumed_aspect_ratio: float = Field(default=1.0, gt=0.0)
    is_synthesised: Literal[True] = True


class UploadedBoundary(BaseModel):
    """A real surveyed or drawn boundary. Once this exists, nothing may assume a square."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["uploaded_boundary"] = "uploaded_boundary"
    boundary: GeoJson
    source_file: str | None = None
    is_synthesised: Literal[False] = False


BoundaryIntent = Annotated[
    StatedDimensions | StatedArea | UploadedBoundary,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------------------
# Crop requests
# --------------------------------------------------------------------------------------


class CropRequest(BaseModel):
    """One crop the grower wants, with the bounds that make it negotiable or not.

    ``max_area_ha`` is what makes the brief's key refinement turn expressible: "I don't have
    a buyer for that much garlic" is a companion-crop area ceiling, and re-solving under it
    is a constraint delta, not a new conversation.
    """

    model_config = ConfigDict(frozen=True)

    crop_slug: str
    role: CropRole
    min_area_ha: Hectares | None = Field(
        default=None, description="Floor. Contract obligations live here."
    )
    max_area_ha: Hectares | None = Field(
        default=None,
        description="Ceiling. Usually a market constraint: area the grower cannot sell.",
    )
    is_contracted: bool = Field(
        default=False,
        description="A contracted floor is not a preference. It must never be relaxed to "
        "make a spray target reachable; the trade-off is reported instead.",
    )
    has_market: bool | None = Field(
        default=None,
        description="None means unasked. False on a companion crop means its area is a pure "
        "cost and the optimizer should use the minimum that achieves the effect.",
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _bounds_ordered(self) -> CropRequest:
        if (
            self.min_area_ha is not None
            and self.max_area_ha is not None
            and self.min_area_ha > self.max_area_ha
        ):
            raise ValueError(
                f"{self.crop_slug}: min area {self.min_area_ha} ha exceeds max "
                f"{self.max_area_ha} ha. This is a contradiction to surface to the grower, "
                "not something to reconcile silently."
            )
        return self


class InterventionCap(BaseModel):
    """The grower's stated ceiling on interventions.

    A *goal*, explicitly not a constraint the model may satisfy by weakening its pest
    assumptions. If the modelled count exceeds the cap, the plan says so and shows the
    trade-off curve.
    """

    model_config = ConfigDict(frozen=True)

    max_applications: float = Field(ge=0.0)
    basis: Literal["count", "tfi", "eiq"] = Field(
        default="count",
        description="'count' is what growers say. Treatment Frequency Index and "
        "Environmental Impact Quotient are better objectives where a sourced product table "
        "exists; Phase 2 picks based on what we can actually source.",
    )
    is_hard_limit: bool = Field(
        default=False,
        description="True only where a certification or regulatory ceiling applies. A "
        "grower's preference is not a hard limit and must not be reported as infeasible.",
    )


# --------------------------------------------------------------------------------------
# The spec
# --------------------------------------------------------------------------------------


class FarmSpec(BaseModel):
    """Structured intake state. Incomplete by design — it exists to be progressively filled."""

    model_config = ConfigDict(frozen=True)

    id: Id = Field(default_factory=new_id)
    user_role: Sourced[UserRole] = Field(
        default_factory=lambda: Sourced[UserRole](
            value=UserRole.UNKNOWN, provenance=Provenance.UNKNOWN
        )
    )
    location: Sourced[Location] | None = None
    boundary_intent: Sourced[BoundaryIntent] | None = None
    crops: tuple[CropRequest, ...] = ()
    intervention_cap: Sourced[InterventionCap] | None = None
    implement_width_m: Sourced[float] | None = None
    irrigation: Sourced[IrrigationType] | None = None
    certification: Sourced[CertificationStatus] | None = None
    season_year: Sourced[int] | None = None
    last_frost_doy: Sourced[int] | None = None
    first_frost_doy: Sourced[int] | None = None
    pest_complex: Sourced[tuple[str, ...]] | None = None
    has_scouting_history: Sourced[bool] | None = None
    raw_transcript: tuple[str, ...] = Field(
        default=(),
        description="Grower turns, verbatim. Kept so CONFIRM can quote rather than "
        "paraphrase, and so a misparse is auditable after the fact.",
    )

    # Fields the solver cannot run without.
    REQUIRED_FOR_SOLVE: ClassVar[tuple[str, ...]] = (
        "location",
        "boundary_intent",
        "implement_width_m",
    )

    def missing_required(self) -> tuple[str, ...]:
        """Required fields that are absent or explicitly UNKNOWN."""
        missing: list[str] = []
        for name in self.REQUIRED_FOR_SOLVE:
            held = getattr(self, name)
            if held is None or held.provenance is Provenance.UNKNOWN:
                missing.append(name)
        if not self.crops:
            missing.append("crops")
        return tuple(missing)

    def missing_optional(self) -> tuple[str, ...]:
        """Absent fields that are not blocking but do change the answer."""
        candidates = (
            "intervention_cap",
            "irrigation",
            "certification",
            "season_year",
            "has_scouting_history",
        )
        return tuple(
            name
            for name in candidates
            if getattr(self, name) is None
            or getattr(self, name).provenance is Provenance.UNKNOWN
        )

    def assumptions(self) -> tuple[tuple[str, str], ...]:
        """(field name, grower-readable basis) for everything the grower did not state.

        This is the payload of the ``CONFIRM`` turn. If it is empty, there is nothing to
        confirm; if it is long, the intake leaned on defaults and the plan is fragile.
        """
        out: list[tuple[str, str]] = []
        for name in type(self).model_fields:
            held = getattr(self, name, None)
            if isinstance(held, Sourced) and held.needs_confirmation:
                out.append((name, held.basis or f"{held.provenance} with no stated basis"))
        return tuple(out)

    def crop_by_slug(self, slug: str) -> CropRequest | None:
        for request in self.crops:
            if request.crop_slug == slug:
                return request
        return None

    @property
    def is_solvable(self) -> bool:
        return not self.missing_required()


OBJECTIVE_SENSITIVITY: dict[str, tuple[int, str]] = {
    # (rank weight, why it moves the layout). Higher weight = ask sooner.
    #
    # PLACEHOLDER RANKS. These are the authors' priors, not measured sensitivities, and
    # Phase 3 must replace them by perturbing each field on a reference case and recording
    # the change in modelled applications. They are kept in one table with this warning so
    # they cannot quietly become folklore.
    "location": (
        100,
        "Sets frost dates, degree-day accumulation, and which pest complex is planned "
        "against. Nothing downstream is meaningful without it.",
    ),
    "crops": (95, "Determines which interaction coefficients exist at all."),
    "boundary_intent": (85, "Sets total area and therefore whether any area floor is reachable."),
    "implement_width_m": (
        70,
        "Sets block size and headland depth; a grid that does not match the machinery is a "
        "grid the grower cannot drive.",
    ),
    "intervention_cap": (
        65,
        "The objective target. Without it the optimizer minimises applications with no "
        "stopping point and cannot report a trade-off against what the grower wanted.",
    ),
    "crop_area_bounds": (
        60,
        "A contracted floor is usually the binding constraint on how much companion area is "
        "available.",
    ),
    "irrigation": (
        50,
        "Drip fixes lateral positions and flood basins forbid fine interleaving. Can rule "
        "out whole row-pattern templates.",
    ),
    "certification": (45, "Determines which interventions are legal to model at all."),
    "season_year": (30, "Selects the weather series or climate normals."),
    "has_scouting_history": (20, "Calibrates the pressure model; cold-start is workable."),
}
