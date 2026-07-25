# Phase 0 checkpoint — domain model, grid geometry, repo skeleton

**Status: stopped for review, as instructed. No solver, no pressure model, no agent.**

156 tests pass. Three rendered grids and the block-size sweep are in `out/`. Nothing
persists to a database yet — that is a deliberate omission, explained in §6.

Read in this order: §1 for the recommendation you asked for, §2 for what I changed in your
entity list, §5 for the agronomic risk I think is the most serious thing on this page, and §7
for what I need from you.

---

## 1. The scale question, answered

You asked for the solve-time curve across 25 / 50 / 100 m before committing to a default.

### 1a. Your requested 50 m is not available at a 1.8 m implement

A block edge must be a whole number of implement passes. 50 / 1.8 = 27.78, so a 50 m block
leaves a part-width pass in every block. The options are 27 passes (48.6 m) or 28 (50.4 m),
and `SnapPolicy` makes that an explicit decision rather than a silent rounding. The default
snaps to nearest, giving **50.4 m**, and the grower is told:

> Block size adjusted from 50 m to 50.4 m (28 passes at 1.8 m). A 50 m block is 27.78 passes
> wide, which would leave a part-width pass in every block.

Your 400-block figure survives this: 1000 / 50.4 = 19.8, so the lattice is 20 × 20 = 400,
of which 39 are partial edge blocks after the 3.6 m headland.

### 1b. Measured curve

`python scripts/block_size_sweep.py` → `out/block_size_sweep.png`

| requested | snapped | blocks | adjacent pairs | bool vars | constraints | grid gen | probe solve | status |
|---|---|---|---|---|---|---|---|---|
| 12.5 m | 12.6 m | 6241 | 24 648 | 74 576 | 92 668 | 4.4 s | 5.28 s | OPTIMAL |
| 25 m | 25.2 m | 1599 | 6 236 | 19 028 | 23 506 | 1.1 s | 0.94 s | OPTIMAL |
| **50 m** | **50.4 m** | **400** | **1 520** | **4 720** | **5 761** | **0.28 s** | **0.19 s** | **OPTIMAL** |
| 100 m | 100.8 m | 100 | 360 | 1 160 | 1 381 | 0.07 s | 0.06 s | OPTIMAL |

**Read this as a lower bound, not a forecast.** The probe has the structural features that
drive CP-SAT difficulty — one-hot template assignment per block, the linearised pairwise
adjacency indicators, and a contracted-area floor — but it has no pressure model, no planting
dates, no temporal overlap logic, and no Pareto sweep. Every one of those adds work.

### 1c. What the curve actually tells us

Not what I expected. **Block count is not the binding constraint.** 1599 blocks solve to
proven optimality in under a second; even 6241 blocks take five. There is at least two orders
of magnitude of headroom against a human's patience, so tractability should not be what picks
the block size.

### 1d. The finding that matters more — and it should shape Phase 2

I checked the kernels against the candidate block spacings:

| block size | centroid spacing | repellency reach | natural-enemy reach |
|---|---|---|---|
| 12.6 m | 12.6 m | **0.00** | 1.00 |
| 25.2 m | 25.2 m | **0.00** | 0.77 |
| 50.4 m | 50.4 m | **0.00** | 0.21 |
| 100.8 m | 100.8 m | **0.00** | 0.00 |

With a short-range repellency kernel, **allium repellency is a within-block phenomenon at
every block size a grower could actually drive.** Neighbouring 50 m blocks are 50 m apart and
a volatile effect measured at 1 m reaches none of it. Protection comes from the row pattern
inside a block, not from which crop is in the block next door.

If that holds once the coefficient is sourced, three consequences follow:

1. The quadratic adjacency term — the thing you flagged for linearisation — is **not needed
   for repellency at all**. It is needed only for the wide-kernel mechanisms: natural-enemy
   provisioning and trap-crop diversion. That is a much smaller model.
