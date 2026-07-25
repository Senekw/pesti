"""Row-pattern geometry, spec intake bookkeeping, and plan-record invariants."""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from intercrop.domain.crops import (
    CropRole,
    PatternComponent,
    PatternGeometry,
    PlantingWindow,
    RowPatternTemplate,
)
from intercrop.domain.plan import (
    ActionItem,
    ActionKind,
    ActionPlan,
    BlockAssignment,
    ObjectiveOutcome,
    Plan,
    PlanRevision,
    PlanStatus,
    SolverProvenance,
)
from intercrop.domain.spec import (
    CropRequest,
    FarmSpec,
    InterventionCap,
    Location,
    StatedDimensions,
    UploadedBoundary,
)
from intercrop.provenance import Provenance, Sourced

# --------------------------------------------------------------------------------------
# Row patterns
# --------------------------------------------------------------------------------------


def bands_4_2(tomato_spacing: float = 1.5, garlic_spacing: float = 0.3) -> RowPatternTemplate:
    """The brief's 4:2 tomato:garlic band pattern."""
    return RowPatternTemplate(
        code="4:2_tomato_garlic",
        name="Four tomato rows to two garlic rows",
        geometry=PatternGeometry.ALTERNATING_BANDS,
        components=(
            PatternComponent(
                crop_slug="tomato",
                role=CropRole.MAIN,
                n_rows=4,
                row_spacing_m=tomato_spacing,
                in_row_spacing_m=0.4,
            ),
            PatternComponent(
                crop_slug="garlic",
                role=CropRole.COMPANION,
                n_rows=2,
                row_spacing_m=garlic_spacing,
                in_row_spacing_m=0.1,
            ),
        ),
    )


def test_band_area_fractions_follow_band_widths() -> None:
    pattern = bands_4_2()
    # 4 x 1.5 = 6.0 m tomato, 2 x 0.3 = 0.6 m garlic, 6.6 m repeat.
    fractions = pattern.area_fraction(block_size_m=50.4)
    assert fractions["tomato"] == pytest.approx(6.0 / 6.6)
    assert fractions["garlic"] == pytest.approx(0.6 / 6.6)
    assert sum(fractions.values()) == pytest.approx(1.0)


def test_same_ratio_at_different_spacing_gives_different_protection_distance() -> None:
    """Why a pattern cannot be stored as a bare ratio.

    A 4:2 pattern is 4:2 at any spacing, but the distance from the worst-placed tomato row to
    the nearest garlic row is what the decay kernel consumes — and that changes with spacing.
    """
    wide = bands_4_2(tomato_spacing=1.5)
    narrow = bands_4_2(tomato_spacing=0.9)

    wide_distance = wide.mean_distance_to_component_m("tomato", "garlic")
    narrow_distance = narrow.mean_distance_to_component_m("tomato", "garlic")

    assert wide_distance == pytest.approx(6.0 / 4.0)  # 1.5 m
    assert narrow_distance == pytest.approx(3.6 / 4.0)  # 0.9 m
    assert narrow_distance < wide_distance

    # And with the shipped 1.5 m threshold kernel, that difference is the difference between
    # protection and none.
    from intercrop.parameters.store import load_default

    (coefficient,) = load_default("params").find_interactions(
        source_crop="garlic", target_crop="tomato"
    )
    assert coefficient.effect_at(narrow_distance) > 0.0
    assert coefficient.effect_at(wide_distance * 2) == 0.0


def test_solid_pattern_offers_no_companion_distance() -> None:
    solid = RowPatternTemplate(
        code="solid_tomato",
        name="Solid tomato",
        geometry=PatternGeometry.SOLID,
        components=(
            PatternComponent(
                crop_slug="tomato",
                role=CropRole.MAIN,
                n_rows=1,
                row_spacing_m=1.5,
                in_row_spacing_m=0.4,
            ),
        ),
    )
    assert solid.area_fraction(50.4) == {"tomato": 1.0}
    assert solid.mean_distance_to_component_m("tomato", "garlic") == float("inf")


