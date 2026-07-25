"""Crop-crop-pest interactions, and the distance-decay kernels that make them spatial.

This module is where the brief's central agronomic claim becomes a data structure. Garlic
next to tomato is not a binary "both are in the field" fact; it is an effect that decays with
distance and requires the two crops to be in the ground at the same time as the pest is
flying. Three separate things therefore have to be represented:

1. **How big the effect is** — with a citation, an effect measure, and a validity range.
2. **How it falls off with distance** — a pluggable kernel, because the literature does not
   agree on a single form and pretending otherwise is a false precision.
3. **When it applies** — a temporal-overlap requirement, so lifting the garlic in June
   correctly removes protection in July.

Mechanisms are never summed blindly. Repellency and natural-enemy provisioning are different
processes with different spatial scales, and the brief requires them held apart; the
:class:`Mechanism` enum plus :attr:`InteractionCoefficient.mechanism` is how Phase 1 keeps
them in separate terms.
"""

from __future__ import annotations

import enum
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from intercrop.domain._types import Id, new_id
from intercrop.provenance import Evidenced


class Mechanism(enum.StrEnum):
    """The process producing the effect. Different mechanisms compose differently.

    Two mechanisms acting on the same pest are combined multiplicatively on the *surviving*
    fraction rather than added, because two interventions each cutting colonisation by 40%
    leave 0.6 x 0.6 = 36% rather than 20%. Adding them would let a stack of weak effects
    fabricate near-total control. That composition rule belongs to Phase 1; the enum exists
    so it can be applied per mechanism rather than to an undifferentiated blob.
    """

    REPELLENCY = "repellency"
    """Volatiles deter settling on the neighbouring crop. Short range, metres."""

    HOST_MASKING = "host_masking"
    """Non-host volatiles or visual disruption interfere with host location. Short range."""

    TRAP_DIVERSION = "trap_diversion"
    """A more attractive host intercepts arriving pests. Range set by pest dispersal, not
    by the volatile, so its kernel is typically much wider than repellency's."""

    NATURAL_ENEMY_PROVISIONING = "natural_enemy_provisioning"
    """Floral or shelter resources raise predator and parasitoid activity. Range is set by
    natural-enemy foraging distance — commonly tens to low hundreds of metres, far wider
    than repellency. Held strictly separate from REPELLENCY per the brief."""

    PHYSICAL_BARRIER = "physical_barrier"
    """A taller or denser neighbour impedes movement or spore splash."""

    DISEASE_SUPPRESSION = "disease_suppression"
    """Suppression of a pathogen rather than an insect."""

    ALLELOPATHY = "allelopathy"
    """A negative effect on the neighbouring crop. Allium on legumes is the case the brief
    names. Never a benefit; feeds the hard validators, not the objective."""

    RESOURCE_COMPETITION = "resource_competition"
    """Light, water, or nutrient competition. Negative, and the reason a yield penalty term
    exists alongside the pest benefit."""

    @property
    def is_beneficial(self) -> bool:
        return self not in (Mechanism.ALLELOPATHY, Mechanism.RESOURCE_COMPETITION)


class EffectMeasure(enum.StrEnum):
    """What scale the effect size is on.

    This is not pedantry. Meta-analyses usually report a log response ratio or a
    standardised mean difference; extension trials usually report a percentage reduction.
    Treating a Hedges' *g* of 0.8 as "80% fewer aphids" overstates control by a wide and
    unpredictable margin. Conversion must be explicit and is refused where it is not
    well defined.
    """

    PROPORTIONAL_REDUCTION = "proportional_reduction"
    """Directly usable: 0.35 means 35% fewer colonising individuals at zero distance."""

    LOG_RESPONSE_RATIO = "log_response_ratio"
    """ln(treatment / control). Convertible: reduction = 1 - exp(lrr)."""

    HEDGES_G = "hedges_g"
    """Standardised mean difference. NOT convertible to a proportion without the control
    mean and SD, which are usually not reported. Phase 1 must refuse these rather than
    guess a conversion."""

    PERCENT_REDUCTION = "percent_reduction"
    """0-100 scale. Converted by dividing by 100."""


