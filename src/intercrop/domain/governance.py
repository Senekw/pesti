"""Proposals, approvals, and the audit log.

The rule this module exists to make unbypassable: **solving is free, persisting is gated.**
Running the optimizer and re-solving during refinement are read-only. Committing a plan of
record, ingesting data, and exporting to farm software each require an explicit human
approval carrying a diff, a rationale, and a hash of the state the proposal was computed
against.

Everything funnels through :func:`authorise_commit`. There is deliberately one choke point
rather than a check at each call site, because a second code path is how an approval gate
eventually gets bypassed. The gate enforces, in order:

1. The proposal exists and is still PENDING.
2. It has not expired.
3. The approval names a *genuine human turn* — see :class:`ApprovalSource`.
4. The approver is not the agent.
5. The state hash still matches what is on disk now, so a stale proposal cannot overwrite
   newer data.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from intercrop.domain._types import Id, new_id

DEFAULT_PROPOSAL_TTL = timedelta(hours=2)
"""Proposals expire. A grower who walked away for a day should not be able to approve a plan
computed against yesterday's parameter set."""


class ProposalKind(enum.StrEnum):
    PLAN_COMMIT = "plan_commit"
    DATA_INGEST = "data_ingest"
    EXPORT = "export"


class ProposalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    COMMITTED = "committed"
    FAILED_REVALIDATION = "failed_revalidation"
    """Approved, but the underlying state moved before commit. Recorded rather than
    discarded, because a silent re-solve here is exactly how newer data gets clobbered."""


class ApprovalSource(enum.StrEnum):
    """Where an approval claims to originate.

    Only :attr:`HUMAN_TURN` is ever acceptable. The other members exist so that an attempted
    bypass is a named, logged, testable rejection instead of an unhandled case — and so the
    attack surface is written down where a reviewer will see it.
    """

    HUMAN_TURN = "human_turn"
    """A real user message in the conversation. The only valid source."""

    MODEL_GENERATED = "model_generated"
    """The agent tried to approve its own proposal. Always rejected."""

    TOOL_RESULT = "tool_result"
    """Text returned by a tool asked to stand in for consent. Always rejected."""

    INGESTED_CONTENT = "ingested_content"
    """Text from an uploaded file — a scouting CSV row reading 'approved: yes', or a
    prompt-injection string in a boundary attribute table. Always rejected."""

    UNKNOWN = "unknown"
    """Provenance could not be established. Rejected: absence of evidence is not consent."""


class ApprovalDenied(Exception):
    """Raised whenever a gated action is attempted without valid human approval."""


class StaleProposal(ApprovalDenied):
    """The state moved between proposal and commit."""


class Proposal(BaseModel):
    """A request for a human to authorise a side effect.

    Returned by every ``propose_*`` tool. Note what is *not* here: any method that commits.
    A proposal is inert data; only :func:`authorise_commit` can act on one.
    """

    model_config = ConfigDict(frozen=True)

    id: Id = Field(default_factory=new_id)
    kind: ProposalKind
    status: ProposalStatus = ProposalStatus.PENDING
    state_hash: str = Field(
        min_length=16,
        description="Hash of the state this proposal was computed against — a plan "
        "revision's state_hash for a commit, or the target dataset's hash for an ingest. "
        "Revalidated at commit time.",
    )
    rationale: str = Field(
        min_length=1,
        description="Why this should happen, in the grower's terms. A proposal a human "
        "cannot evaluate is not a meaningful gate.",
    )
    diff: dict[str, Any] = Field(
        description="Structured description of what changes. Required, and empty is not "
        "allowed: 'nothing visibly changes' is itself something a human should be told."
    )
    payload: dict[str, Any] = Field(
        default_factory=dict, description="Action-specific detail (target revision, file, format)."
    )
    proposed_by: str = Field(description="Agent or component identifier. Never a human.")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC) + DEFAULT_PROPOSAL_TTL
    )

    @model_validator(mode="after")
    def _wellformed(self) -> Proposal:
        if not self.diff:
            raise ValueError(
                "a proposal must carry a diff; an unexplained change cannot be meaningfully "
                "approved"
            )
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at


