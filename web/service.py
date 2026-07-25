"""Turn a browser's field description into a drawn grid, a layout, and a dated plan.

This is a *demonstration* surface over the Phase 0 domain code. It calls the real
generator, the real pydantic models and the real parameter store — nothing here
recomputes geometry or invents a number. Two things it deliberately does not do:

* It does not optimise. There is no solver in Phase 0, so a layout here is a static
  pattern applied by a rule the caller chose, and the response says so in
  ``layout.is_optimised``.
* It does not report a spray count. Building an :class:`ObjectiveOutcome` would mean
  inventing ``expected_applications``, and a fabricated headline number is the exact
  failure the domain models are built to prevent. The response carries the reason
  instead of the figure.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from shapely.geometry import LineString

from intercrop.domain.crops import (
    CropRole,
    PatternComponent,
    PatternGeometry,
    RowPatternTemplate,
)
from intercrop.domain.field import Exclusion, ExclusionKind, Field_
from intercrop.domain.grid import GridSpec, ManagementGrid, SnapPolicy
from intercrop.domain.plan import ActionItem, ActionKind, ActionPlan, BlockAssignment
from intercrop.geometry import WGS84_EPSG, reproject, utm_epsg_for
from intercrop.grid.generator import build_grid, edge_adjacency, grid_summary, synthesise_rectangle
from intercrop.parameters.store import ParameterSet, load_default

ROOT = Path(__file__).resolve().parents[1]
PARAMS_DIR = ROOT / "params"

# Punjab tomato is the brief's worked example, so it is what the sheet opens on.
DEFAULT_LON, DEFAULT_LAT = 75.85, 30.90


# --------------------------------------------------------------------------------------
# Row patterns on offer
# --------------------------------------------------------------------------------------


def _solid_tomato() -> RowPatternTemplate:
    return RowPatternTemplate(
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
        notes="No companion. The comparison every other pattern is judged against.",
    )


def _tomato_garlic_bands() -> RowPatternTemplate:
    return RowPatternTemplate(
        code="tomato_garlic_4_2",
        name="Tomato : garlic bands, 4 rows to 2",
        geometry=PatternGeometry.ALTERNATING_BANDS,
        components=(
            PatternComponent(
                crop_slug="tomato",
                role=CropRole.MAIN,
                n_rows=4,
                row_spacing_m=1.5,
                in_row_spacing_m=0.4,
            ),
            PatternComponent(
                crop_slug="garlic",
                role=CropRole.COMPANION,
                n_rows=2,
                row_spacing_m=0.3,
                in_row_spacing_m=0.1,
            ),
        ),
        notes="Garlic is sown the previous autumn and must stand through aphid flight.",
    )


def _tomato_marigold_trap() -> RowPatternTemplate:
    return RowPatternTemplate(
        code="tomato_marigold_trap",
        name="Tomato with marigold trap perimeter",
        geometry=PatternGeometry.TRAP_PERIMETER,
        components=(
            PatternComponent(
                crop_slug="tomato",
                role=CropRole.MAIN,
                n_rows=1,
                row_spacing_m=1.5,
                in_row_spacing_m=0.4,
            ),
            PatternComponent(
                crop_slug="marigold",
                role=CropRole.TRAP,
                n_rows=3,
                row_spacing_m=0.6,
                in_row_spacing_m=0.3,
            ),
        ),
        border_depth_m=3.0,
        notes="Trap crop, not a harvest. Its area is a pure cost and it is destroyed on time.",
    )


PATTERNS: dict[str, RowPatternTemplate] = {
    p.code: p for p in (_solid_tomato(), _tomato_garlic_bands(), _tomato_marigold_trap())
}

LAYOUTS: dict[str, dict[str, str]] = {
    "solid": {
        "name": "Tomato throughout",
        "note": "Every block solid tomato. The baseline.",
    },
    "bands": {
        "name": "Garlic bands everywhere",
        "note": "Every block banded 4 tomato rows to 2 garlic.",
    },
    "trap_edge": {
        "name": "Marigold on the field edge",
        "note": "Blocks with an open side get a trap perimeter; interior blocks stay solid.",
    },
    "trap_edge_bands_core": {
        "name": "Marigold edge, garlic core",
        "note": "Trap perimeter on edge blocks, banded garlic on the interior.",
    },
}

CROP_COLOURS = {
    "tomato": "#B4472C",
    "garlic": "#7C8B4E",
    "marigold": "#D69A2C",
}


class DemoInputError(ValueError):
    """The browser sent something the domain models refuse. Message is shown verbatim."""


# --------------------------------------------------------------------------------------
# Field construction
# --------------------------------------------------------------------------------------


def _track_exclusion(lon: float, lat: float, length_m: float, width_m: float,
                     epsg: int) -> Exclusion:
    """A farm track straight across the parcel, drawn as a centreline the way growers do."""
    centre = reproject_point(lon, lat, epsg)
    half = length_m / 2.0 + 5.0
    line = LineString([(centre[0] - half, centre[1] + width_m * 0.12),
                       (centre[0] + half, centre[1] - width_m * 0.12)])
    return Exclusion(
        kind=ExclusionKind.ROAD,
        geometry=dict(reproject(line, epsg, WGS84_EPSG).__geo_interface__),
        width_m=5.0,
        standoff_m=1.0,
        label="Farm track",
    )


def reproject_point(lon: float, lat: float, epsg: int) -> tuple[float, float]:
    from shapely.geometry import Point

    p = reproject(Point(lon, lat), WGS84_EPSG, epsg)
    return (float(p.x), float(p.y))


def _rings(geom: Any, ox: float, oy: float) -> list[list[list[float]]]:
    """Exterior rings in field-local metres, y kept north-up for the client to flip."""
    parts = list(geom.geoms) if geom.geom_type.startswith("Multi") else [geom]
    out: list[list[list[float]]] = []
    for part in parts:
        if part.is_empty or not hasattr(part, "exterior"):
            continue
        out.append([[round(x - ox, 2), round(y - oy, 2)] for x, y in part.exterior.coords])
    return out


# --------------------------------------------------------------------------------------
# Layout: a rule, not a solve
# --------------------------------------------------------------------------------------


def _assign_patterns(grid: ManagementGrid, layout: str) -> dict[str, str]:
    adjacency = edge_adjacency(grid)
    on_edge = {code for code, neighbours in adjacency.items() if len(neighbours) < 4}
    chosen: dict[str, str] = {}
    for block in grid.blocks:
        match layout:
            case "solid":
                chosen[block.code] = "solid_tomato"
            case "bands":
                chosen[block.code] = "tomato_garlic_4_2"
            case "trap_edge":
                chosen[block.code] = (
                    "tomato_marigold_trap" if block.code in on_edge else "solid_tomato"
                )
            case "trap_edge_bands_core":
                chosen[block.code] = (
                    "tomato_marigold_trap" if block.code in on_edge else "tomato_garlic_4_2"
                )
            case _:
                raise DemoInputError(f"unknown layout {layout!r}")
    return chosen


def _areas_by_crop(grid: ManagementGrid, chosen: dict[str, str]) -> dict[str, float]:
    """Hectares per crop, from each pattern's own area split at this block size."""
    block_size = grid.spec.block_size_m
    totals: dict[str, float] = {}
    for block in grid.blocks:
        pattern = PATTERNS[chosen[block.code]]
        try:
            fractions = pattern.area_fraction(block_size)
        except ValueError as exc:
            raise DemoInputError(
                f"{pattern.name} does not fit a {block_size:g} m block: {exc}"
            ) from exc
        for slug, fraction in fractions.items():
            totals[slug] = totals.get(slug, 0.0) + fraction * block.plantable_area_ha
    return {slug: round(ha, 3) for slug, ha in sorted(totals.items())}