def test_solid_pattern_rejects_two_components() -> None:
    with pytest.raises(ValueError, match="exactly one component"):
        RowPatternTemplate(
            code="bad_solid",
            name="Bad",
            geometry=PatternGeometry.SOLID,
            components=bands_4_2().components,
        )


def test_alternating_bands_needs_two_components() -> None:
    with pytest.raises(ValueError, match="at least two components"):
        RowPatternTemplate(
            code="bad_bands",
            name="Bad",
            geometry=PatternGeometry.ALTERNATING_BANDS,
            components=(
                PatternComponent(
                    crop_slug="tomato",
                    role=CropRole.MAIN,
                    n_rows=1,
                    row_spacing_m=1.5,
                    in_row_spacing_m=0.4,
                ),
            ),
        )


def border_pattern(depth: float = 3.0) -> RowPatternTemplate:
    return RowPatternTemplate(
        code="garlic_border",
        name="Garlic border strip around solid tomato",
        geometry=PatternGeometry.BORDER_ONLY,
        border_depth_m=depth,
        components=(
            PatternComponent(
                crop_slug="tomato",
                role=CropRole.MAIN,
                n_rows=1,
                row_spacing_m=1.5,
                in_row_spacing_m=0.4,
            ),
            PatternComponent(
                crop_slug="garlic",
                role=CropRole.COMPANION,
                n_rows=10,
                row_spacing_m=0.3,
                in_row_spacing_m=0.1,
            ),
        ),
    )


def test_border_area_fraction_depends_on_block_size() -> None:
    """Border economics are not scale-free: block size and template must be solved together."""
    pattern = border_pattern(depth=3.0)
    small = pattern.area_fraction(50.4)["garlic"]
    large = pattern.area_fraction(100.8)["garlic"]
    assert small > large
    assert small == pytest.approx((50.4**2 - 44.4**2) / 50.4**2)


def test_border_depth_must_fit_the_block() -> None:
    with pytest.raises(ValueError, match="cannot fit twice"):
        border_pattern(depth=30.0).area_fraction(50.4)


def test_strip_patterns_require_a_border_depth() -> None:
    with pytest.raises(ValueError, match="requires border_depth_m"):
        RowPatternTemplate(
            code="no_depth",
            name="Bad",
            geometry=PatternGeometry.BORDER_ONLY,
            components=border_pattern().components,
        )


def test_band_distance_helper_refuses_strip_geometry() -> None:
    """The block-size-free helper must not silently answer for a geometry it cannot."""
    with pytest.raises(NotImplementedError, match="depends on block size"):
        border_pattern().mean_distance_to_component_m("tomato", "garlic")


def test_trap_and_insectary_area_is_not_counted_as_saleable() -> None:
    """Otherwise the optimizer satisfies a tomato contract with a crop nobody buys."""
    trap = RowPatternTemplate(
        code="trap_perimeter",
        name="Trap perimeter",
        geometry=PatternGeometry.TRAP_PERIMETER,
        border_depth_m=3.0,
        components=(
            PatternComponent(
                crop_slug="tomato",
                role=CropRole.MAIN,
                n_rows=30,
                row_spacing_m=1.5,
                in_row_spacing_m=0.4,
            ),
            PatternComponent(
                crop_slug="marigold",
                role=CropRole.TRAP,
                n_rows=4,
                row_spacing_m=0.5,
                in_row_spacing_m=0.3,
            ),
        ),
    )
    assert trap.yields_saleable < 1.0
    assert bands_4_2().yields_saleable == pytest.approx(1.0)  # both crops sell


# --------------------------------------------------------------------------------------
# Planting windows
# --------------------------------------------------------------------------------------