# --------------------------------------------------------------------------------------
# Distance-decay kernels
# --------------------------------------------------------------------------------------


class ExponentialKernel(BaseModel):
    """``exp(-d / scale)``. Smooth, never reaches zero.

    The usual default for volatile-mediated effects, and defensible as a first
    approximation: concentration falls off continuously from a source. Its weakness is the
    long tail — at 3x the scale length it still returns 5%, which across a 100 ha field sums
    to a non-trivial phantom benefit. Phase 1 should apply ``cutoff_m`` so that tail cannot
    accumulate into a spray reduction nobody measured.
    """

    model_config = ConfigDict(frozen=True)
    form: Literal["exponential"] = "exponential"
    scale_m: float = Field(gt=0.0, description="e-folding distance in metres.")
    cutoff_m: float | None = Field(
        default=None, gt=0.0, description="Hard zero beyond this distance."
    )

    def evaluate(self, distance_m: float) -> float:
        if distance_m < 0:
            raise ValueError("distance cannot be negative")
        if self.cutoff_m is not None and distance_m > self.cutoff_m:
            return 0.0
        return math.exp(-distance_m / self.scale_m)


class ThresholdKernel(BaseModel):
    """Full effect within ``radius_m``, nothing beyond.

    Blunt, but it is what most of the intercropping literature actually supports: trials
    report "interplanted" versus "monoculture" at one spacing, which is a single point, not
    a curve. A threshold kernel is honest about that — it claims an effect at the spacing
    measured and declines to extrapolate. Prefer this for any coefficient whose source
    tested one arrangement.
    """

    model_config = ConfigDict(frozen=True)
    form: Literal["threshold"] = "threshold"
    radius_m: float = Field(gt=0.0)

    def evaluate(self, distance_m: float) -> float:
        if distance_m < 0:
            raise ValueError("distance cannot be negative")
        return 1.0 if distance_m <= self.radius_m else 0.0


class LinearTaperKernel(BaseModel):
    """Full effect to ``full_m``, linear decline to zero at ``zero_m``.

    A middle course when a source reports two spacings. No mechanistic claim, but it does
    not invent a tail the way an exponential does.
    """

    model_config = ConfigDict(frozen=True)
    form: Literal["linear_taper"] = "linear_taper"
    full_m: float = Field(ge=0.0)
    zero_m: float = Field(gt=0.0)

    @model_validator(mode="after")
    def _ordered(self) -> LinearTaperKernel:
        if self.zero_m <= self.full_m:
            raise ValueError("zero_m must exceed full_m")
        return self

    def evaluate(self, distance_m: float) -> float:
        if distance_m < 0:
            raise ValueError("distance cannot be negative")
        if distance_m <= self.full_m:
            return 1.0
        if distance_m >= self.zero_m:
            return 0.0
        return (self.zero_m - distance_m) / (self.zero_m - self.full_m)


DecayKernel = Annotated[
    ExponentialKernel | ThresholdKernel | LinearTaperKernel,
    Field(discriminator="form"),
]


# --------------------------------------------------------------------------------------
# The interaction itself
# --------------------------------------------------------------------------------------


class TemporalRequirement(BaseModel):
    """When the effect is actually available.

    The garlic/tomato case exists to be caught here. Garlic goes in in autumn and is lifted
    mid-summer; tomato goes out after last frost. Protection is only real in the intersection
    of (garlic standing) x (tomato standing) x (pest active), and a model that ignores this
    will happily credit July protection to a crop lifted in June.
    """

    model_config = ConfigDict(frozen=True)

    requires_co_occupancy: bool = Field(
        default=True,
        description="False only for residue- or soil-mediated effects that outlast the "
        "standing crop.",
    )
    min_overlap_days: int = Field(
        default=0,
        ge=0,
        description="Establishment lag before the effect is available. A just-emerged garlic "
        "clove is not yet producing protective volatiles at field scale.",
    )
    effect_persists_after_removal_days: int = Field(
        default=0,
        ge=0,
        description="Residual effect after the companion is lifted. Usually near zero for "
        "volatile-mediated repellency, which is precisely why lift date matters.",
    )


