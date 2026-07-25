# Intercrop planner

A grower describes their farm in plain language. The system asks for the few missing facts
that actually move the answer, lays a management grid over the field, optimises an
intercropping layout to reduce the number of pesticide applications needed over the season,
and returns a field map plus a dated action plan.

**Phase 0 only. Stopped at the checkpoint for review.**

- **[`docs/phase0-checkpoint.md`](docs/phase0-checkpoint.md)** — start here. The block-size
  recommendation and its evidence, every departure from the proposed entity list, the
  agronomic risks, and the open questions.
- **[`docs/erd.md`](docs/erd.md)** — entity relationships, in four diagrams.

## What exists

Pydantic v2 domain models, the grid generator, and a versioned agronomic parameter store.
No solver, no pressure model, no agent, no persistence.

## Quick start

```bash
uv sync --all-extras
python -m pytest tests/ -q                 # 156 tests
python scripts/render_grid_examples.py     # -> out/grid_*.png
python scripts/block_size_sweep.py         # -> out/block_size_sweep.png
```

Python 3.13. Not 3.14 — OR-Tools has no wheel for it yet.

## Four invariants the code enforces rather than trusts

**No unsourced agronomy.** Every efficacy figure, degree-day base, and yield penalty lives in
a versioned parameter file with a citation and a validity range. `ParameterRecord` rejects
`PUBLISHED` status without a citation; unsourced values must be marked `PROVISIONAL` with a
rationale, and provisional status propagates to every output that touches them. There is no
default-returning lookup anywhere in the parameter store — a missing parameter raises with a
message written to be shown to the grower. *Every one of the 11 entries currently shipped is
provisional.*

**Never satisfy a constraint by relaxing the model.** `ObjectiveOutcome` will not validate if
the modelled application count exceeds the grower's cap without a `shortfall_note` explaining
the gap. A coefficient asked to speak outside the envelope it was measured in raises
`OutOfValidityRange` rather than extrapolating. A Hedges' *g* refuses conversion to a
percentage.

**Solving is free; persisting is gated.** Running the optimizer and re-solving during
refinement need no approval. Committing a plan of record, ingesting data, and exporting all
funnel through a single function, `authorise_commit`, which checks proposal status, expiry,
that the approval names a genuine human turn, that the approver is not the agent, and that the
state hash still matches. One choke point, not a check per call site — a second code path is
how an approval gate eventually gets bypassed.

**Ingested text is data, never instruction.** Free text from uploaded files is wrapped in
`UntrustedText` and reaches a prompt only through `for_prompt()`, which fences and labels it
and escapes the fence token so a hostile note cannot close its own quoting. No
`ApprovalSource` other than `HUMAN_TURN` can be constructed into an `ApprovalRecord`.

## Layout

```
src/intercrop/
  provenance.py       Sourced[T], Evidenced, Citation, ValidityRange
  geometry.py         CRS selection, WGS84 <-> metric, long axis, grid frame
  domain/             field, grid, crops, pests, interactions, spec, plan, governance
  grid/generator.py   tessellation, adjacency, distance matrix
  parameters/store.py versioned parameter set, hashing
params/               parameters.v0.1.0.toml
docs/                 checkpoint, ERD
scripts/              renders, block-size sweep
tests/                156 tests
out/                  generated images (not committed)
```

## Three frames, three jobs

Confusing these is the bug class this project is most exposed to.

- **WGS84 (EPSG:4326)** — interchange only. GeoJSON in, GeoJSON out. Nothing is measured here:
  at Punjab's latitude equal degree extents differ by ~15% on the ground.
- **Working CRS (metric)** — chosen once per field from the boundary centroid and *stored* on
  the `Field`, so a parcel near a UTM zone edge cannot silently change projection between
  solves. Every area, distance, and buffer is computed here.
- **Grid frame** — the working CRS rotated so +x runs along the field's long axis.
  Tessellation happens axis-aligned here, which is what makes the grid describe passes a
  grower can actually drive.