def test_autumn_planting_window_wraps_the_year_end() -> None:
    """Garlic: mid-October to mid-November is not earliest <= latest."""
    window = PlantingWindow(region_code="IN-PB", earliest_doy=288, latest_doy=325)
    assert not window.wraps_year_end

    wrapping = PlantingWindow(region_code="IN-PB", earliest_doy=320, latest_doy=20)
    assert wrapping.wraps_year_end
    assert wrapping.contains(350)
    assert wrapping.contains(10)
    assert not wrapping.contains(180)


# --------------------------------------------------------------------------------------
# Spec intake
# --------------------------------------------------------------------------------------


def test_new_spec_reports_what_it_still_needs() -> None:
    spec = FarmSpec()
    missing = spec.missing_required()
    assert "location" in missing
    assert "boundary_intent" in missing
    assert "crops" in missing
    assert not spec.is_solvable


def test_spec_becomes_solvable_once_required_fields_land() -> None:
    spec = FarmSpec(
        location=Sourced[Location].stated(
            Location(lon=75.85, lat=30.90, region_code="IN-PB", precision="district"),
            utterance="Northern India, Punjab",
        ),
        boundary_intent=Sourced[StatedDimensions].stated(
            StatedDimensions(length_m=1000.0, width_m=1000.0),
            utterance="I have a 1 km by 1 km farm",
        ),
        implement_width_m=Sourced[float].stated(1.8, utterance="1.8 metres"),
        crops=(
            CropRequest(
                crop_slug="tomato",
                role=CropRole.MAIN,
                min_area_ha=80.0,
                is_contracted=True,
                has_market=True,
            ),
        ),
    )
    assert spec.is_solvable
    assert spec.missing_required() == ()


def test_spec_enumerates_only_the_values_the_grower_did_not_state() -> None:
    """The payload of the CONFIRM turn."""
    spec = FarmSpec(
        location=Sourced[Location].stated(
            Location(lon=75.85, lat=30.90, region_code="IN-PB"), utterance="Punjab"
        ),
        last_frost_doy=Sourced[int].inferred(
            45, basis="Punjab last-frost date from regional normals"
        ),
        implement_width_m=Sourced[float].defaulted(
            1.8, basis="common tractor implement width; not stated by the grower"
        ),
    )
    flagged = dict(spec.assumptions())
    assert "last_frost_doy" in flagged
    assert "implement_width_m" in flagged
    assert "location" not in flagged, "the grower stated this; do not ask them to confirm it"
    assert "regional normals" in flagged["last_frost_doy"]


def test_contradictory_area_bounds_are_surfaced_not_reconciled() -> None:
    with pytest.raises(ValueError, match="contradiction to surface"):
        CropRequest(
            crop_slug="tomato", role=CropRole.MAIN, min_area_ha=80.0, max_area_ha=60.0
        )


def test_uploaded_boundary_is_never_marked_synthesised() -> None:
    """Once a real boundary exists, nothing downstream may treat the field as a square."""
    boundary = UploadedBoundary(
        boundary={
            "type": "Polygon",
            "coordinates": [
                [[75.8, 30.9], [75.81, 30.9], [75.81, 30.91], [75.8, 30.91], [75.8, 30.9]]
            ],
        }
    )
    assert boundary.is_synthesised is False
    assert StatedDimensions(length_m=1000.0, width_m=1000.0).is_synthesised is True


def test_grower_preference_is_not_a_hard_limit_by_default() -> None:
    """'I don't want to spray more than 3 times' is a goal, not an infeasibility."""
    cap = InterventionCap(max_applications=3)
    assert not cap.is_hard_limit


def test_unknown_provenance_counts_as_missing() -> None:
    spec = FarmSpec(
        implement_width_m=Sourced[float](value=1.8, provenance=Provenance.UNKNOWN),
    )
    assert "implement_width_m" in spec.missing_required()


# --------------------------------------------------------------------------------------
# Plan invariants
# --------------------------------------------------------------------------------------


