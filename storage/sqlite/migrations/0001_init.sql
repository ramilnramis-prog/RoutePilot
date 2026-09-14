-- RoutePilot storage schema, migration 0001 (Stage 3 U9; decision D38).
--
-- This file implements sections 2-6 of the APPROVED schema `docs/STORAGE_SCHEMA.md` exactly:
-- every table, column, type, nullability, default, CHECK constraint and index. It is applied
-- once, in ascending version order, by `storage.sqlite.database.migrate`. A released migration is
-- never edited, but that is a source-control rule, not an enforced check: `schema_migrations`
-- records only (version, applied_at_utc), so applied versions are recorded and never re-applied,
-- while an already-applied migration file that was modified afterwards is NOT detected here.
--
-- Storage conventions encoded here (schema section 1):
--   * absolute timestamps are UTC ISO-8601 TEXT with a trailing `Z` (`2026-09-11T01:00:00Z`);
--   * durations are INTEGER seconds (`*_sec`);
--   * booleans are INTEGER 0/1;
--   * service windows are stored as `service_window_kind` + LOCAL wall-clock `HH:MM:SS` text,
--     never as resolved instants; strict DST validation happens at compute time (D3);
--   * `*_json` columns are TEXT holding JSON, validated by the domain on load, never trusted;
--   * derived data (timelines, ETA, waiting) is NOT persisted - it is recomputed (D38).
--
-- Structural notes that must hold:
--   * START and FINISH are plan location columns on `route_plans` and are NEVER rows in
--     `route_stops` (I1/I2): a start location can therefore never be served;
--   * there is no `recommended_stop_id` column anywhere: a recommendation is derived and
--     recomputable, and must never be mistaken for the driver's decision (D4/D11/D32);
--   * `input_position` is immutable input-order provenance: `NOT NULL`, no default, unique per
--     plan, gaps allowed, never rewritten by optimization or reorder (v2 sections 25/30, D33).
--
-- The runner wraps this script in an explicit transaction and rolls that transaction back if any
-- statement fails, so a failure leaves neither partial DDL nor a `schema_migrations` row behind.