# --------------------------------------------------------------------------------------
# The dated plan
# --------------------------------------------------------------------------------------


def _threshold_note(pset: ParameterSet) -> str:
    """Quote the store rather than paraphrase it. Today the store holds a refusal."""
    record = pset.get("pest.myzus_persicae.economic_threshold")
    return (
        f"Parameter set v{pset.meta.version} holds {record.value!r} for the aphid action "
        "threshold. Until a local extension threshold is on file, scout to that guidance "
        "and treat on what you count, not on this window."
    )


def _action_plan(grid: ManagementGrid, chosen: dict[str, str], season: int,
                 pset: ParameterSet) -> tuple[ActionPlan, tuple[BlockAssignment, ...]]:
    all_blocks = tuple(b.code for b in grid.blocks)
    garlic_blocks = tuple(
        c for c, p in sorted(chosen.items()) if p == "tomato_garlic_4_2"
    )
    trap_blocks = tuple(
        c for c, p in sorted(chosen.items()) if p == "tomato_marigold_trap"
    )

    gdd_keys = tuple(
        k for k in ("pest.myzus_persicae.gdd.base_temp_c",
                    "pest.helicoverpa_armigera.gdd.base_temp_c")
        if k in pset.records
    )

    items: list[ActionItem] = []

    if garlic_blocks:
        items.append(
            ActionItem(
                kind=ActionKind.PLANT,
                window_start=date(season - 1, 10, 15),
                window_end=date(season - 1, 11, 15),
                block_codes=garlic_blocks,
                crop_slug="garlic",
                role=CropRole.COMPANION,
                description="Sow garlic bands in the blocks that carry them",
                rationale="Garlic must be established before the tomato crop goes in; a "
                "spring sowing has no bulb and no standing canopy during aphid flight.",
            )
        )

    items.append(
        ActionItem(
            kind=ActionKind.LAND_PREP,
            window_start=date(season, 2, 1),
            window_end=date(season, 2, 28),
            block_codes=all_blocks,
            description="Plough, level and mark out the block grid on the ground",
            rationale="Block edges are whole implement passes, so they can be pegged from "
            "the headland and driven without a part-width pass.",
        )
    )

    if trap_blocks:
        items.append(
            ActionItem(
                kind=ActionKind.PLANT,
                window_start=date(season, 2, 20),
                window_end=date(season, 3, 5),
                block_codes=trap_blocks,
                crop_slug="marigold",
                role=CropRole.TRAP,
                description="Plant the marigold perimeter ahead of the tomato transplant",
                rationale="A trap perimeter has to be standing and attractive before the "
                "main crop is available, or it traps nothing.",
            )
        )

    items.append(
        ActionItem(
            kind=ActionKind.PLANT,
            window_start=date(season, 3, 1),
            window_end=date(season, 3, 15),
            block_codes=all_blocks,
            crop_slug="tomato",
            role=CropRole.MAIN,
            description="Transplant tomato",
            rationale="Soil warm enough to avoid transplant check; window kept short so "
            "the whole field passes through each growth stage together.",
        )
    )

    items.append(
        ActionItem(
            kind=ActionKind.SCOUT,
            window_start=date(season, 3, 20),
            window_end=date(season, 6, 30),
            block_codes=all_blocks,
            crop_slug="tomato",
            description="Walk every block weekly and count what you find",
            rationale="A threshold-based plan that never scouts is a calendar-spray plan.",
            threshold_note=_threshold_note(pset),
            depends_on_parameter_keys=gdd_keys,
        )
    )

    items.append(
        ActionItem(
            kind=ActionKind.EXPECTED_INTERVENTION,
            window_start=date(season, 5, 15),
            window_end=date(season, 6, 15),
            block_codes=all_blocks,
            crop_slug="tomato",
            description="Expect a treatment decision in this window",
            rationale="Placed on degree-day accumulation from the parameter set, both "
            "entries of which are provisional. This is a window to be ready in, not a date "
            "to spray on.",
            threshold_note=_threshold_note(pset),
            depends_on_parameter_keys=gdd_keys,
        )
    )

    if garlic_blocks:
        items.append(
            ActionItem(
                kind=ActionKind.REMOVE_COMPANION,
                window_start=date(season, 6, 1),
                window_end=date(season, 6, 20),
                block_codes=garlic_blocks,
                crop_slug="garlic",
                role=CropRole.COMPANION,
                description="Lift garlic",
                rationale="Lifting is a decision, not a given: the interplant effect stops "
                "the day the garlic leaves the field, and the modelled flight period is "
                "itself provisional. Lift late where the bulb can take it.",
            )
        )

    if trap_blocks:
        items.append(
            ActionItem(
                kind=ActionKind.DESTROY_TRAP_CROP,
                window_start=date(season, 6, 20),
                window_end=date(season, 7, 5),
                block_codes=trap_blocks,
                crop_slug="marigold",
                role=CropRole.TRAP,
                description="Destroy the marigold perimeter, do not let it stand",
                rationale="A trap crop left standing past its window becomes a nursery and "
                "returns the population to the main crop.",
            )
        )

    items.append(
        ActionItem(
            kind=ActionKind.HARVEST,
            window_start=date(season, 6, 1),
            window_end=date(season, 8, 31),
            block_codes=all_blocks,
            crop_slug="tomato",
            role=CropRole.MAIN,
            description="Pick tomato, block by block",
            rationale="Block-level picking keeps yield attributable to the pattern each "
            "block was planted with, which is what makes next season's comparison possible.",
        )
    )

    plant_dates = {"tomato": date(season, 3, 1)}
    assignments = tuple(
        BlockAssignment(
            block_code=block.code,
            pattern_code=chosen[block.code],
            planting_dates=(
                plant_dates
                | ({"garlic": date(season - 1, 10, 15)}
                   if chosen[block.code] == "tomato_garlic_4_2" else {})
                | ({"marigold": date(season, 2, 20)}
                   if chosen[block.code] == "tomato_marigold_trap" else {})
            ),
            removal_dates=(
                ({"garlic": date(season, 6, 1)}
                 if chosen[block.code] == "tomato_garlic_4_2" else {})
                | ({"marigold": date(season, 6, 20)}
                   if chosen[block.code] == "tomato_marigold_trap" else {})
            ),
        )
        for block in grid.blocks
    )
    return ActionPlan(items=tuple(items)), assignments


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def _f(payload: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(payload.get(key, default))
    except (TypeError, ValueError):
        raise DemoInputError(f"{key} must be a number, got {payload.get(key)!r}") from None


def build_plate(payload: dict[str, Any]) -> dict[str, Any]:
    """Everything the sheet draws, from one description of a field."""
    lon = _f(payload, "lon", DEFAULT_LON)
    lat = _f(payload, "lat", DEFAULT_LAT)
    length_m = _f(payload, "length_m", 600.0)
    width_m = _f(payload, "width_m", 400.0)
    field_azimuth = _f(payload, "field_azimuth_deg", 20.0)
    season = int(_f(payload, "season_year", 2026))
    layout = str(payload.get("layout", "trap_edge_bands_core"))
    if layout not in LAYOUTS:
        raise DemoInputError(f"unknown layout {layout!r}")

    if length_m <= 0 or width_m <= 0:
        raise DemoInputError("field length and width must both be greater than zero")

    epsg = utm_epsg_for(lon, lat)
    boundary = synthesise_rectangle(lon, lat, length_m, width_m, field_azimuth)
    exclusions: tuple[Exclusion, ...] = ()
    if payload.get("include_track"):
        exclusions = (_track_exclusion(lon, lat, length_m, width_m, epsg),)

    field = Field_.from_boundary(
        str(payload.get("field_name") or "Sheet 1"),
        boundary,
        exclusions=exclusions,
        region_code="IN-PB",
        boundary_source="demo_synthesised",
    )

    override = payload.get("azimuth_override_deg")
    spec = GridSpec(
        requested_block_size_m=_f(payload, "requested_block_size_m", 50.0),
        implement_width_m=_f(payload, "implement_width_m", 1.8),
        snap_policy=SnapPolicy(str(payload.get("snap_policy", "nearest"))),
        headland_multiple=_f(payload, "headland_multiple", 2.0),
        min_plantable_fraction=_f(payload, "min_plantable_fraction", 0.25),
        azimuth_override_deg=None if override in (None, "", "auto") else float(override),
    )

    grid = build_grid(field, spec)
    summary = grid_summary(field, grid)
    chosen = _assign_patterns(grid, layout)
    areas = _areas_by_crop(grid, chosen)

    pset = load_default(PARAMS_DIR)
    plan, assignments = _action_plan(grid, chosen, season, pset)

    boundary_m = field.boundary_m()
    ox, oy, mx, my = boundary_m.bounds
    plantable = boundary_m.buffer(-spec.headland_depth_m) if spec.headland_depth_m else boundary_m
    footprint = field.exclusion_footprint_m()

    used_keys = tuple(sorted({
        key for item in plan.items for key in item.depends_on_parameter_keys
    }))

    return {
        "field": {
            "name": field.name,
            "gross_area_ha": round(field.gross_area_ha, 3),
            "working_crs_epsg": field.working_crs_epsg,
            "crs_rationale": field.crs_rationale,
            "crs_warnings": list(field.crs_warnings),
            "boundary_source": field.boundary_source,
            "region_code": field.region_code,
        },
        "extent": {"width_m": round(mx - ox, 2), "height_m": round(my - oy, 2)},
        "draw": {
            "boundary": _rings(boundary_m, ox, oy),
            "plantable": _rings(plantable, ox, oy),
            "exclusions": _rings(footprint.intersection(boundary_m), ox, oy) if footprint else [],
        },
        "grid": {
            **summary,
            "blocks": [
                {
                    "code": b.code,
                    "row": b.row_index,
                    "col": b.col_index,
                    "rings": _rings(b.geometry_m(field.working_crs_epsg), ox, oy),
                    "plantable_ha": round(b.plantable_area_ha, 3),
                    "plantable_fraction": round(b.plantable_fraction, 3),
                    "is_partial": b.is_partial,
                    "pattern_code": chosen[b.code],
                }
                for b in grid.blocks
            ],
        },
        "layout": {
            "code": layout,
            "name": LAYOUTS[layout]["name"],
            "note": LAYOUTS[layout]["note"],
            "is_optimised": False,
            "patterns": [
                {
                    "code": p.code,
                    "name": p.name,
                    "geometry": str(p.geometry),
                    "repeat_width_m": round(p.repeat_width_m, 2),
                    "notes": p.notes,
                    "block_count": sum(1 for c in chosen.values() if c == p.code),
                    "components": [
                        {"crop_slug": c.crop_slug, "role": str(c.role), "n_rows": c.n_rows,
                         "row_spacing_m": c.row_spacing_m}
                        for c in p.components
                    ],
                }
                for p in PATTERNS.values()
                if any(c == p.code for c in chosen.values())
            ],
            "area_ha_by_crop": areas,
            "assignment_count": len(assignments),
        },
        "crop_colours": CROP_COLOURS,
        "plan": [
            {
                "kind": str(item.kind),
                "window_start": item.window_start.isoformat(),
                "window_end": item.window_end.isoformat(),
                "block_count": len(item.block_codes),
                "crop_slug": item.crop_slug,
                "description": item.description,
                "rationale": item.rationale,
                "is_advisory": item.is_advisory,
                "threshold_note": item.threshold_note,
                "depends_on_parameter_keys": list(item.depends_on_parameter_keys),
            }
            for item in plan.in_order()
        ],
        "objective": {
            "expected_applications": None,
            "why_absent": "No pressure model exists in Phase 0, so the number of "
            "applications this layout would save has not been computed. Every interaction "
            "coefficient on file is provisional and unsourced; a figure here would be "
            "invented, and ObjectiveOutcome exists precisely to stop that being shown as a "
            "result.",
        },
        "provenance": {
            "parameter_set_version": pset.meta.version,
            "parameter_set_hash": pset.content_hash()[:12],
            "record_count": len(pset.records),
            "interaction_count": len(pset.interactions),
            "provisional_count": pset.unsourced_count,
            "used_keys": list(used_keys),
            "grid_generator_version": grid.generator_version,
            "grid_content_hash": grid.content_hash()[:12],
        },
    }


def parameter_register() -> dict[str, Any]:
    """The whole store, for the register panel. Status is the point, not the values."""
    pset = load_default(PARAMS_DIR)
    return {
        "version": pset.meta.version,
        "description": pset.meta.description,
        "hash": pset.content_hash()[:12],
        "records": [
            {
                "key": r.key,
                "value": r.value,
                "units": r.units,
                "status": str(r.status),
                "is_provisional": r.is_provisional,
                "rationale": (r.provisional_rationale or "").strip(),
                "citation_count": len(r.citations),
                "scope": list(r.validity.geographic_scope),
            }
            for r in sorted(pset.records.values(), key=lambda r: r.key)
        ],
        "interactions": [
            {
                "key": i.key,
                "source_crop": i.source_crop_slug,
                "target_crop": i.target_crop_slug,
                "pest": i.pest_slug,
                "mechanism": str(i.mechanism),
                "effect_size": i.effect_size,
                "effect_measure": str(i.effect_measure),
                "kernel": i.kernel.model_dump(mode="json"),
                "status": str(i.status),
                "is_provisional": i.is_provisional,
                "rationale": (i.provisional_rationale or "").strip(),
                "citation_count": len(i.citations),
            }
            for i in sorted(pset.interactions, key=lambda i: i.key)
        ],
    }
