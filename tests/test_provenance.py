"""Provenance tests.

The brief's requirement: any output containing a numeric efficacy claim traces to a parameter
with a non-null source. At Phase 0 there is no output layer yet, so what is testable — and
what these tests cover — is that the *representation* makes an unsourced published claim
impossible to construct, that provisional entries are enumerable, and that a parameter cannot
be used outside the envelope it was measured in.
"""

from __future__ import annotations

import pytest

from intercrop.domain.interactions import (
    EffectMeasure,
    ExponentialKernel,
    InteractionCoefficient,
    LinearTaperKernel,
    Mechanism,
    ThresholdKernel,
)
from intercrop.parameters.store import MissingParameter, ParameterSet, load_default
from intercrop.provenance import (
    Citation,
    CitationKind,
    OutOfValidityRange,
    ParameterRecord,
    ParameterStatus,
    Provenance,
    Sourced,
    ValidityRange,
)

REAL_CITATION = Citation(
    kind=CitationKind.PEER_REVIEWED,
    title="Example intercropping trial",
    year=2020,
    authors="Example, A.",
    doi="10.0000/example",
    locator="Table 2",
)


# --------------------------------------------------------------------------------------
# No unsourced published numbers
# --------------------------------------------------------------------------------------


def test_published_parameter_without_a_citation_is_rejected() -> None:
    with pytest.raises(ValueError, match="PUBLISHED with no citation"):
        ParameterRecord(
            key="pest.example.gdd.base_temp_c",
            value=10.0,
            units="degC",
            status=ParameterStatus.PUBLISHED,
        )


def test_published_parameter_with_a_citation_is_accepted() -> None:
    record = ParameterRecord(
        key="pest.example.gdd.base_temp_c",
        value=10.0,
        units="degC",
        status=ParameterStatus.PUBLISHED,
        citations=(REAL_CITATION,),
    )
    assert not record.is_provisional
    assert record.citations[0].doi == "10.0000/example"


def test_provisional_parameter_needs_a_rationale() -> None:
    with pytest.raises(ValueError, match="needs a rationale"):
        ParameterRecord(
            key="pest.example.gdd.base_temp_c",
            value=10.0,
            status=ParameterStatus.PROVISIONAL,
        )


def test_citation_without_doi_or_url_is_rejected() -> None:
    """A number nobody can look up is not sourced."""
    with pytest.raises(ValueError, match="not sourced"):
        Citation(kind=CitationKind.PEER_REVIEWED, title="Trust me", year=2021)


def test_expert_elicitation_must_name_the_expert() -> None:
    with pytest.raises(ValueError, match="must name the expert"):
        Citation(kind=CitationKind.EXPERT_ELICITATION, title="Local practice")

    ok = Citation(
        kind=CitationKind.EXPERT_ELICITATION,
        title="Local practice for Ludhiana tomato",
        authors="Dr Example, PAU Ludhiana",
    )
    assert ok.authors is not None


def test_deprecated_parameter_must_name_its_replacement() -> None:
    with pytest.raises(ValueError, match="must say what replaced it"):
        ParameterRecord(
            key="pest.example.old", value=1.0, status=ParameterStatus.DEPRECATED
        )


# --------------------------------------------------------------------------------------
# Validity range: refuse, do not extrapolate
# --------------------------------------------------------------------------------------


def test_parameter_outside_its_geographic_scope_raises() -> None:
    record = ParameterRecord(
        key="region.example.last_frost_doy",
        value=45,
        status=ParameterStatus.PUBLISHED,
        citations=(REAL_CITATION,),
        validity=ValidityRange(geographic_scope=("IN-PB",)),
    )
    record.check_applicable({"region": "IN-PB"})  # in range: no raise
    with pytest.raises(OutOfValidityRange, match="does not apply here"):
        record.check_applicable({"region": "BR-SP"})


def test_parameter_outside_a_numeric_bound_raises() -> None:
    record = ParameterRecord(
        key="interaction.example.effect",
        value=0.3,
        status=ParameterStatus.PUBLISHED,
        citations=(REAL_CITATION,),
        validity=ValidityRange(numeric_bounds={"row_spacing_m": (0.9, 1.8)}),
    )
    record.check_applicable({"row_spacing_m": 1.5})
    with pytest.raises(OutOfValidityRange, match="outside measured range"):
        record.check_applicable({"row_spacing_m": 3.0})


def test_greenhouse_coefficient_refuses_to_speak_about_open_field() -> None:
    """The failure mode the brief calls the easiest way to hurt someone."""
    record = ParameterRecord(
        key="interaction.example.effect",
        value=0.6,
        status=ParameterStatus.PUBLISHED,
        citations=(REAL_CITATION,),
        validity=ValidityRange(cropping_system=("protected", "greenhouse")),
    )
    with pytest.raises(OutOfValidityRange):
        record.check_applicable({"cropping_system": "open_field"})


