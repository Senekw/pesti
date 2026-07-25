"""Plans, revisions, and the dated action plan that is the actual deliverable.

A layout is not a plan. The grower cannot act on a coloured map; they act on "drill garlic
in the border strips between 20 October and 5 November", "lift garlic from block R04C11
onward no earlier than 10 July, because lifting before then removes aphid protection during
peak flight", and "scout weekly from first flight; treat only on threshold". So
:class:`ActionPlan` is a first-class part of a revision rather than a rendering concern —
another addition to the brief's entity list.

:class:`PlanRevision` is immutable and carries everything needed to reproduce itself: the
grid hash, the parameter-set hash, the solver seed and version, and the full constraint set.
A revision that cannot be recomputed is not evidence.
"""

from __future__ import annotations

import enum
import hashlib
import json
from datetime import UTC, date, datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from intercrop.domain._types import Fraction, Id, new_id
from intercrop.domain.crops import CropRole


class BlockAssignment(BaseModel):
    """What the solver decided for one block."""

    model_config = ConfigDict(frozen=True)

    block_code: str
    pattern_code: str = Field(description="Row-pattern template chosen for this block.")
    planting_dates: dict[str, date] = Field(
        default_factory=dict,
        description="Crop slug -> planting date. Per crop because a companion is frequently "
        "sown months before the main crop; a single block-level date cannot express the "
        "autumn-garlic / post-frost-tomato arrangement at all.",
    )
    removal_dates: dict[str, date] = Field(
        default_factory=dict,
        description="Crop slug -> harvest or destruction date. The garlic lift date belongs "
        "here, and it is a decision variable, not a given: lifting early forfeits protection.",
    )

    @model_validator(mode="after")
    def _removal_after_planting(self) -> BlockAssignment:
        for slug, removed in self.removal_dates.items():
            planted = self.planting_dates.get(slug)
            if planted and removed <= planted:
                raise ValueError(
                    f"block {self.block_code}, {slug}: removal {removed} is not after "
                    f"planting {planted}"
                )
        return self


class ActionKind(enum.StrEnum):
    LAND_PREP = "land_prep"
    PLANT = "plant"
    IRRIGATE = "irrigate"
    SCOUT = "scout"
    """Not optional. A threshold-based plan that never scouts is a calendar-spray plan."""
    EXPECTED_INTERVENTION = "expected_intervention"
    """A modelled *expectation* of a treatment window, never a prescription. See
    :attr:`ActionItem.is_advisory`."""
    HARVEST = "harvest"
    REMOVE_COMPANION = "remove_companion"
    DESTROY_TRAP_CROP = "destroy_trap_crop"


class ActionItem(BaseModel):
    """One dated operation.

    ``is_advisory`` is fixed True for interventions on purpose. The brief requires framing
    spray reduction as a modelled expectation, deferring to product labels and local
    extension guidance, and never advising a grower to skip a legally required treatment. A
    structural flag beats hoping the presentation layer remembers the disclaimer.
    """

    model_config = ConfigDict(frozen=True)

    kind: ActionKind
    window_start: date
    window_end: date
    block_codes: tuple[str, ...] = Field(
        description="Blocks this applies to. Empty tuple is not allowed; whole-field actions "
        "list every block so the export is unambiguous."
    )
    crop_slug: str | None = None
    role: CropRole | None = None
    description: str
    rationale: str = Field(
        description="Why this date. 'Garlic must remain standing through peak Myzus flight "
        "(modelled 8-22 July) to retain the interplant effect' is a rationale; 'lift garlic' "
        "is not."
    )
    depends_on_parameter_keys: tuple[str, ...] = Field(
        default=(),
        description="Parameters this date derives from. Makes the provenance test able to "
        "check that every dated claim traces to a source.",
    )
    is_advisory: bool = Field(
        default=True,
        description="Always True for EXPECTED_INTERVENTION. Decision support, not "
        "prescription: the label and local extension guidance govern.",
    )
    threshold_note: str | None = Field(
        default=None,
        description="For SCOUT and EXPECTED_INTERVENTION, the economic threshold that should "
        "actually trigger action, quoted from its source.",
    )

    @model_validator(mode="after")
    def _sane(self) -> ActionItem:
        if self.window_end < self.window_start:
            raise ValueError("window_end precedes window_start")
        if not self.block_codes:
            raise ValueError("an action must name the blocks it applies to")
        if self.kind is ActionKind.EXPECTED_INTERVENTION:
            if not self.is_advisory:
                raise ValueError(
                    "an expected intervention is always advisory; it must never be emitted "
                    "as a prescription"
                )
            if not self.threshold_note:
                raise ValueError(
                    "an expected intervention must carry the scouting threshold that should "
                    "trigger it, so the grower treats on evidence rather than on our calendar"
                )
        return self


class ActionPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[ActionItem, ...] = ()

    @property
    def expected_intervention_count(self) -> int:
        return sum(1 for i in self.items if i.kind is ActionKind.EXPECTED_INTERVENTION)

    def in_order(self) -> tuple[ActionItem, ...]:
        return tuple(sorted(self.items, key=lambda i: (i.window_start, i.kind)))


class ObjectiveOutcome(BaseModel):
    """What the solve actually achieved, including what it did not.

    :attr:`shortfall_note` is required whenever the modelled count exceeds the grower's cap.
    The brief's third rule — never satisfy a constraint by relaxing the model — is enforced
    here by making the honest answer mandatory rather than optional.
    """

    model_config = ConfigDict(frozen=True)

    expected_applications: float = Field(ge=0.0)
    basis: str = Field(default="count", description="'count', 'tfi', or 'eiq'.")
    main_crop_area_ha: float = Field(ge=0.0)
    companion_area_ha: dict[str, float] = Field(default_factory=dict)
    requested_cap: float | None = None
    area_floor_ha: float | None = None
    shortfall_note: str | None = None
    uses_provisional_parameters: tuple[str, ...] = Field(
        default=(),
        description="Provisional parameter keys this result depends on. Non-empty means "
        "every number presented alongside it must be labelled provisional.",
    )

    @model_validator(mode="after")
    def _must_admit_shortfall(self) -> ObjectiveOutcome:
        if (
            self.requested_cap is not None
            and self.expected_applications > self.requested_cap + 1e-9
            and not self.shortfall_note
        ):
            raise ValueError(
                f"modelled {self.expected_applications} applications exceeds the requested "
                f"cap of {self.requested_cap} but no shortfall_note explains it. State the "
                "gap and the trade-off; do not present this as a success."
            )
        return self

    @property
    def meets_cap(self) -> bool:
        if self.requested_cap is None:
            return True
        return self.expected_applications <= self.requested_cap + 1e-9


class SolverProvenance(BaseModel):
    """Enough to rerun the solve and get the same answer."""

    model_config = ConfigDict(frozen=True)

    solver: str = Field(default="cp_sat")
    solver_version: str
    seed: int = Field(description="Fixed seed. A plan that cannot be reproduced is not a plan.")
    status: str = Field(description="OPTIMAL / FEASIBLE / INFEASIBLE / UNKNOWN.")
    wall_time_s: float = Field(ge=0.0)
    objective_bound: float | None = None
    warm_started_from_revision: Id | None = None
    infeasibility_explanation: tuple[str, ...] = Field(
        default=(),
        description="Grower-readable conflicting constraints, from assumption literals. "
        "Required when status is INFEASIBLE — a bare INFEASIBLE is a bug, per the brief.",
    )

    @model_validator(mode="after")
    def _infeasible_must_explain(self) -> SolverProvenance:
        if self.status.upper() == "INFEASIBLE" and not self.infeasibility_explanation:
            raise ValueError(
                "INFEASIBLE with no explanation. Report which constraints conflict, in "
                "language the grower can act on."
            )
        return self