class ApprovalRecord(BaseModel):
    """Evidence that a human authorised a specific proposal.

    ``human_turn_id`` is required and must be non-empty. It is the anchor tying this record
    to an identifiable message in the conversation, which is what makes "callable only from a
    genuine human turn" checkable after the fact rather than a convention.
    """

    model_config = ConfigDict(frozen=True)

    id: Id = Field(default_factory=new_id)
    proposal_id: Id
    source: ApprovalSource
    human_turn_id: str = Field(
        min_length=1,
        description="Identifier of the user turn carrying the approval. Required.",
    )
    approver: str = Field(min_length=1, description="Who approved. Must not be the agent.")
    approved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revalidated_state_hash: str | None = Field(
        default=None, description="Set by the gate at commit time once the hash is re-checked."
    )
    note: str | None = None

    @model_validator(mode="after")
    def _only_humans_approve(self) -> ApprovalRecord:
        if self.source is not ApprovalSource.HUMAN_TURN:
            raise ValueError(
                f"approval source {self.source} cannot authorise anything. Only a genuine "
                "human turn may approve a proposal — not the model, not a tool result, and "
                "not text inside an ingested file."
            )
        return self


class AuditEventKind(enum.StrEnum):
    PROPOSAL_RAISED = "proposal_raised"
    PROPOSAL_APPROVED = "proposal_approved"
    PROPOSAL_REJECTED = "proposal_rejected"
    PROPOSAL_EXPIRED = "proposal_expired"
    COMMIT_APPLIED = "commit_applied"
    COMMIT_DENIED = "commit_denied"
    """Every denial is logged. A repeated denial pattern is a signal worth seeing."""
    DATA_INGESTED = "data_ingested"
    EXPORTED = "exported"
    SOLVE_RUN = "solve_run"
    """Read-only, ungated, but still logged so a plan's history is legible."""


class AuditEvent(BaseModel):
    """One append-only log entry.

    Append-only is a storage guarantee, not a Python one — there is no update or delete path
    for this table, and Phase 0's persistence decision (see docs) must preserve that.
    """

    model_config = ConfigDict(frozen=True)

    id: Id = Field(default_factory=new_id)
    kind: AuditEventKind
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: str = Field(description="Who or what acted.")
    actor_is_human: bool
    proposal_id: Id | None = None
    plan_id: Id | None = None
    revision_id: Id | None = None
    state_hash_before: str | None = None
    state_hash_after: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


def authorise_commit(
    proposal: Proposal,
    approval: ApprovalRecord,
    current_state_hash: str,
    *,
    agent_identity: str,
    now: datetime | None = None,
) -> ApprovalRecord:
    """The single gate. Returns the approval stamped with the revalidated hash, or raises.

    Callers must treat a raise as final and must not retry with a fresh proposal on the
    grower's behalf — re-proposing after a denial converts a human gate into a formality.
    """
    now = now or datetime.now(UTC)

    if approval.proposal_id != proposal.id:
        raise ApprovalDenied(
            f"approval {approval.id} is for proposal {approval.proposal_id}, not "
            f"{proposal.id}. An approval is not transferable between proposals."
        )

    if proposal.status is not ProposalStatus.PENDING:
        raise ApprovalDenied(
            f"proposal {proposal.id} is {proposal.status}, not pending; it cannot be "
            "committed again"
        )

    if proposal.is_expired(now):
        raise ApprovalDenied(
            f"proposal {proposal.id} expired at {proposal.expires_at.isoformat()}. Re-run "
            "the solve against current data and raise a fresh proposal for the grower."
        )

    # Redundant with ApprovalRecord's own validator, and deliberately so: the gate must not
    # depend on the record having been constructed through validation.
    if approval.source is not ApprovalSource.HUMAN_TURN:
        raise ApprovalDenied(
            f"approval source {approval.source} is not a human turn and cannot authorise a "
            "commit"
        )

    if not approval.human_turn_id.strip():
        raise ApprovalDenied("approval carries no human turn identifier")

    if approval.approver.strip().casefold() == agent_identity.strip().casefold():
        raise ApprovalDenied(
            f"{agent_identity!r} cannot approve its own proposal"
        )

    if current_state_hash != proposal.state_hash:
        raise StaleProposal(
            f"state moved since proposal {proposal.id} was raised (proposed against "
            f"{proposal.state_hash[:12]}..., now {current_state_hash[:12]}...). Committing "
            "would overwrite newer data. Re-solve and re-propose."
        )

    return approval.model_copy(update={"revalidated_state_hash": current_state_hash})
