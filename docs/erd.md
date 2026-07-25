# Domain model — entity relationships

Split into four diagrams rather than one, because the four clusters have genuinely different
lifecycles: geometry is regenerated, agronomy is versioned and hashed, intake is mutated turn
by turn, and governance is append-only.

Entities marked **[+]** are additions to the brief's entity list; **[~]** are changes to what
it proposed. The reasoning for each is in [`phase0-checkpoint.md`](phase0-checkpoint.md).

---

## 1. Field and grid geometry

```mermaid
erDiagram
    FIELD ||--o{ EXCLUSION : "subtracts"
    FIELD |o--o| SOIL_SUMMARY : "described by"
    FIELD ||--o{ MANAGEMENT_GRID : "tessellated into"
    GRID_SPEC ||--o{ MANAGEMENT_GRID : "parameterises"
    MANAGEMENT_GRID ||--|{ MANAGEMENT_BLOCK : "contains"
    MANAGEMENT_BLOCK ||--o{ ROTATION_ENTRY : "cropping history"

    FIELD {
        uuid id PK
        string name
        geojson boundary "WGS84, interchange only"
        int working_crs_epsg "[+] stored, never re-derived"
        string crs_rationale "[+] shown at CONFIRM"
        string_array crs_warnings "[+] zone straddle, high latitude"
        string region_code "keys climate and pest complex"
        string boundary_source "[+] surveyed vs synthesised"
    }
    EXCLUSION {
        uuid id PK
        enum kind "road, irrigation_main, drainage, building, watercourse"
        geojson geometry "line or polygon"
        float width_m "required for line geometries"
        float standoff_m "operational or regulatory buffer"
    }
    GRID_SPEC {
        float requested_block_size_m
        float implement_width_m
        enum snap_policy "[+] nearest, down, up"
        float headland_multiple "default 2x implement"
        float min_plantable_fraction "sliver cutoff"
        float azimuth_override_deg "[+] grower sets row direction"
        int passes_per_block "derived, whole number"
        float block_size_m "derived, snapped"
    }
    MANAGEMENT_GRID {
        uuid id PK
        uuid field_id FK
        float azimuth_deg
        string azimuth_source "[+] long axis, override, or arbitrary"
        float_pair frame_origin_xy_m "[+] stored so indices are stable"
        int dropped_sliver_count "[+] reported, never silent"
        string generator_version
        string content_hash "referenced by plan revisions"
    }
    MANAGEMENT_BLOCK {
        uuid id PK
        string code "R03C12"
        int row_index "across short axis"
        int col_index "along long axis"
        geojson geometry "clipped plantable footprint"
        float_pair centroid_xy_m "[+] metric, feeds the decay kernel"
        float nominal_area_m2
        float plantable_area_m2 "after headland and exclusions"
    }
    ROTATION_ENTRY {
        int season_year
        string crop_family "[~] family, not just variety"
        string crop_name
    }
```

Adjacency and the block-to-block distance matrix are **derived, not stored**. At 400 blocks
the matrix is 1.3 MB and builds in milliseconds; materialising 160,000 pair rows to avoid
that would be a poor trade.

---

## 2. Agronomy and the parameter set