class InteractionCoefficient(Evidenced):
    """"Crop A, next to crop B, changes pressure from pest P by this much, over this range."

    Direction matters and is not symmetric: garlic protecting tomato from aphids is a
    different claim from tomato protecting garlic, and only one of them may be in evidence.
    """

    id: Id = Field(default_factory=new_id)
    source_crop_slug: str = Field(description="The crop producing the effect (e.g. garlic).")
    target_crop_slug: str = Field(description="The crop receiving it (e.g. tomato).")
    pest_slug: str | None = Field(
        default=None,
        description="The pest affected. Required for pest-mediated mechanisms; None only "
        "for crop-on-crop mechanisms such as ALLELOPATHY or RESOURCE_COMPETITION.",
    )
    mechanism: Mechanism
    effect_size: float
    effect_measure: EffectMeasure
    kernel: DecayKernel
    temporal: TemporalRequirement = Field(default_factory=TemporalRequirement)
    measured_at_distance_m: float | None = Field(
        default=None,
        ge=0.0,
        description="The spacing the source actually tested. The kernel is calibrated so it "
        "returns the reported effect here; without it a kernel fit is unfalsifiable.",
    )

    @model_validator(mode="after")
    def _coherent(self) -> InteractionCoefficient:
        pest_mediated = self.mechanism not in (
            Mechanism.ALLELOPATHY,
            Mechanism.RESOURCE_COMPETITION,
        )
        if pest_mediated and not self.pest_slug:
            raise ValueError(f"mechanism {self.mechanism} requires a pest_slug")
        if not pest_mediated and self.pest_slug:
            raise ValueError(
                f"mechanism {self.mechanism} is crop-on-crop and must not name a pest"
            )
        if self.source_crop_slug == self.target_crop_slug:
            raise ValueError("an interaction needs two different crops")
        if self.effect_measure is EffectMeasure.PROPORTIONAL_REDUCTION and not (
            -1.0 <= self.effect_size <= 1.0
        ):
            raise ValueError("proportional reduction must be in [-1, 1]")
        if self.effect_measure is EffectMeasure.PERCENT_REDUCTION and not (
            -100.0 <= self.effect_size <= 100.0
        ):
            raise ValueError("percent reduction must be in [-100, 100]")
        return self

    def proportional_reduction(self) -> float:
        """Effect size converted to a proportional reduction at the measured distance.

        Raises for :attr:`EffectMeasure.HEDGES_G`, which cannot be converted without the
        control mean and standard deviation. Refusing is the point: a standardised mean
        difference silently reinterpreted as a percentage is a fabricated efficacy claim.
        """
        match self.effect_measure:
            case EffectMeasure.PROPORTIONAL_REDUCTION:
                return self.effect_size
            case EffectMeasure.PERCENT_REDUCTION:
                return self.effect_size / 100.0
            case EffectMeasure.LOG_RESPONSE_RATIO:
                return 1.0 - math.exp(self.effect_size)
            case EffectMeasure.HEDGES_G:
                raise ValueError(
                    f"{self.key!r} reports Hedges' g, which has no distance-free conversion "
                    "to a proportional reduction. Supply the control mean and SD as separate "
                    "parameters, or record a proportional reduction from a source that "
                    "reports one."
                )

    def effect_at(self, distance_m: float) -> float:
        """Proportional reduction at ``distance_m``, before any temporal gating.

        Temporal availability is applied by the Phase 1 pressure model, not here, because it
        depends on the planting dates the solver chose.
        """
        return self.proportional_reduction() * self.kernel.evaluate(distance_m)