def _outcome(**overrides: object) -> ObjectiveOutcome:
    defaults: dict[str, object] = {
        "expected_applications": 3.2,
        "main_crop_area_ha": 81.5,
        "companion_area_ha": {"garlic": 12.0},
        "requested_cap": 3.0,
        "area_floor_ha": 80.0,
        "shortfall_note": (
            "Modelled at 3.2 applications against your cap of 3. Reaching 3 requires "
            "dropping tomato to about 76 ha, below your 80 ha contract."
        ),
    }
    return ObjectiveOutcome(**(defaults | overrides))  # type: ignore[arg-type]


def test_exceeding_the_cap_without_admitting_it_is_rejected() -> None:
    """Enforces the brief's third rule at the type level."""
    with pytest.raises(ValueError, match="do not present this as a success"):
        _outcome(shortfall_note=None)


def test_exceeding_the_cap_with_an_honest_note_is_accepted() -> None:
    outcome = _outcome()
    assert not outcome.meets_cap
    assert "below your 80 ha contract" in (outcome.shortfall_note or "")


def test_meeting_the_cap_needs_no_note() -> None:
    outcome = ObjectiveOutcome(
        expected_applications=2.4, main_crop_area_ha=80.5, requested_cap=3.0
    )
    assert outcome.meets_cap
    assert outcome.shortfall_note is None


def test_infeasible_solve_must_explain_itself() -> None:
    with pytest.raises(ValueError, match="which constraints conflict"):
        SolverProvenance(solver_version="9.12", seed=7, status="INFEASIBLE", wall_time_s=1.0)


def test_infeasible_with_an_explanation_is_accepted() -> None:
    provenance = SolverProvenance(
        solver_version="9.12",
        seed=7,
        status="INFEASIBLE",
        wall_time_s=1.0,
        infeasibility_explanation=(
            "An 80 ha tomato floor and a 5 ha garlic ceiling cannot both hold: the garlic "
            "area needed to protect 80 ha of tomato is at least 9 ha.",
        ),
    )
    assert provenance.infeasibility_explanation


def test_expected_intervention_must_carry_its_scouting_threshold() -> None:
    """Decision support, not prescription."""
    with pytest.raises(ValueError, match="scouting threshold"):
        ActionItem(
            kind=ActionKind.EXPECTED_INTERVENTION,
            window_start=date(2026, 7, 8),
            window_end=date(2026, 7, 22),
            block_codes=("R03C12",),
            description="Expected aphid treatment window",
            rationale="Modelled peak flight",
        )


def test_expected_intervention_cannot_be_marked_non_advisory() -> None:
    with pytest.raises(ValueError, match="never be emitted as a prescription"):
        ActionItem(
            kind=ActionKind.EXPECTED_INTERVENTION,
            window_start=date(2026, 7, 8),
            window_end=date(2026, 7, 22),
            block_codes=("R03C12",),
            description="Treat for aphids",
            rationale="Modelled peak flight",
            threshold_note="Treat on local extension threshold, not on this date.",
            is_advisory=False,
        )


def test_action_must_name_its_blocks() -> None:
    with pytest.raises(ValueError, match="must name the blocks"):
        ActionItem(
            kind=ActionKind.PLANT,
            window_start=date(2026, 10, 20),
            window_end=date(2026, 11, 5),
            block_codes=(),
            description="Drill garlic",
            rationale="Autumn planting window",
        )


def test_removal_before_planting_is_rejected() -> None:
    with pytest.raises(ValueError, match="not after planting"):
        BlockAssignment(
            block_code="R03C12",
            pattern_code="4:2_tomato_garlic",
            planting_dates={"garlic": date(2026, 10, 20)},
            removal_dates={"garlic": date(2026, 6, 15)},
        )