def test_deprecated_parameter_refuses_use() -> None:
    record = ParameterRecord(
        key="pest.example.old",
        value=1.0,
        status=ParameterStatus.DEPRECATED,
        superseded_by="pest.example.new",
    )
    with pytest.raises(OutOfValidityRange, match="deprecated"):
        record.check_applicable({})


def test_missing_context_key_does_not_silently_pass_as_in_range() -> None:
    """A gap is reported as a gap by the caller, not treated as compliance.

    ``violations`` ignores absent keys by design, which is why computation must go through
    ``ParameterSet.require`` with a fully populated context. This test pins the documented
    behaviour so a future change to it is deliberate.
    """
    record = ParameterRecord(
        key="interaction.example.effect",
        value=0.3,
        status=ParameterStatus.PROVISIONAL,
        provisional_rationale="placeholder",
        validity=ValidityRange(numeric_bounds={"row_spacing_m": (0.9, 1.8)}),
    )
    assert record.validity.violations({}) == []
    assert record.validity.violations({"row_spacing_m": 5.0}) != []


# --------------------------------------------------------------------------------------
# Effect measures are not interchangeable
# --------------------------------------------------------------------------------------


def _coefficient(measure: EffectMeasure, size: float) -> InteractionCoefficient:
    return InteractionCoefficient(
        key=f"garlic>tomato:aphid:{measure}",
        source_crop_slug="garlic",
        target_crop_slug="tomato",
        pest_slug="myzus_persicae",
        mechanism=Mechanism.REPELLENCY,
        effect_size=size,
        effect_measure=measure,
        kernel=ThresholdKernel(radius_m=1.5),
        status=ParameterStatus.PROVISIONAL,
        provisional_rationale="test fixture",
    )


def test_hedges_g_refuses_conversion_to_a_proportion() -> None:
    """Reading a standardised mean difference as a percentage fabricates an efficacy claim."""
    coefficient = _coefficient(EffectMeasure.HEDGES_G, 0.8)
    with pytest.raises(ValueError, match="no distance-free conversion"):
        coefficient.proportional_reduction()


def test_log_response_ratio_converts_correctly() -> None:
    coefficient = _coefficient(EffectMeasure.LOG_RESPONSE_RATIO, -0.3567)
    assert coefficient.proportional_reduction() == pytest.approx(0.30, abs=0.005)


def test_percent_reduction_converts_correctly() -> None:
    assert _coefficient(EffectMeasure.PERCENT_REDUCTION, 30.0).proportional_reduction() == (
        pytest.approx(0.30)
    )


def test_proportional_reduction_outside_minus_one_to_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="proportional reduction must be"):
        _coefficient(EffectMeasure.PROPORTIONAL_REDUCTION, 1.5)


# --------------------------------------------------------------------------------------
# Kernels
# --------------------------------------------------------------------------------------


def test_threshold_kernel_is_a_step() -> None:
    kernel = ThresholdKernel(radius_m=1.5)
    assert kernel.evaluate(0.0) == 1.0
    assert kernel.evaluate(1.5) == 1.0
    assert kernel.evaluate(1.51) == 0.0


def test_exponential_kernel_decays_and_honours_its_cutoff() -> None:
    kernel = ExponentialKernel(scale_m=2.0, cutoff_m=10.0)
    assert kernel.evaluate(0.0) == pytest.approx(1.0)
    assert kernel.evaluate(2.0) == pytest.approx(0.3679, abs=1e-3)
    assert kernel.evaluate(10.01) == 0.0, "the cutoff exists to stop a phantom tail summing"


def test_linear_taper_interpolates() -> None:
    kernel = LinearTaperKernel(full_m=15.0, zero_m=60.0)
    assert kernel.evaluate(10.0) == 1.0
    assert kernel.evaluate(37.5) == pytest.approx(0.5)
    assert kernel.evaluate(60.0) == 0.0


def test_linear_taper_requires_ordered_bounds() -> None:
    with pytest.raises(ValueError, match="zero_m must exceed full_m"):
        LinearTaperKernel(full_m=60.0, zero_m=15.0)


def test_kernels_reject_negative_distance() -> None:
    for kernel in (
        ThresholdKernel(radius_m=1.0),
        ExponentialKernel(scale_m=1.0),
        LinearTaperKernel(full_m=1.0, zero_m=2.0),
    ):
        with pytest.raises(ValueError, match="cannot be negative"):
            kernel.evaluate(-1.0)


# --------------------------------------------------------------------------------------
# Interaction coherence
# --------------------------------------------------------------------------------------


