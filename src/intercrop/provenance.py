"""Provenance primitives.

Two separate concerns live here, and conflating them is a bug:

``Sourced[T]``
    Where a value in a :class:`~intercrop.domain.spec.FarmSpec` came from. Did the grower
    say it, did we infer it from their location, or is it a cold-start default? The
    ``CONFIRM`` conversation state exists to show the grower every value that they did
    *not* personally state, so this has to be attached to the value, not logged elsewhere.

``ParameterRecord``
    Where an *agronomic* number came from. Interaction coefficients, degree-day base
    temperatures, economic thresholds, yield penalties. These are only allowed to exist in
    a versioned parameter file with a citation and a validity range. A parameter with no
    citation is legal only when explicitly marked ``PROVISIONAL``, and provisional status
    propagates to every output that touches it.
"""

from __future__ import annotations

import enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")


class Provenance(enum.StrEnum):
    """How a spec value came to hold its current value."""

    STATED = "stated"
    """The grower said it, in their own words. Never needs confirming."""

    MEASURED = "measured"
    """Read from ingested data (boundary file, scouting record, sensor)."""

    INFERRED = "inferred"
    """Derived from something the grower did state — frost dates from a location,
    pest complex from a region and crop. Must be shown back for confirmation."""

    DEFAULT = "default"
    """A cold-start default with no grower input behind it at all. Must be shown back,
    and is the first thing to question when a plan looks wrong."""

    UNKNOWN = "unknown"
    """We need this and do not have it. Drives ``rank_missing_fields``."""


class Sourced(BaseModel, Generic[T]):
    """A spec value carrying where it came from.

    ``basis`` is written for the grower, not for a log file. It ends up on screen during
    ``CONFIRM``, so "Punjab is in CWC zone; last frost from IMD normals 1991-2020" is
    useful and "inferred from location" is not.
    """

    model_config = ConfigDict(frozen=True)

    value: T
    provenance: Provenance
    basis: str | None = Field(
        default=None,
        description="Grower-readable explanation of how this value was arrived at. "
        "Required for anything not STATED or MEASURED.",
    )
    utterance: str | None = Field(
        default=None,
        description="Verbatim grower text this was extracted from, when STATED. Kept so "
        "the CONFIRM step can quote them back rather than paraphrase.",
    )
    parameter_key: str | None = Field(
        default=None,
        description="Key into the parameter set, when this value was looked up rather "
        "than stated. Gives outputs a traceable path back to a citation.",
    )

    @model_validator(mode="after")
    def _explain_yourself(self) -> Sourced[T]:
        if self.provenance in (Provenance.INFERRED, Provenance.DEFAULT) and not self.basis:
            raise ValueError(
                f"provenance={self.provenance} requires a 'basis' the grower can read; "
                "an unexplained inference is indistinguishable from a guess"
            )
        return self

    @property
    def needs_confirmation(self) -> bool:
        """True when the ``CONFIRM`` step must surface this value as an assumption."""
        return self.provenance not in (Provenance.STATED, Provenance.MEASURED)

    @classmethod
    def stated(cls, value: T, utterance: str | None = None) -> Sourced[T]:
        return cls(value=value, provenance=Provenance.STATED, utterance=utterance)

    @classmethod
    def inferred(cls, value: T, basis: str, parameter_key: str | None = None) -> Sourced[T]:
        return cls(
            value=value,
            provenance=Provenance.INFERRED,
            basis=basis,
            parameter_key=parameter_key,
        )

    @classmethod
    def defaulted(cls, value: T, basis: str) -> Sourced[T]:
        return cls(value=value, provenance=Provenance.DEFAULT, basis=basis)


# --------------------------------------------------------------------------------------
# Agronomic parameter provenance
# --------------------------------------------------------------------------------------


class CitationKind(enum.StrEnum):
    PEER_REVIEWED = "peer_reviewed"
    META_ANALYSIS = "meta_analysis"
    EXTENSION = "extension"
    """State/university extension guidance. Regionally authoritative, rarely quantitative."""
    DATASET = "dataset"
    """Climate normals, soil survey, pest survey."""
    EXPERT_ELICITATION = "expert_elicitation"
    """A named agronomist's judgement. Legitimate, but must be attributed to a person."""


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: CitationKind
    title: str
    year: int | None = None
    authors: str | None = None
    doi: str | None = None
    url: str | None = None
    locator: str | None = Field(
        default=None,
        description="Where in the source, e.g. 'Table 3' or 'Fig. 2b'. Without this a "
        "reviewer cannot check the number, which defeats the point of the citation.",
    )

    @model_validator(mode="after")
    def _must_be_findable(self) -> Citation:
        if self.kind is CitationKind.EXPERT_ELICITATION:
            if not self.authors:
                raise ValueError("expert elicitation must name the expert")
            return self
        if not (self.doi or self.url):
            raise ValueError(
                f"citation {self.title!r} has neither DOI nor URL; a number nobody can "
                "look up is not sourced"
            )
        return self


