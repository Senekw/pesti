"""Block-size sensitivity sweep: 25 m / 50 m / 100 m on the 1 km Punjab case.

The brief asks for the solve-time curve across these block sizes before a default is
committed. Phase 2's optimizer does not exist yet, so what runs here is a **sizing probe**,
and the distinction matters for how the numbers should be read.

What the probe DOES have, because these are what drive CP-SAT's difficulty:

* one-hot crop/row-pattern assignment per block (the real decision variable),
* linearised pairwise adjacency indicators over edge-adjacent blocks — the quadratic term
  the brief flags, reformulated as ``z[a,b] = garlic[a] AND tomato[b]`` with the standard
  three-clause AND encoding,
* a contracted-area floor on the main crop, which is what makes the problem bind at all.

What it does NOT have: the pest-pressure model, planting-date variables, the temporal
overlap logic, distance-decay beyond immediate neighbours, or the Pareto sweep. Every one of
those adds work.

**So these timings are a lower bound, not a forecast.** They are useful for ranking the three
block sizes against each other and for finding the size at which the model stops fitting in
memory, and they are not a promise about Phase 2.

Run: python scripts/block_size_sweep.py
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from ortools.sat.python import cp_model

from intercrop.domain.field import Field_
from intercrop.domain.grid import GridSpec
from intercrop.grid.generator import build_grid, edge_adjacency, synthesise_rectangle

OUT = Path(__file__).resolve().parents[1] / "out"
PUNJAB_LON, PUNJAB_LAT = 75.85, 30.90
SEED = 20260724
TIME_LIMIT_S = 120.0
TOMATO_FLOOR_HA = 80.0

# A minimal template library: (label, tomato area fraction, garlic area fraction).
# Deliberately coarse. Its job is to give the solver a realistic branching factor, not to be
# the Phase 2 library.
TEMPLATES: list[tuple[str, float, float]] = [
    ("solid_tomato", 1.00, 0.00),
    ("bands_4_2", 0.91, 0.09),
    ("bands_2_2", 0.71, 0.29),
    ("garlic_border_3m", 0.78, 0.22),
    ("solid_garlic", 0.00, 1.00),
    ("fallow", 0.00, 0.00),
]

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRIDLINE = "#e1e0d9"
SERIES_1 = "#2a78d6"  # validated pair: see scripts note
SERIES_2 = "#eb6834"


@dataclass
class Result:
    requested_m: float
    block_size_m: float
    block_count: int
    adjacent_pairs: int
    bool_vars: int
    constraints: int
    grid_seconds: float
    solve_seconds: float
    status: str
    objective: float | None
    tomato_ha: float
    garlic_ha: float


def probe(field: Field_, requested_m: float) -> Result:
    started = time.perf_counter()
    spec = GridSpec(requested_block_size_m=requested_m, implement_width_m=1.8)
    grid = build_grid(field, spec)
    adjacency = edge_adjacency(grid)
    grid_seconds = time.perf_counter() - started

    model = cp_model.CpModel()
    blocks = grid.blocks
    n_templates = len(TEMPLATES)

    # Decision variable: which template each block gets. One-hot, exactly one.
    assign = [
        [model.NewBoolVar(f"x_{b.code}_{t}") for t in range(n_templates)] for b in blocks
    ]
    for row in assign:
        model.AddExactlyOne(row)

    # Derived presence booleans. Reified off the one-hot rather than introduced
    # independently, so there is nothing for the solver to make inconsistent.
    garlic = []
    tomato = []
    for i, _ in enumerate(blocks):
        g = model.NewBoolVar(f"g_{i}")
        t = model.NewBoolVar(f"t_{i}")
        model.AddMaxEquality(
            g, [assign[i][k] for k, (_, _, gf) in enumerate(TEMPLATES) if gf > 0]
        )
        model.AddMaxEquality(
            t, [assign[i][k] for k, (_, tf, _) in enumerate(TEMPLATES) if tf > 0]
        )
        garlic.append(g)
        tomato.append(t)

    # The adjacency term, linearised. z[a,b] <-> garlic in a AND tomato in b. This is the
    # reformulation the brief asks to see: the objective wants a product of two decision
    # variables, which CP-SAT cannot take directly, so each product becomes one indicator
    # with three clauses pinning it to the conjunction.
    index = {b.code: i for i, b in enumerate(blocks)}
    protection = []
    pairs = 0
    for code, neighbours in adjacency.items():
        a = index[code]
        for neighbour in neighbours:
            b = index[neighbour]
            z = model.NewBoolVar(f"z_{a}_{b}")
            model.AddBoolOr([garlic[a].Not(), tomato[b].Not(), z])  # g AND t  -> z
            model.AddImplication(z, garlic[a])  # z -> g
            model.AddImplication(z, tomato[b])  # z -> t
            protection.append((z, blocks[b].plantable_area_m2))
            pairs += 1

    # Contracted-area floor on tomato. Integer m2 to keep the model integral.
    tomato_area = sum(
        round(b.plantable_area_m2 * tf) * assign[i][k]
        for i, b in enumerate(blocks)
        for k, (_, tf, _) in enumerate(TEMPLATES)
        if tf > 0
    )
    garlic_area = sum(
        round(b.plantable_area_m2 * gf) * assign[i][k]
        for i, b in enumerate(blocks)
        for k, (_, _, gf) in enumerate(TEMPLATES)
        if gf > 0
    )
    model.Add(tomato_area >= int(TOMATO_FLOOR_HA * 10_000))

    # Stand-in objective: maximise protected tomato adjacency, penalise garlic area. The
    # real objective is expected applications, which needs the Phase 1 pressure model.
    model.Maximize(
        sum(round(area) * z for z, area in protection) - 3 * garlic_area
    )

    solver = cp_model.CpSolver()
    solver.parameters.random_seed = SEED
    solver.parameters.max_time_in_seconds = TIME_LIMIT_S
    solver.parameters.num_workers = 8

    started = time.perf_counter()
    status = solver.Solve(model)
    solve_seconds = time.perf_counter() - started

    status_name = solver.StatusName(status)
    tomato_ha = garlic_ha = 0.0
    objective = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        objective = solver.ObjectiveValue()
        for i, b in enumerate(blocks):
            for k, (_, tf, gf) in enumerate(TEMPLATES):
                if solver.Value(assign[i][k]):
                    tomato_ha += b.plantable_area_m2 * tf / 10_000.0
                    garlic_ha += b.plantable_area_m2 * gf / 10_000.0

    proto = model.Proto()
    return Result(
        requested_m=requested_m,
        block_size_m=spec.block_size_m,
        block_count=len(blocks),
        adjacent_pairs=pairs,
        bool_vars=len(proto.variables),
        constraints=len(proto.constraints),
        grid_seconds=grid_seconds,
        solve_seconds=solve_seconds,
        status=status_name,
        objective=objective,
        tomato_ha=tomato_ha,
        garlic_ha=garlic_ha,
    )


def chart(results: list[Result], path: Path) -> None:
    """Two stacked panels sharing the x axis.

    Block count and seconds are different measures on different scales, so they get separate
    panels rather than a second y axis. Both series in the lower panel are seconds, which is
    what makes sharing one axis there legitimate.
    """
    sizes = [r.block_size_m for r in results]
    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(9.0, 7.6), dpi=140, sharex=True, layout="constrained",
        gridspec_kw={"height_ratios": [1.0, 1.15]},
    )
    fig.patch.set_facecolor(SURFACE)

    for ax in (top, bottom):
        ax.set_facecolor(SURFACE)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.grid(True, axis="y", color=GRIDLINE, linewidth=0.7)
        ax.set_axisbelow(True)
        ax.tick_params(colors=MUTED, labelsize=9, length=0)

    top.set_title(
        "Block-size sweep on the 1 km x 1 km Punjab case",
        color=INK, fontsize=13, fontweight="bold", loc="left", pad=26,
    )
    top.text(
        0.0, 1.01,
        "CP-SAT sizing probe: assignment + linearised adjacency + area floor. "
        "No pressure model, no dates. Lower bound on Phase 2.",
        transform=top.transAxes, color=INK_SECONDARY, fontsize=9, va="bottom", ha="left",
    )

    # Panel 1: decision units. Single series, so no legend — the y label names it.
    top.plot(sizes, [r.block_count for r in results], color=SERIES_1, linewidth=2,
             marker="o", markersize=8, markeredgecolor=SURFACE, markeredgewidth=1.5)
    for r in results:
        top.annotate(
            f"{r.block_count}", xy=(r.block_size_m, r.block_count),
            xytext=(0, 11), textcoords="offset points", ha="center",
            color=INK, fontsize=9.5, fontweight="bold",
        )
    top.set_ylabel("management blocks", color=INK_SECONDARY, fontsize=10)
    # Headroom so the topmost direct label does not run into the subtitle.
    top.set_ylim(0, max(r.block_count for r in results) * 1.22)

    # Panel 2: both series in seconds.
    bottom.plot(sizes, [r.solve_seconds for r in results], color=SERIES_1, linewidth=2,
                marker="o", markersize=8, markeredgecolor=SURFACE, markeredgewidth=1.5,
                label="probe solve")
    bottom.plot(sizes, [r.grid_seconds for r in results], color=SERIES_2, linewidth=2,
                marker="s", markersize=8, markeredgecolor=SURFACE, markeredgewidth=1.5,
                label="grid generation")
    # Direct-label the solve series only. The two series converge at coarse block sizes, so
    # labelling both would stack numbers on top of each other and on the x axis; the legend
    # carries grid-generation identity and its exact values are in the printed table.
    for r in results:
        bottom.annotate(
            f"{r.solve_seconds:.2f}s ({r.status.lower()})",
            xy=(r.block_size_m, r.solve_seconds), xytext=(0, 13),
            textcoords="offset points", ha="center", color=INK, fontsize=9.5,
            fontweight="bold",
        )
    bottom.set_ylabel("seconds", color=INK_SECONDARY, fontsize=10)
    bottom.set_ylim(0, max(r.solve_seconds for r in results) * 1.3)
    bottom.set_xlabel("block edge length (m, snapped to 1.8 m implement)",
                      color=INK_SECONDARY, fontsize=10)
    bottom.set_ylim(bottom=0)
    bottom.set_xticks(sizes)
    bottom.set_xticklabels([f"{s:g}" for s in sizes])
    legend = bottom.legend(frameon=False, fontsize=9.5, labelcolor=INK_SECONDARY,
                           loc="upper right")
    legend.set_zorder(5)

    fig.supxlabel(
        f"seed {SEED} | {TIME_LIMIT_S:g}s limit | tomato floor {TOMATO_FLOOR_HA:g} ha | "
        "'feasible' would mean the limit was hit before proving optimality",
        color=MUTED, fontsize=8.5,
    )
    fig.savefig(path, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    field = Field_.from_boundary(
        "Punjab 1 km square",
        synthesise_rectangle(PUNJAB_LON, PUNJAB_LAT, 1000.0, 1000.0),
        region_code="IN-PB",
    )

    # The brief asks for 25 / 50 / 100. The 12.5 m point is a headroom check: it is finer
    # than anyone would drive, and it exists to show where the structural size actually
    # starts to bite rather than to be a candidate default.
    results = [probe(field, m) for m in (100.0, 50.0, 25.0, 12.5)]
    results.sort(key=lambda r: r.block_size_m)

    header = (
        f"{'req':>5} {'block':>7} {'blocks':>7} {'pairs':>7} {'vars':>8} {'constr':>8} "
        f"{'grid_s':>7} {'solve_s':>8} {'status':>10} {'tomato_ha':>10} {'garlic_ha':>10}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.requested_m:>5.0f} {r.block_size_m:>7.1f} {r.block_count:>7} "
            f"{r.adjacent_pairs:>7} {r.bool_vars:>8} {r.constraints:>8} "
            f"{r.grid_seconds:>7.2f} {r.solve_seconds:>8.2f} {r.status:>10} "
            f"{r.tomato_ha:>10.1f} {r.garlic_ha:>10.1f}"
        )

    chart(results, OUT / "block_size_sweep.png")
    print("\n-> out/block_size_sweep.png")


if __name__ == "__main__":
    main()