class PlanRevision(BaseModel):
    """An immutable candidate layout. Becomes the plan of record only via approval."""

    model_config = ConfigDict(frozen=True)

    id: Id = Field(default_factory=new_id)
    plan_id: Id
    revision_number: int = Field(ge=1)
    parent_revision_id: Id | None = None
    grid_content_hash: str
    parameter_set_hash: str
    constraint_set: dict[str, object] = Field(
        description="The full, serialised constraint set that produced this revision — "
        "including any refinement deltas. Stored verbatim so a later reviewer can see what "
        "was actually asked, not a summary of it."
    )
    assignments: tuple[BlockAssignment, ...]
    outcome: ObjectiveOutcome
    action_plan: ActionPlan = Field(default_factory=ActionPlan)
    solver: SolverProvenance
    pareto_label: str | None = Field(
        default=None,
        description="Which point on the front this is, when several were generated.",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str = Field(description="Agent or user identifier that produced this revision.")

    @model_validator(mode="after")
    def _unique_blocks(self) -> PlanRevision:
        codes = [a.block_code for a in self.assignments]
        if len(set(codes)) != len(codes):
            raise ValueError("a block cannot receive two assignments in one revision")
        return self

    def state_hash(self) -> str:
        """Hash of everything a proposal must be revalidated against.

        Covers the inputs (grid, parameters, constraints) and the decisions
        (assignments, outcome). If any of these moved since a proposal was raised, the
        proposal is stale and must not commit.
        """
        payload = {
            "grid_content_hash": self.grid_content_hash,
            "parameter_set_hash": self.parameter_set_hash,
            "constraint_set": self.constraint_set,
            "assignments": [
                {
                    "block_code": a.block_code,
                    "pattern_code": a.pattern_code,
                    "planting_dates": {
                        k: v.isoformat() for k, v in sorted(a.planting_dates.items())
                    },
                    "removal_dates": {
                        k: v.isoformat() for k, v in sorted(a.removal_dates.items())
                    },
                }
                for a in sorted(self.assignments, key=lambda a: a.block_code)
            ],
            "outcome": self.outcome.model_dump(mode="json"),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def block_count(self) -> int:
        return len(self.assignments)


class PlanStatus(enum.StrEnum):
    DRAFT = "draft"
    """Solved and shown, never committed. The default and the common case."""
    OF_RECORD = "of_record"
    """Committed under an approval. The only status an export may reference."""
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"


class Plan(BaseModel):
    """Durable identity for one field's planning effort across many revisions."""

    model_config = ConfigDict(frozen=True)

    id: Id = Field(default_factory=new_id)
    field_id: Id
    spec_id: Id
    season_year: int
    name: str
    status: PlanStatus = PlanStatus.DRAFT
    revision_of_record_id: Id | None = Field(
        default=None,
        description="Set only by a committed proposal with a matching ApprovalRecord.",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _record_needs_revision(self) -> Plan:
        if self.status is PlanStatus.OF_RECORD and self.revision_of_record_id is None:
            raise ValueError("a plan of record must point at the revision that was approved")
        if self.status is not PlanStatus.OF_RECORD and self.revision_of_record_id is not None:
            raise ValueError(
                "only a plan of record may name a revision of record; a draft pointing at "
                "one is how an unapproved layout gets treated as committed"
            )
        return self


class PlanDiff(BaseModel):
    """Structured difference between two revisions.

    Carried by a commit proposal, and shown during ``REFINE`` so the grower can see what
    their objection cost. ``applications_delta`` is the number they will look at first.
    """

    model_config = ConfigDict(frozen=True)

    from_revision_id: Id | None
    to_revision_id: Id
    blocks_changed: tuple[str, ...] = ()
    blocks_added: tuple[str, ...] = ()
    blocks_removed: tuple[str, ...] = ()
    area_delta_ha: dict[str, float] = Field(
        default_factory=dict, description="Crop slug -> change in hectares."
    )
    applications_delta: float | None = None
    changed_fraction: Fraction = 0.0
    summary: str = Field(description="One or two sentences a grower would recognise as true.")