-- ---------------------------------------------------------------------------
-- 2. schema_migrations
-- ---------------------------------------------------------------------------
CREATE TABLE schema_migrations (
    version        INTEGER PRIMARY KEY,
    applied_at_utc TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 3. route_plans (23 columns; indexes: none beyond the PK - plans are few)
-- ---------------------------------------------------------------------------
CREATE TABLE route_plans (
    id                          TEXT PRIMARY KEY,
    name                        TEXT,
    timezone                    TEXT NOT NULL,
    departure_label             TEXT NOT NULL,
    departure_latitude          REAL NOT NULL,
    departure_longitude         REAL NOT NULL,
    departure_time_utc          TEXT NOT NULL,
    finish_label                TEXT NOT NULL,
    finish_latitude             REAL NOT NULL,
    finish_longitude            REAL NOT NULL,
    route_mode                  TEXT NOT NULL CHECK (route_mode IN (
                                    'FASTEST', 'SHORTEST', 'MINIMUM_TURNS', 'ON_THE_WAY',
                                    'START_TO_FINISH', 'SMART_ROUTE')),
    first_stop_mode             TEXT NOT NULL CHECK (first_stop_mode IN ('recommend', 'manual')),
    first_stop_selected_stop_id TEXT REFERENCES route_stops (id),
    first_stop_selection_source TEXT CHECK (first_stop_selection_source IS NULL
                                    OR first_stop_selection_source IN (
                                        'accepted_recommendation', 'manual_choice')),
    first_stop_pinned           INTEGER NOT NULL DEFAULT 0,
    window_end_policy           TEXT NOT NULL DEFAULT 'service_finish_before_end'
                                    CHECK (window_end_policy IN (
                                        'service_start_before_end', 'service_finish_before_end')),
    order_overrides_json        TEXT NOT NULL DEFAULT '{"version": 1, "constraints": []}',
    cost_policy_json            TEXT NOT NULL,
    default_service_duration_sec INTEGER,
    inputs_fingerprint          TEXT,
    data_provenance             TEXT NOT NULL CHECK (data_provenance IN (
                                    'DEMO_SYNTHETIC', 'REAL_ROUTING')),
    created_at_utc              TEXT NOT NULL,
    updated_at_utc              TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 4. route_stops (21 columns; 5 table-level CHECK blocks + UNIQUE; 2 indexes)
-- ---------------------------------------------------------------------------
CREATE TABLE route_stops (
    id                    TEXT PRIMARY KEY,
    plan_id               TEXT NOT NULL REFERENCES route_plans (id) ON DELETE CASCADE,
    input_position        INTEGER NOT NULL,
    raw_address           TEXT NOT NULL,
    normalized_address    TEXT,
    latitude              REAL,
    longitude             REAL,
    geocode_status        TEXT NOT NULL CHECK (geocode_status IN (
                              'pending', 'resolved', 'ambiguous', 'failed')),
    geocode_provider      TEXT,
    geocode_checked_at_utc TEXT,
    service_window_kind   TEXT NOT NULL CHECK (service_window_kind IN (
                              'fixed', 'unrestricted', 'unknown')),
    service_window_start  TEXT,
    service_window_end    TEXT,
    window_end_policy     TEXT,
    service_duration_sec  INTEGER,
    priority              INTEGER,
    service_status        TEXT NOT NULL DEFAULT 'pending' CHECK (service_status IN (
                              'pending', 'in_progress', 'served', 'failed', 'skipped')),
    enabled               INTEGER NOT NULL DEFAULT 1,
    notes                 TEXT,
    created_at_utc        TEXT NOT NULL,
    updated_at_utc        TEXT NOT NULL,
    -- window shape (D28): a fixed window has both local times, any other kind has neither
    CHECK (
        (service_window_kind = 'fixed'
            AND service_window_start IS NOT NULL AND service_window_end IS NOT NULL)
        OR
        (service_window_kind <> 'fixed'
            AND service_window_start IS NULL AND service_window_end IS NULL)
    ),
    -- coordinates exist exactly when geocoding succeeded
    CHECK (
        (geocode_status = 'resolved' AND latitude IS NOT NULL AND longitude IS NOT NULL)
        OR (geocode_status <> 'resolved')
    ),
    -- a fixed customer window implies a resolved customer location
    CHECK (service_window_kind <> 'fixed' OR latitude IS NOT NULL),
    -- only a fixed window has an end whose meaning can be chosen (D29)
    CHECK (window_end_policy IS NULL OR service_window_kind = 'fixed'),
    -- input-order provenance: non-negative and unique inside a plan, gaps allowed (D33)
    CHECK (input_position >= 0),
    UNIQUE (plan_id, input_position)
);

-- Reconstructs the user's BEFORE baseline in immutable input order (v2 section 30, D33).
CREATE INDEX idx_route_stops_plan ON route_stops (plan_id, input_position);

-- Active stop set of a plan.
CREATE INDEX idx_route_stops_plan_enabled ON route_stops (plan_id, enabled);

-- ---------------------------------------------------------------------------
-- 5. route_optimization_runs (17 columns; immutable append-only history; 1 index)
-- ---------------------------------------------------------------------------
CREATE TABLE route_optimization_runs (
    id                            TEXT PRIMARY KEY,
    plan_id                       TEXT NOT NULL REFERENCES route_plans (id) ON DELETE CASCADE,
    run_kind                      TEXT NOT NULL CHECK (run_kind IN (
                                      'optimize', 'reoptimize', 'preview')),
    algorithm                     TEXT NOT NULL,
    algorithm_version             TEXT NOT NULL,
    inputs_fingerprint            TEXT NOT NULL,
    route_fingerprint             TEXT NOT NULL,
    tzdata_version                TEXT,
    cost_policy_json              TEXT NOT NULL,
    data_provenance               TEXT NOT NULL CHECK (data_provenance IN (
                                      'DEMO_SYNTHETIC', 'REAL_ROUTING')),
    status                        TEXT NOT NULL CHECK (status IN (
                                      'ok', 'has_infeasible_windows', 'unresolved_first_stop')),
    order_json                    TEXT NOT NULL,
    first_stop_recommendation_json TEXT NOT NULL,
    top_k_json                    TEXT,
    violations_json               TEXT NOT NULL,
    metrics_json                  TEXT NOT NULL,
    created_at_utc                TEXT NOT NULL
);

CREATE INDEX idx_runs_plan_created ON route_optimization_runs (plan_id, created_at_utc);

-- ---------------------------------------------------------------------------
-- 6. app_settings (3 columns)
-- ---------------------------------------------------------------------------
CREATE TABLE app_settings (
    key            TEXT PRIMARY KEY,
    value_json     TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