2. Block size stops being an optimizer parameter and becomes purely an operational one: match
   the machinery, keep the map legible. 50.4 m is fine on both counts.
3. The real decision variable carrying the pest objective is **row-pattern template per
   block**, not crop per block. Worth reflecting in how Phase 2 is framed.

The whole conclusion is conditional on one number: the repellency kernel's range. At 1.5 m it
is airtight; at 15 m the inter-block term comes back. **So that parameter decides the
optimizer's architecture, and it should be sourced before Phase 2 is designed rather than
during it.** Currently it is a placeholder (§5).

### 1e. Recommendation

**Default to 50 m requested (50.4 m snapped at a 1.8 m implement).** Not because coarser is
faster — 100 m is faster still — but because it is the coarsest size that keeps a readable map
and per-block pattern decisions meaningful, and finer buys nothing while the repellency term
stays intra-block. Reconsider if the sourced kernel range turns out to be tens of metres.

---

## 2. Your entity list — what I changed

I agreed with most of it. Here is every departure.

### Added (I think these are gaps, not preferences)

| Entity / field | Why |
|---|---|
| **`working_crs_epsg` on `Field`** | Your list has no coordinate system. Everything the planner computes is metric — block edges, headland buffers, hectares, and above all the decay kernel. At Punjab's latitude a degree of longitude is ~87 km and a degree of latitude ~111 km, so an axis-aligned "square" in lon/lat is 15% out of square on the ground. There is a test that measures exactly this. The CRS is *stored*, not re-derived, so a field near a zone boundary cannot silently change projection between solves. |
| **`ManagementGrid`** | Your list has `ManagementBlock` with nothing owning it. A block is meaningless without the tessellation that produced it — its size, implement width, azimuth, headland rule. `ManagementGrid` is content-hashed and plan revisions reference the hash, so regridding a field cannot silently invalidate a plan while leaving the block codes looking valid. |
| **`GridSpec` as a separate entity** | Lets the same spec apply to several fields, and makes the block-size sweep a loop over specs rather than a special case. |
| **`ParameterSet`** | Your rule 2 requires versioned parameter files and rule 5 requires every revision to store a parameter-set hash. That needs a first-class, hashable set, not loose records. |
| **`ActionPlan` / `ActionItem`** | A layout is not a plan. The grower acts on dates: drill garlic 20 Oct – 5 Nov, do not lift before 10 July, scout weekly from first flight. `ActionItem` carries `rationale`, `depends_on_parameter_keys`, and an `is_advisory` flag forced True for interventions. |
| **`AuditEvent`** | You asked for an append-only audit log; it needs an entity. Includes `commit_denied`, because a repeated denial pattern is worth seeing. |
| **`Sourced[T]`** | Your `CONFIRM` state must show every inferred default and flagged assumption. That only works if provenance rides on the value. `FarmSpec.assumptions()` then enumerates exactly what to confirm, and `Provenance.INFERRED` without a grower-readable `basis` fails validation. |
| **`Crop` above `CropVariety`** | Your list has only `CropVariety`, which cannot express "no Solanaceae after Solanaceae" — the rotation rule is family-level. `Crop.family` is what the validator needs. |
| **`Mechanism` on `InteractionCoefficient`** | You require natural-enemy effects kept separate from repellency. Same table, different mechanism, never summed blindly — and, per §1d, different spatial ranges. |
| **`EffectMeasure`** | Meta-analyses report log response ratios or Hedges' *g*; extension trials report percentages. Reading a Hedges' *g* of 0.8 as "80% fewer aphids" is a fabricated efficacy claim, so `proportional_reduction()` **refuses** to convert Hedges' *g* rather than guessing. |
| **`TemporalRequirement`** | Makes your June-lift case structural rather than remembered: co-occupancy required, establishment lag, and zero persistence after removal. |
| **`ValidityRange` + `check_applicable()`** | A coefficient measured on greenhouse aubergine at 0.3 m spacing raises rather than extrapolating to 100 ha of field tomato. This is your rule 3 enforced at the data layer. |
| **`UntrustedText`** | Makes the trust boundary visible in the type system. Ingested free text goes through `for_prompt()`, which fences and labels it, and the fence token is escaped so a hostile note cannot close its own quoting. |
| **`ApprovalSource`** | Only `HUMAN_TURN` is constructible into an `ApprovalRecord`; `MODEL_GENERATED`, `TOOL_RESULT`, and `INGESTED_CONTENT` exist so a bypass attempt is a named, tested rejection rather than an unhandled case. |
| **`azimuth_override_deg` + `azimuth_source`** | Growers override row direction for slope, contour, and prevailing wind. And on a *square* field there is no long axis — see §4. |
| **`max_area_ha` on `CropRequest`** | This is what makes your key refinement turn expressible. "I don't have a buyer for that much garlic" is a companion-crop area ceiling. |