class ParameterStatus(enum.StrEnum):
    PUBLISHED = "published"
    """Backed by a citation. Safe to use inside its validity range."""

    PROVISIONAL = "provisional"
    """A placeholder. Usable, but every output it touches must say so."""

    DEPRECATED = "deprecated"
    """Superseded. Loading is allowed so old plan revisions stay reproducible, but new
    solves must refuse it."""


class ValidityRange(BaseModel):
    """The envelope a parameter was measured in.

    Phase 1 must refuse to evaluate a parameter outside this envelope rather than
    extrapolate. A repellency coefficient measured on greenhouse aubergine at 0.3 m
    spacing is not evidence about 100 ha of field tomato at 1.5 m, and quietly reusing it
    is the failure mode the brief calls out as most likely to hurt someone.
    """

    model_config = ConfigDict(frozen=True)

    geographic_scope: tuple[str, ...] = Field(
        default=("global",),
        description="Region codes (ISO 3166-2 where possible) or 'global'.",
    )
    cropping_system: tuple[str, ...] = Field(
        default=(),
        description="e.g. ('open_field',) vs ('protected', 'greenhouse'). Empty means unstated "
        "in the source, which is itself worth surfacing.",
    )
    numeric_bounds: dict[str, tuple[float, float]] = Field(
        default_factory=dict,
        description="Named inclusive bounds the measurement holds within, e.g. "
        "{'row_spacing_m': (0.9, 1.8), 'mean_temp_c': (12.0, 32.0)}.",
    )
    notes: str | None = None

    def violations(self, context: dict[str, Any]) -> list[str]:
        """Return grower-readable reasons this parameter does not apply to ``context``.

        Empty list means in range. Unknown context keys are ignored; a missing key is a
        *silent* out-of-range risk, so callers that care should assert the keys they
        supplied. Phase 1's kernel evaluation is the caller that cares.
        """
        problems: list[str] = []
        region = context.get("region")
        if (
            region is not None
            and "global" not in self.geographic_scope
            and not any(str(region).startswith(scope) for scope in self.geographic_scope)
        ):
            problems.append(
                f"measured in {'/'.join(self.geographic_scope)}, applied to {region}"
            )
        system = context.get("cropping_system")
        if system is not None and self.cropping_system and system not in self.cropping_system:
            problems.append(
                f"measured in {'/'.join(self.cropping_system)}, applied to {system}"
            )
        for key, (low, high) in self.numeric_bounds.items():
            observed = context.get(key)
            if observed is None:
                continue
            if not low <= float(observed) <= high:
                problems.append(f"{key}={observed} outside measured range {low}–{high}")
        return problems


class Evidenced(BaseModel):
    """Base for anything asserting an agronomic fact.

    Both :class:`ParameterRecord` and
    :class:`~intercrop.domain.interactions.InteractionCoefficient` inherit this, so the
    provenance test in the suite covers every numeric claim the system can emit rather than
    just the ones that happen to live in the parameter file.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(
        pattern=r"^[a-z0-9]+(?:[._:>-]+[a-z0-9]+)*$",
        description="Dotted key, e.g. 'pest.myzus_persicae.gdd.base_temp_c'.",
    )
    status: ParameterStatus
    validity: ValidityRange = Field(default_factory=ValidityRange)
    citations: tuple[Citation, ...] = ()
    provisional_rationale: str | None = Field(
        default=None,
        description="Why a provisional value is defensible and what would replace it. "
        "Required when status is PROVISIONAL.",
    )
    superseded_by: str | None = None

    @model_validator(mode="after")
    def _no_unsourced_published_claims(self) -> Evidenced:
        if self.status is ParameterStatus.PUBLISHED and not self.citations:
            raise ValueError(
                f"{self.key!r} is PUBLISHED with no citation. Either add the source or mark "
                "it PROVISIONAL — those are the only two honest options."
            )
        if self.status is ParameterStatus.PROVISIONAL and not self.provisional_rationale:
            raise ValueError(
                f"provisional entry {self.key!r} needs a rationale saying what it stands in "
                "for and what evidence would replace it"
            )
        if self.status is ParameterStatus.DEPRECATED and not self.superseded_by:
            raise ValueError(f"deprecated entry {self.key!r} must say what replaced it")
        return self

    @property
    def is_provisional(self) -> bool:
        return self.status is ParameterStatus.PROVISIONAL

    def check_applicable(self, context: dict[str, Any]) -> None:
        """Raise if this claim is being used outside the envelope it was measured in.

        Deliberately an exception rather than a warning. The brief's third rule is never to
        satisfy a constraint by relaxing the model, and silent extrapolation is the quiet
        version of exactly that.
        """
        if self.status is ParameterStatus.DEPRECATED:
            raise OutOfValidityRange(
                f"{self.key!r} is deprecated; use {self.superseded_by!r}"
            )
        problems = self.validity.violations(context)
        if problems:
            raise OutOfValidityRange(
                f"{self.key!r} does not apply here: " + "; ".join(problems)
            )


class OutOfValidityRange(Exception):
    """Raised when a sourced number is asked to speak outside its evidence."""


class ParameterRecord(Evidenced):
    """One agronomic number, with everything needed to defend it."""

    value: float | int | str | bool | list[float]
    units: str | None = Field(
        default=None, description="Explicit units, or None for dimensionless/categorical."
    )
