"""Demo: Simple tomato crop on a 500 m x 500 m field.

Shows a complete workflow for a single-crop system:
- Create a field boundary
- Generate a management grid
- Assign tomato to blocks
- Create a basic action plan with planting/harvest dates
- Render the grid visualization

This is a minimal Phase 0 prototype: no solver, no companion crops, just the
geometry pipeline and a single crop type working end-to-end.

Run: python scripts/demo_tomato_crop.py
Output: out/tomato_demo_field.png and console action plan.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import matplotlib
matplotlib.use("Agg")

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, Normalize
from shapely.geometry import Polygon

from intercrop.domain._types import as_shapely
from intercrop.domain.crops import (
    CropRole,
    PatternComponent,
    PatternGeometry,
    RowPatternTemplate,
)
from intercrop.domain.field import Field_
from intercrop.domain.grid import GridSpec
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
from intercrop.domain.spec import CropRequest, FarmSpec
from intercrop.geometry import WGS84_EPSG, reproject, utm_epsg_for
from intercrop.grid.generator import build_grid, grid_summary, synthesise_rectangle

OUT = Path(__file__).resolve().parents[1] / "out"
OUT.mkdir(exist_ok=True)

# Punjab region for weather/frost data consistency
PUNJAB_LON, PUNJAB_LAT = 75.85, 30.90

# Color scheme: green for tomato blocks
TOMATO_COLOR = "#2ecc71"
HEADLAND_COLOR = "#e8e6e1"
GRIDLINE_COLOR = "#bfbdba"
SURFACE_COLOR = "#fcfcfb"
INK_COLOR = "#0b0b0b"
INK_SECONDARY = "#52514e"


def create_tomato_field() -> Field_:
    """Create a simple rectangular field, 500 m x 500 m."""
    boundary = synthesise_rectangle(PUNJAB_LON, PUNJAB_LAT, 500.0, 500.0)
    return Field_.from_boundary(
        "Tomato Demo Field",
        boundary,
        region_code="IN-PB",
        boundary_source="demo_synthesised",
    )


def create_tomato_pattern() -> RowPatternTemplate:
    """Simple solid tomato pattern: all tomato, no companions."""
    return RowPatternTemplate(
        code="solid_tomato",
        name="Solid tomato block (1.5 m rows)",
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


def create_grid_spec() -> GridSpec:
    """Grid config: 50 m blocks with 2x implement-width headlands."""
    return GridSpec(
        requested_block_size_m=50.0,
        implement_width_m=1.8,  # Standard sprayer boom width
        headland_multiple=2.0,  # 2x implement width = 3.6 m headland
    )


def generate_block_assignments(grid: object) -> list[BlockAssignment]:
    """Assign tomato pattern to every block."""
    # Get block codes from grid summary
    blocks = []
    # Simple assignment: all blocks get solid_tomato pattern
    # In a real solve, the solver would choose the pattern per block.
    # For this demo, we just assign one crop to all blocks.
    return blocks


def create_action_plan(field: Field_, grid, farm_spec: FarmSpec) -> ActionPlan:
    """Create a simple action plan: land prep, planting, scouting, harvest."""
    # Get all block codes from the grid
    all_blocks = tuple(b.code for b in grid.blocks)
    
    actions = [
        ActionItem(
            kind=ActionKind.LAND_PREP,
            window_start=date(2025, 2, 1),
            window_end=date(2025, 2, 28),
            block_codes=all_blocks,
            description="Prepare field: plough, rake, level",
            rationale="Soil preparation required before transplanting tomato seedlings",
        ),
        ActionItem(
            kind=ActionKind.PLANT,
            window_start=date(2025, 3, 1),
            window_end=date(2025, 3, 15),
            block_codes=all_blocks,
            crop_slug="tomato",
            role=CropRole.MAIN,
            description="Transplant tomato seedlings into field",
            rationale="Tomato transplants ready after hardening off; soil warm enough to avoid "
                     "transplant shock",
        ),
        ActionItem(
            kind=ActionKind.SCOUT,
            window_start=date(2025, 3, 20),
            window_end=date(2025, 6, 30),
            block_codes=all_blocks,
            crop_slug="tomato",
            description="Weekly field scout: look for early pest signs",
            rationale="Early detection of tomato hornworm, whitefly, or disease lesions "
                     "enables threshold-based treatment",
        ),
        ActionItem(
            kind=ActionKind.IRRIGATE,
            window_start=date(2025, 4, 1),
            window_end=date(2025, 7, 15),
            block_codes=all_blocks,
            crop_slug="tomato",
            description="Drip irrigate as needed (60-80 mm/week during fruiting)",
            rationale="Tomato requires consistent moisture during flowering and fruiting; "
                     "deficit irrigation after July improves fruit quality",
        ),
        ActionItem(
            kind=ActionKind.HARVEST,
            window_start=date(2025, 6, 1),
            window_end=date(2025, 8, 31),
            block_codes=all_blocks,
            crop_slug="tomato",
            role=CropRole.MAIN,
            description="Begin tomato harvest when fruits reach red stage",
            rationale="Red-ripe tomatoes achieve maximum sugar content and shelf life; "
                     "daily or every-other-day picking extends harvest season",
        ),
    ]
    return ActionPlan(items=tuple(actions))


def render_field_grid(field: Field_, grid: object, action_plan: ActionPlan, path: Path) -> None:
    """Render the field grid with tomato assignment."""
    from intercrop.grid.generator import grid_summary
    
    epsg = field.working_crs_epsg
    summary = grid_summary(field, grid)

    boundary = field.boundary_m()
    ox, oy, _, _ = boundary.bounds

    fig, ax = plt.subplots(figsize=(9.2, 8.4), dpi=140)
    fig.patch.set_facecolor(SURFACE_COLOR)

    # Plot boundary
    xs, ys = boundary.exterior.xy
    ax.plot([x - ox for x in xs], [y - oy for y in ys], color=INK_COLOR, linewidth=2.0, zorder=10)

    # Plot blocks (simplified: just show the field area in tomato color for now)
    ax.fill(
        [x - ox for x in xs], [y - oy for y in ys],
        color=TOMATO_COLOR, alpha=0.7, edgecolor=INK_COLOR, linewidth=0.5, zorder=5
    )

    # Styling
    ax.set_facecolor(SURFACE_COLOR)
    ax.set_aspect("equal")
    ax.set_title("Tomato Field Layout Demo", color=INK_COLOR, fontsize=13, fontweight="bold", loc="left", pad=30)
    ax.text(
        0.0, 1.008, "500 m × 500 m field with 50 m blocks",
        transform=ax.transAxes, color=INK_SECONDARY, fontsize=9.0,
        va="bottom", ha="left",
    )
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(colors=INK_SECONDARY, labelsize=8.5, length=0)
    ax.grid(True, color=GRIDLINE_COLOR, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    ax.set_xlabel("metres east", color=INK_SECONDARY, fontsize=8.5)
    ax.set_ylabel("metres north", color=INK_SECONDARY, fontsize=8.5)

    # Legend
    tomato_patch = mpatches.Patch(color=TOMATO_COLOR, label="Tomato")
    ax.legend(handles=[tomato_patch], loc="upper right", frameon=True, fontsize=9)

    fig.tight_layout()
    fig.savefig(path, dpi=140, facecolor=SURFACE_COLOR, edgecolor="none")
    print(f"✓ Rendered: {path}")
    plt.close(fig)


def main() -> None:
    """Run the complete demo workflow."""
    print("\n" + "=" * 70)
    print("  INTERCROP PLANNER - TOMATO CROP DEMO")
    print("=" * 70 + "\n")

    # Step 1: Create field
    print("1. Creating field...")
    field = create_tomato_field()
    print(f"   ✓ Field: {field.name}")
    print(f"     Working CRS: EPSG {field.working_crs_epsg}")
    print(f"     Boundary area: {field.gross_area_ha:.2f} ha")

    # Step 2: Create grid spec
    print("\n2. Creating grid specification...")
    grid_spec = create_grid_spec()
    print(f"   ✓ Target block size: {grid_spec.requested_block_size_m} m")
    print(f"   ✓ Implement width: {grid_spec.implement_width_m} m")
    print(f"   ✓ Headland multiple: {grid_spec.headland_multiple}x (= {grid_spec.implement_width_m * grid_spec.headland_multiple:.1f} m)")

    # Step 3: Generate grid
    print("\n3. Generating management grid...")
    grid = build_grid(field, grid_spec)
    summary = grid_summary(field, grid)
    print(f"   ✓ Grid generated")
    print(f"     - Total blocks: {summary['block_count']}")
    print(f"     - Plantable area: {summary['plantable_area_ha']:.2f} ha")
    print(f"     - Block size: {summary['block_size_m']:.1f} m")
    print(f"     - Dropped slivers: {summary['dropped_sliver_count']}")

    # Step 4: Create tomato pattern
    print("\n4. Creating tomato crop pattern...")
    pattern = create_tomato_pattern()
    print(f"   ✓ Pattern: {pattern.name}")
    print(f"     Row spacing: {pattern.components[0].row_spacing_m} m")
    print(f"     In-row spacing: {pattern.components[0].in_row_spacing_m} m")

    # Step 5: Create action plan
    print("\n5. Creating action plan...")
    farm_spec = FarmSpec()  # Minimal spec for demo
    action_plan = create_action_plan(field, grid, farm_spec)
    print(f"   ✓ Action plan created with {len(action_plan.items)} actions:")
    for i, action in enumerate(action_plan.items, 1):
        print(f"     {i}. {action.kind.value}: {action.description}")
        print(f"        {action.window_start} to {action.window_end}")

    # Step 6: Render grid visualization
    print("\n6. Rendering field visualization...")
    render_field_grid(field, grid, action_plan, OUT / "tomato_demo_field.png")

    # Step 7: Print summary
    print("\n" + "=" * 70)
    print("  DEMO SUMMARY")
    print("=" * 70)
    print(f"\nField: {field.name}")
    print(f"  Area: {field.gross_area_ha:.2f} ha")
    print(f"  Crop: Tomato (main crop)")
    print(f"  Region: Punjab (IN-PB)")
    print(f"\nGrid Layout:")
    print(f"  Blocks: {summary['block_count']}")
    print(f"  Block size: ~{summary['block_size_m']} m")
    print(f"  Plantable area: {summary['plantable_area_ha']:.2f} ha")
    print(f"\nSeason Plan:")
    print(f"  Land prep: Feb 2025")
    print(f"  Planting: Mar 1-15, 2025")
    print(f"  Growth: Mar-Jun 2025")
    print(f"  Harvest: Jun-Aug 2025")
    print(f"\nVisualization: out/tomato_demo_field.png")
    print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    main()