def test_per_crop_dates_express_the_autumn_garlic_arrangement() -> None:
    """A single block-level planting date could not represent this at all."""
    assignment = BlockAssignment(
        block_code="R03C12",
        pattern_code="4:2_tomato_garlic",
        planting_dates={"garlic": date(2025, 10, 20), "tomato": date(2026, 2, 25)},
        removal_dates={"garlic": date(2026, 7, 20), "tomato": date(2026, 6, 30)},
    )
    assert assignment.planting_dates["garlic"] < assignment.planting_dates["tomato"]


def test_a_draft_plan_cannot_name_a_revision_of_record() -> None:
    """How an unapproved layout would otherwise get treated as committed."""
    with pytest.raises(ValueError, match="only a plan of record"):
        Plan(
            field_id=uuid.uuid4(),
            spec_id=uuid.uuid4(),
            season_year=2026,
            name="Punjab tomato 2026",
            status=PlanStatus.DRAFT,
            revision_of_record_id=uuid.uuid4(),
        )


def test_a_plan_of_record_must_point_at_its_approved_revision() -> None:
    with pytest.raises(ValueError, match="must point at the revision"):
        Plan(
            field_id=uuid.uuid4(),
            spec_id=uuid.uuid4(),
            season_year=2026,
            name="Punjab tomato 2026",
            status=PlanStatus.OF_RECORD,
        )


def _revision(**overrides: object) -> PlanRevision:
    defaults: dict[str, object] = {
        "plan_id": uuid.uuid4(),
        "revision_number": 1,
        "grid_content_hash": "g" * 64,
        "parameter_set_hash": "p" * 64,
        "constraint_set": {"tomato_min_ha": 80.0, "spray_cap": 3},
        "assignments": (
            BlockAssignment(block_code="R00C00", pattern_code="solid_tomato"),
            BlockAssignment(block_code="R00C01", pattern_code="4:2_tomato_garlic"),
        ),
        "outcome": _outcome(),
        "solver": SolverProvenance(
            solver_version="9.12", seed=7, status="FEASIBLE", wall_time_s=12.4
        ),
        "created_by": "intercrop-agent",
    }
    return PlanRevision(**(defaults | overrides))  # type: ignore[arg-type]


def test_a_block_cannot_receive_two_assignments() -> None:
    with pytest.raises(ValueError, match="cannot receive two assignments"):
        _revision(
            assignments=(
                BlockAssignment(block_code="R00C00", pattern_code="solid_tomato"),
                BlockAssignment(block_code="R00C00", pattern_code="4:2_tomato_garlic"),
            )
        )


def test_revision_state_hash_is_deterministic_and_sensitive() -> None:
    base = _revision()
    assert base.state_hash() == _revision().state_hash()

    changed = _revision(constraint_set={"tomato_min_ha": 76.0, "spray_cap": 3})
    assert changed.state_hash() != base.state_hash()

    regridded = _revision(grid_content_hash="h" * 64)
    assert regridded.state_hash() != base.state_hash()

    reparameterised = _revision(parameter_set_hash="q" * 64)
    assert reparameterised.state_hash() != base.state_hash()


def test_action_plan_counts_expected_interventions() -> None:
    plan = ActionPlan(
        items=(
            ActionItem(
                kind=ActionKind.PLANT,
                window_start=date(2025, 10, 20),
                window_end=date(2025, 11, 5),
                block_codes=("R00C00",),
                description="Drill garlic in border strips",
                rationale="Autumn planting window for Punjab garlic",
            ),
            ActionItem(
                kind=ActionKind.EXPECTED_INTERVENTION,
                window_start=date(2026, 7, 8),
                window_end=date(2026, 7, 22),
                block_codes=("R00C00",),
                description="Aphid treatment may be needed",
                rationale="Modelled peak flight overlapping standing garlic",
                threshold_note="Treat only on the local extension action threshold.",
            ),
        )
    )
    assert plan.expected_intervention_count == 1
    assert plan.in_order()[0].kind is ActionKind.PLANT
