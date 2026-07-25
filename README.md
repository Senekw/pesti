# Intercrop planner

**The goal.** A grower describes their farm in plain language. The system asks for the few
missing facts that actually move the answer, lays a management grid over the field, optimises
an intercropping layout to reduce the number of pesticide applications needed over the season,
and returns a field map plus a dated action plan.

**What is built.** Everything in that sentence except the optimiser. Phase 0 is the domain
model, the geometry, and the evidence discipline: Pydantic v2 models, the grid generator, and
a versioned agronomic parameter store, with a web demo over the three. No solver, no pressure
model, no agent, no persistence — and the demo is built to make those absences visible rather
than paper over them.

```bash
uv sync --all-extras
python web/server.py        # the demo, on http://127.0.0.1:8765
python -m pytest tests/ -q  # 156 tests
```

Python 3.13. Not 3.14 — OR-Tools has no wheel for it yet.

- **[`docs/phase0-checkpoint.md`](docs/phase0-checkpoint.md)** — start here. The block-size
  recommendation and its evidence, every departure from the proposed entity list, the
  agronomic risks, and the open questions.
- **[`docs/erd.md`](docs/erd.md)** — entity relationships, in four diagrams.

## The demo

Three pages, served by `web/server.py` — standard library only, so there is nothing to
install beyond the runtime dependencies and the deployed site runs the same handler.

| Route | What it is |
| --- | --- |
| `/` | Landing page. The hero plate is drawn from a live `POST /api/plate`, not a picture. |
| `/chat` | Intake conversation. **Scripted** — canned replies, no model call, and it always plans the Ludhiana tomato-and-garlic example. It hands off to the sheet through the query string. |
| `/sheet` | The field sheet. Every control re-runs the real generator. |

The sheet is the part with nothing faked in it. Move a control and `POST /api/plate` rebuilds
the field, tessellates it, applies a row-pattern rule per block and re-dates the season:

- **Blocks are drawn at true scale.** Garlic bands are 0.6 m in a 6.6 m repeat and the
  marigold rim is 3 m deep, because a schematic that exaggerated either would misstate what
  the block holds.
- **Failures are shown, not swallowed.** Ask for a headland that consumes the field and the
  generator's own explanation is stamped on the plate.
- **No spray count.** The one number a grower most wants is the one the demo refuses to
  print, and it says why: there is no pressure model, and every interaction coefficient on
  file is provisional.

`GET /api/parameters` backs the register at the bottom of the sheet: every entry, its status,
and the reason it is not yet a source.

### The other scripts

```bash
python scripts/render_grid_examples.py     # -> out/grid_*.png
python scripts/block_size_sweep.py         # -> out/block_size_sweep.png
python scripts/demo_tomato_crop.py         # the console walkthrough the sheet grew out of
```

## Deploying

Pushing to `main` deploys, once the repository is imported into Vercel (**New Project → import
this repo → Deploy**; the defaults are correct, there is nothing to configure). Put the
production URL here when that is done.

- [`api/index.py`](api/index.py) is the entry point — it re-exports the same
  `BaseHTTPRequestHandler` the local server uses, so local and deployed behaviour cannot
  drift.
- [`vercel.json`](vercel.json) rewrites every path to that function and ships `src/`, `web/`
  and `params/` alongside it. The parameter store is read from disk per request.
- [`requirements.txt`](requirements.txt) mirrors `[project.dependencies]`. The solver and
  plotting extras are not needed to serve the sheet.

The function runs on Vercel's Python 3.12, which every language feature in `src/` predates.

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
web/
  server.py           routes and the JSON API, standard library only
  service.py          browser request -> real domain objects -> drawable response
  landing.html        landing page
  chat.html           scripted intake
  index.html          the field sheet
api/index.py          Vercel entry point, re-exports web/server.py's handler
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