### Changed

- **`PlantingWindow` demoted from entity to value object.** It has no identity and is never
  referenced independently; it is always "this variety, in this region". It is a nested model
  on `CropVariety`. Also: `latest_doy < earliest_doy` is legal, because autumn-planted garlic
  wraps the year end — assuming `earliest <= latest` would silently break the worked example.
- **`RowPatternTemplate` carries geometry, not a ratio.** Storing "4:2" loses what the kernel
  needs. A 4:2 pattern at 1.5 m tomato spacing puts the mean tomato row 1.5 m from garlic; at
  0.9 m spacing, 0.9 m. Same ratio, and with a 1.5 m kernel that is the difference between
  protection and none. There is a test asserting exactly that.
- **`BlockAssignment` dates are per crop, not per block.** A single block-level planting date
  cannot represent autumn garlic beside post-frost tomato. Removal dates are decision
  variables too — the garlic lift date is a choice with a cost.
- **`ScoutingObservation` carries `trust` and `grid_content_hash`.** Without the grid hash, a
  regridded field silently reassigns historical observations to different ground.

### Rejected

Nothing outright. The only thing I actively pushed back on is `PlantingWindow`'s status.

---

## 3. What was built

```
src/intercrop/
  provenance.py          Sourced[T], Evidenced, Citation, ValidityRange, ParameterRecord
  geometry.py            CRS choice, WGS84<->metric, long axis, GridFrame
  domain/
    field.py             Field_, Exclusion, SoilSummary
    grid.py              GridSpec, ManagementGrid, ManagementBlock, SnapPolicy
    crops.py             Crop, CropVariety, PlantingWindow, RowPatternTemplate
    pests.py             PestSpecies, DegreeDayModel, ScoutingObservation, UntrustedText
    interactions.py      InteractionCoefficient, three decay kernels, Mechanism
    spec.py              FarmSpec, CropRequest, InterventionCap, boundary intents
    plan.py              Plan, PlanRevision, ActionPlan, ObjectiveOutcome, PlanDiff
    governance.py        Proposal, ApprovalRecord, AuditEvent, authorise_commit
  grid/generator.py      build_grid, adjacency, distance matrix, synthesise_rectangle
  parameters/store.py    ParameterSet loading, hashing, MissingParameter
params/parameters.v0.1.0.toml
```

### Grid generator pipeline

Project to the working CRS → erode by headland depth → subtract exclusions → rotate into the
grid frame → lay an axis-aligned lattice of snapped cells → intersect, drop slivers → rotate
back → reproject for storage. Headlands come out *before* tessellation so a boundary block's
plantable area is right the first time. Row and column indices are assigned in the grid frame,
so column index runs along the long axis — the direction a grower drives.

### Renders (`out/`)

| File | What it shows |
|---|---|
| `grid_square_km.png` | The cold-start 1 km square. 400 blocks, 39 partial, 98.57 ha plantable of 100 ha. |
| `grid_irregular.png` | Irregular parcel with a concave notch. Grid rotated to the 30.3° long axis; 346 blocks, 28 slivers dropped. |
| `grid_with_exclusions.png` | Road, irrigation main with standoff, diagonal drain, pump shed. 96.41 ha plantable; 91 partial blocks. |
| `block_size_sweep.png` | §1b. |