```mermaid
erDiagram
    PARAMETER_SET ||--o{ PARAMETER_RECORD : "contains"
    PARAMETER_SET ||--o{ INTERACTION_COEFFICIENT : "contains"
    PARAMETER_RECORD ||--o{ CITATION : "sourced by"
    PARAMETER_RECORD |o--|| VALIDITY_RANGE : "measured within"
    INTERACTION_COEFFICIENT ||--o{ CITATION : "sourced by"
    INTERACTION_COEFFICIENT |o--|| VALIDITY_RANGE : "measured within"
    INTERACTION_COEFFICIENT |o--|| DECAY_KERNEL : "falls off by"
    INTERACTION_COEFFICIENT |o--|| TEMPORAL_REQUIREMENT : "available when"

    CROP ||--o{ CROP_VARIETY : "has cultivars"
    CROP_VARIETY ||--o{ PLANTING_WINDOW : "per region"
    ROW_PATTERN_TEMPLATE ||--|{ PATTERN_COMPONENT : "composed of"
    PEST_SPECIES |o--|| DEGREE_DAY_MODEL : "phenology"
    DEGREE_DAY_MODEL ||--o{ PHENOLOGY_STAGE : "stages"
    PEST_SPECIES ||--o{ SCOUTING_OBSERVATION : "observed as"
    SCOUTING_OBSERVATION |o--o| UNTRUSTED_TEXT : "operator note"

    PARAMETER_SET {
        string version PK "semver, from filename"
        string description
        string content_hash "stored on every plan revision"
    }
    PARAMETER_RECORD {
        string key PK "pest.myzus_persicae.gdd.base_temp_c"
        value value
        string units
        enum status "published, provisional, deprecated"
        string provisional_rationale "required when provisional"
    }
    CITATION {
        enum kind "peer_reviewed, meta_analysis, extension, dataset, expert"
        string title
        string doi "one of doi or url required"
        string url
        string locator "[+] Table 3, so it is checkable"
    }
    VALIDITY_RANGE {
        string_array geographic_scope
        string_array cropping_system "open_field vs protected"
        map numeric_bounds "row_spacing_m, mean_temp_c"
    }
    INTERACTION_COEFFICIENT {
        string key PK
        string source_crop_slug "produces the effect"
        string target_crop_slug "receives it"
        string pest_slug "null only for crop-on-crop"
        enum mechanism "[+] repellency vs natural_enemy vs allelopathy"
        float effect_size
        enum effect_measure "[+] proportion, LRR, Hedges g, percent"
        float measured_at_distance_m "[+] the spacing actually tested"
    }
    DECAY_KERNEL {
        enum form "[+] exponential, threshold, linear_taper"
        float scale_m "exponential"
        float cutoff_m "stops a phantom tail summing"
        float radius_m "threshold"
    }
    TEMPORAL_REQUIREMENT {
        bool requires_co_occupancy "[+] the garlic lift-date case"
        int min_overlap_days "establishment lag"
        int effect_persists_after_removal_days
    }
    CROP {
        string slug PK
        string scientific_name
        string family "[~] drives the Solanaceae rotation rule"
        bool is_legume "allium allelopathy target"
    }
    CROP_VARIETY {
        string name
        float row_spacing_m
        float in_row_spacing_m
        float base_temp_c "crop base, NOT the pest's"
    }
    PLANTING_WINDOW {
        string region_code
        int earliest_doy
        int latest_doy "[~] may wrap the year end"
        bool requires_frost_free
    }
    ROW_PATTERN_TEMPLATE {
        string code PK "4:2_tomato_garlic"
        enum geometry "solid, bands, border, trap_perimeter, insectary"
        float border_depth_m
    }
    PATTERN_COMPONENT {
        string crop_slug
        enum role "[+] main, companion, trap, insectary, cover"
        int n_rows
        float row_spacing_m "[+] carries intra-block distance"
    }
    PEST_SPECIES {
        string slug PK
        enum kind "[+] insect vs fungal_pathogen: different drivers"
        enum guild "sap_sucking, fruit_borer, foliar_lesion"
        string_array host_families
        string economic_threshold "free text, keeps the sample unit"
        float adult_dispersal_range_m "[+] can a border even intercept it"
        string_array vectors_pathogens "[+] non-persistent transmission caveat"
    }
    DEGREE_DAY_MODEL {
        float base_temp_c
        float upper_threshold_c "[+] matters in a Punjab summer"
        string method "single_sine; must match the source"
    }
    PHENOLOGY_STAGE {
        string name
        float gdd_start "from the biofix, not 1 January"
        bool is_damaging
        bool is_treatable "[+] a borer inside a fruit is not"
    }
    SCOUTING_OBSERVATION {
        uuid id PK
        string block_code
        string grid_content_hash "[+] which grid the code refers to"
        date observed_on
        enum metric "count, incidence, severity, trap_catch"
        float value
        string sample_unit "a bare number is not an observation"
        enum trust "[+] grower, scout, sensor, imported_unverified"
    }
    UNTRUSTED_TEXT {
        string text "[+] fenced before reaching any prompt"
        string origin "file or endpoint"
    }
```

---

## 3. Intake