def test_pest_mediated_mechanism_requires_a_pest() -> None:
    with pytest.raises(ValueError, match="requires a pest_slug"):
        InteractionCoefficient(
            key="garlic>tomato:repellency",
            source_crop_slug="garlic",
            target_crop_slug="tomato",
            mechanism=Mechanism.REPELLENCY,
            effect_size=0.3,
            effect_measure=EffectMeasure.PROPORTIONAL_REDUCTION,
            kernel=ThresholdKernel(radius_m=1.5),
            status=ParameterStatus.PROVISIONAL,
            provisional_rationale="test",
        )


def test_allelopathy_must_not_name_a_pest() -> None:
    with pytest.raises(ValueError, match="crop-on-crop"):
        InteractionCoefficient(
            key="garlic>bean:allelopathy",
            source_crop_slug="garlic",
            target_crop_slug="bean",
            pest_slug="myzus_persicae",
            mechanism=Mechanism.ALLELOPATHY,
            effect_size=-0.2,
            effect_measure=EffectMeasure.PROPORTIONAL_REDUCTION,
            kernel=ThresholdKernel(radius_m=1.0),
            status=ParameterStatus.PROVISIONAL,
            provisional_rationale="test",
        )


def test_allelopathy_and_competition_are_not_beneficial() -> None:
    assert not Mechanism.ALLELOPATHY.is_beneficial
    assert not Mechanism.RESOURCE_COMPETITION.is_beneficial
    assert Mechanism.REPELLENCY.is_beneficial
    assert Mechanism.NATURAL_ENEMY_PROVISIONING.is_beneficial


# --------------------------------------------------------------------------------------
# Sourced spec values
# --------------------------------------------------------------------------------------


def test_inferred_value_without_a_basis_is_rejected() -> None:
    with pytest.raises(ValueError, match="requires a 'basis'"):
        Sourced[int](value=45, provenance=Provenance.INFERRED)


def test_stated_values_need_no_confirmation_and_defaults_do() -> None:
    stated = Sourced[int].stated(80, utterance="I need at least 80 ha in tomatoes")
    assert not stated.needs_confirmation

    inferred = Sourced[int].inferred(45, basis="Punjab last frost from regional normals")
    assert inferred.needs_confirmation

    defaulted = Sourced[float].defaulted(50.0, basis="cold-start default block size")
    assert defaulted.needs_confirmation


# --------------------------------------------------------------------------------------
# The shipped parameter set
# --------------------------------------------------------------------------------------


def test_shipped_parameter_set_loads_and_hashes_stably() -> None:
    first = load_default("params")
    second = load_default("params")
    assert first.content_hash() == second.content_hash()
    assert len(first.content_hash()) == 64


def test_every_shipped_entry_is_currently_provisional() -> None:
    """Phase 0 ships zero sourced agronomy, and says so rather than implying otherwise.

    This test is expected to change in Phase 1 as entries acquire citations. It exists so
    that transition is deliberate and visible in a diff.
    """
    pset = load_default("params")
    assert pset.unsourced_count == len(pset.records) + len(pset.interactions)
    for record in pset.records.values():
        assert record.status is ParameterStatus.PROVISIONAL
        assert record.provisional_rationale


def test_missing_parameter_raises_rather_than_defaulting() -> None:
    pset = load_default("params")
    with pytest.raises(MissingParameter) as excinfo:
        pset.get("pest.invented.gdd.base_temp_c")
    message = str(excinfo.value)
    assert "not been sourced" in message
    assert "do not substitute" in message


def test_parameter_set_has_no_default_returning_lookup() -> None:
    """Guard against someone adding a convenience ``get_or_default``."""
    forbidden = {"get_or_default", "get_default", "value_or", "fetch_or"}
    assert not forbidden & set(dir(ParameterSet))


def test_finding_no_interaction_returns_empty_rather_than_inventing_one() -> None:
    pset = load_default("params")
    assert pset.find_interactions(source_crop="marigold", target_crop="tomato") == ()


def test_garlic_tomato_interaction_is_present_and_temporally_gated() -> None:
    """The worked example, and the property that makes the June-lift case work."""
    pset = load_default("params")
    (coefficient,) = pset.find_interactions(
        source_crop="garlic", target_crop="tomato", pest="myzus_persicae"
    )
    assert coefficient.mechanism is Mechanism.REPELLENCY
    assert coefficient.temporal.requires_co_occupancy
    assert coefficient.temporal.effect_persists_after_removal_days == 0
    assert coefficient.temporal.min_overlap_days > 0


def test_natural_enemy_kernel_is_wider_than_the_repellency_kernel() -> None:
    """Different mechanisms, different spatial ranges. Interchanging them is a modelling error."""
    pset = load_default("params")
    (repellency,) = pset.find_interactions(mechanism="repellency", source_crop="garlic")
    (insectary,) = pset.find_interactions(mechanism="natural_enemy_provisioning")

    assert repellency.effect_at(5.0) == 0.0, "repellency should not reach 5 m"
    assert insectary.effect_at(5.0) > 0.0, "natural-enemy effects act over tens of metres"