Blocks are shaded by plantable fraction on a sequential ramp — crop assignment does not exist
until Phase 2, so what is honest to show now is how much of each block survived. Headland and
exclusions are distinguished by hatch angle rather than hue, since they are masks and not
categories on the same scale.

### Test coverage

156 tests. Notably: blocks tile the plantable region to 1e-6 relative and never overlap
(checked on both the square and the irregular polygon, and again under a forced 37.5°
rotation, which is where tessellators usually leak); exclusions are honoured; headlands are
subtracted; block edges are whole multiples of implement width across nine
size/width/policy combinations; grid hashes are stable across regeneration and sensitive to
block size; no non-human `ApprovalSource` can commit even with validation bypassed via
`model_construct`; a prompt-injection string in a scouting CSV has no path to an approval;
Hedges' *g* refuses conversion; a greenhouse coefficient refuses to speak about open field.

---

## 4. A geometry problem your brief's own example walks into

On a **square** field there is no long axis. Every side is 1000 m, so whichever edge
`minimum_rotated_rectangle` happens to return would set the row direction for a 100 ha
block — and the grower would get no say in a decision that depends on slope, prevailing wind,
and any existing irrigation layout.

The generator detects this (`squareness_tolerance`, default 2%) and reports
`azimuth_source = "arbitrary_field_near_square"` instead of pretending it derived something.
**`CONFIRM` should ask for row direction whenever it sees that flag.** Your target transcript
asks about implement width; on a square field it should ask about row direction too, and
that question is worth more than most.

---

## 5. Agronomic risks — the most serious content on this page

Framed as questions, because I will not assert agronomy I have not sourced.

### 5a. Aphids may not be what drives spray count on Punjab tomato

Your worked example targets allium suppression of aphid colonisation. But on field tomato in
that region, the pest most likely to determine the number of applications is plausibly the
fruit borer (*Helicoverpa armigera*) rather than aphids — a borer inside a fruit is not
reachable by a foliar spray, so control is calendar- and threshold-driven around flights.

If that is right, then a garlic interplant that cuts aphid colonisation by 30% may reduce
total applications by **zero**, because the sprays were never for aphids. The optimizer would
faithfully minimise an aphid index while the grower's actual spray count did not move — and
the product would be confidently wrong in exactly the way your rule 3 is meant to prevent.

**This needs settling before Phase 1**, because it decides whether the pressure model is built
around aphid colonisation or around borer flights, and the intercropping evidence base is much
thinner for the latter. I have put a `Helicoverpa` placeholder in the parameter file as a
reminder that it is unresolved.

### 5b. The protective overlap window may be shorter than assumed

Your brief says garlic is "lifted mid-summer". Rabi garlic in north-west India is, I believe,
typically harvested considerably earlier than that — and if so, the overlap between standing
garlic and a post-frost tomato crop may be too short to cover peak pest pressure at all.

That is not a parameter to tune; it would mean the mechanism does not apply to this crop pair
in this region, and the honest output is "this does not work here, try a different companion".
Needs a regional planting calendar from a real source.

### 5c. Cutting colonisation is not cutting virus

Aphids vector non-persistently transmitted viruses, which can be acquired and transmitted
during brief probing by a transient aphid that never settles. A repellent that reduces
*colonisation* may reduce virus spread far less than proportionally. `PestSpecies` carries
`vectors_pathogens` with this warning attached, and Phase 1 must not apply a colonisation
coefficient to a virus outcome without a separate transmission parameter.

### 5d. "3.2 applications" needs a definition before it is shown to anyone

Your target transcript reports 3.2 modelled applications against a cap of 3. Nobody sprays
0.2 times. That number is an expectation over a distribution, and presenting it as though it
were a measurement invites a precision the model does not have.