```mermaid
erDiagram
    FARM_SPEC ||--o{ CROP_REQUEST : "wants"
    FARM_SPEC |o--o| INTERVENTION_CAP : "targets"
    FARM_SPEC |o--o| LOCATION : "sited at"
    FARM_SPEC |o--o| BOUNDARY_INTENT : "shaped by"
    FARM_SPEC ||--o{ SOURCED_VALUE : "every field wrapped in"

    FARM_SPEC {
        uuid id PK
        sourced user_role "grower, agronomist, researcher"
        sourced implement_width_m
        sourced irrigation "drip and flood constrain layout"
        sourced certification "changes what is legal"
        sourced last_frost_doy "inferred from location"
        sourced pest_complex "inferred from region and crop"
        sourced has_scouting_history
        string_array raw_transcript "verbatim, so CONFIRM can quote"
    }
    SOURCED_VALUE {
        value value
        enum provenance "[+] stated, measured, inferred, default, unknown"
        string basis "[+] grower-readable; required if inferred"
        string utterance "[+] verbatim text it came from"
        string parameter_key "[+] traces back to a citation"
    }
    LOCATION {
        float lon
        float lat
        string region_code
        enum precision "[+] exact, district, region, country"
    }
    BOUNDARY_INTENT {
        enum kind "stated_dimensions, stated_area, uploaded_boundary"
        bool is_synthesised "[+] a placeholder shape stays labelled"
    }
    CROP_REQUEST {
        string crop_slug
        enum role
        float min_area_ha "contract floors"
        float max_area_ha "[+] 'no buyer for that much garlic'"
        bool is_contracted "never relaxed to hit a spray target"
        bool has_market "[+] null means unasked"
    }
    INTERVENTION_CAP {
        float max_applications
        enum basis "count, tfi, eiq"
        bool is_hard_limit "[+] preference is not infeasibility"
    }
```

---

## 4. Plans and governance

```mermaid
erDiagram
    PLAN ||--|{ PLAN_REVISION : "revisions"
    PLAN_REVISION ||--|{ BLOCK_ASSIGNMENT : "assigns"
    PLAN_REVISION |o--|| OBJECTIVE_OUTCOME : "achieved"
    PLAN_REVISION |o--|| SOLVER_PROVENANCE : "produced by"
    PLAN_REVISION |o--|| ACTION_PLAN : "actionable as"
    ACTION_PLAN ||--o{ ACTION_ITEM : "dated items"
    PLAN_REVISION ||--o{ PLAN_DIFF : "compared in"
    PROPOSAL ||--o| APPROVAL_RECORD : "authorised by"
    PROPOSAL ||--o{ AUDIT_EVENT : "logged as"

    PLAN {
        uuid id PK
        uuid field_id FK
        int season_year
        enum status "draft, of_record, superseded, abandoned"
        uuid revision_of_record_id "set only via an approval"
    }
    PLAN_REVISION {
        uuid id PK
        int revision_number
        uuid parent_revision_id "refinement chain"
        string grid_content_hash "reproducibility"
        string parameter_set_hash "reproducibility"
        json constraint_set "verbatim, not a summary"
        string pareto_label "which point on the front"
    }
    BLOCK_ASSIGNMENT {
        string block_code
        string pattern_code
        map planting_dates "[~] per crop, not per block"
        map removal_dates "[+] the garlic lift date is a decision"
    }
    OBJECTIVE_OUTCOME {
        float expected_applications
        float main_crop_area_ha
        float requested_cap
        string shortfall_note "[+] required when the cap is missed"
        string_array uses_provisional_parameters "[+] labels propagate"
    }
    SOLVER_PROVENANCE {
        string solver_version
        int seed "a plan that cannot be reproduced is not a plan"
        string status
        float wall_time_s
        uuid warm_started_from_revision
        string_array infeasibility_explanation "required if INFEASIBLE"
    }
    ACTION_PLAN {
        int expected_intervention_count
    }
    ACTION_ITEM {
        enum kind "[+] plant, scout, expected_intervention, remove_companion"
        date window_start
        date window_end
        string_array block_codes
        string rationale "[+] why this date"
        string_array depends_on_parameter_keys "[+] provenance of a date"
        bool is_advisory "[+] always true for interventions"
        string threshold_note "[+] treat on evidence, not our calendar"
    }
    PLAN_DIFF {
        string_array blocks_changed
        map area_delta_ha
        float applications_delta "what the objection cost"
        string summary
    }
    PROPOSAL {
        uuid id PK
        enum kind "plan_commit, data_ingest, export"
        enum status "pending, approved, committed, failed_revalidation"
        string state_hash "revalidated at commit time"
        string rationale
        json diff "required and non-empty"
        datetime expires_at "[+] proposals go stale"
    }
    APPROVAL_RECORD {
        uuid id PK
        uuid proposal_id FK
        enum source "[+] only human_turn is ever valid"
        string human_turn_id "[+] required, anchors to a real message"
        string approver "must not be the agent"
        string revalidated_state_hash
    }
    AUDIT_EVENT {
        uuid id PK
        enum kind "[+] includes commit_denied and solve_run"
        datetime at
        string actor
        bool actor_is_human
        string state_hash_before
        string state_hash_after
    }
```

Every gated action funnels through one function, `authorise_commit`, rather than a check per
call site — a second code path is how an approval gate eventually gets bypassed.