I would rather present **P(applications ≤ 3) = 0.4** — or a range — than a point estimate to
one decimal. Worth deciding before Phase 4 builds a UI around a spurious decimal.

### 5e. The parameter file currently contains zero sourced agronomy

All 11 entries are `PROVISIONAL` with explicit rationales, and `ParameterRecord` structurally
rejects `PUBLISHED` without a citation, so nothing can be quietly promoted by editing a value.
Right now the honest answer to "how many sprays will this save" is that we cannot say. Phase 1
is mostly a sourcing exercise, and I would like to know what literature access you have (§7).

---

## 6. What I deliberately did not build

**Persistence and migrations.** Your Phase 0 line says "Pydantic v2 models and migrations",
but the checkpoint deliverable is the ERD, the models, and a rendered grid — and you said you
would rather correct a data model than a codebase. Committing to a storage engine before you
have told me whether this is a local tool or a hosted service seemed like the wrong thing to
lock in.

My recommendation when you are ready:

- **Postgres, geometry as JSONB, no PostGIS.** There are no spatial *queries* here — adjacency
  and distances are computed in memory from stored metric centroids (§1d, and it is ~1.3 MB at
  400 blocks). PostGIS would be weight without a use case.
- **Alembic** for migrations, with the Pydantic models staying the source of truth and a thin
  mapping layer, so validation logic does not migrate into the ORM.
- **`audit_event` genuinely append-only at the database level** — a rule or trigger denying
  UPDATE and DELETE, not a convention in application code.
- **SQLite for local dev and CI**, since nothing above needs Postgres-specific features.

Say the word and this is a contained piece of work.

---

## 7. What I need from you

Your own ask-first list, with a recommendation against each so you can mostly just say "yes".

| Question | My recommendation |
|---|---|
| **Is 5a right — do borers, not aphids, drive Punjab tomato spray count?** | The highest-value answer on this page. If borers dominate, we should reconsider the worked example before building a model around aphids. |
| **Crops beyond garlic and tomato?** | Hold at those two for Phase 1, plus one insectary crop *you* name. There is a coriander placeholder in the parameter file purely to exercise the separate natural-enemy term; it should not survive without your sign-off. |
| **Target region first?** | Punjab (`IN-PB`) only, to keep the sourcing bounded. Everything is keyed by `region_code` so a second region is additive. |
| **Historical scouting data, or cold start?** | Assume cold start; the pressure model must work without observations. Confirm so I do not build calibration you cannot feed. |
| **Organic certification status?** | Ask per farm, default `UNKNOWN`, and refuse to model interventions until answered — it changes what is legal. |
| **Irrigation type?** | Ask per farm. This is not cosmetic: flood basins effectively forbid fine interleaving of two crops with different water demand, which would rule out the 4:2 band pattern entirely. It may be the constraint that decides whether band patterns are usable in Punjab at all. |
| **Who is the user?** | Assume grower for the default path, with a researcher mode exposing the Pareto front and parameter provenance. |
| **Literature access for Phase 1?** | Tell me what you have. If it is abstracts only, expect many parameters to stay provisional, and that should be visible in the product rather than hidden. |
| **`SnapPolicy` default?** | `NEAREST`. Say if you would rather never exceed a requested block size (`DOWN`). |
| **Objective basis — count, TFI, or EIQ?** | Start with count because that is what growers say, and add TFI only if you can point me at a sourced product table. EIQ has known methodological criticism; I would not lead with it. |

---

## Running it

```bash
uv sync --all-extras                       # or: uv pip install -e ".[viz,solver]"
python -m pytest tests/ -q                 # 156 tests
python -m pytest tests/ -q -m "not slow"   # fast loop
python scripts/render_grid_examples.py     # -> out/grid_*.png
python scripts/block_size_sweep.py         # -> out/block_size_sweep.png
```

Python 3.13 (not 3.14 — OR-Tools has no 3.14 wheel yet; CP-SAT is verified working on 3.13).
