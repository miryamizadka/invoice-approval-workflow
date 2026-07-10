# ADR-009: Configurable Policy & Thresholds via Dapr Configuration API, Fetch-Once

**Status:** Accepted

## Context

F7 ("Configurable policy and thresholds", MASTER_CHECKLIST.md, Priority: MUST HAVE) and M13 ("External Configuration") require that the policy and autonomy thresholds be changeable without a code redeploy. Before this phase, `services/decision/router/config.py`'s `DEFAULT_THRESHOLDS` was a hardcoded Python constant - its own docstring admitted as much: "In production these values would be loaded from Dapr configuration at runtime instead of imported as a Python constant." `policy/policy.md` (the text injected into the LLM prompt) was loaded from a file, but that file is baked into the Docker image (`Dockerfile`: `COPY policy/ ./policy/`) - changing it still meant a rebuild, not truly "without redeploy."

`ARCHITECTURE.md` §9 already commits to the mechanism by name: "The policy and autonomy thresholds live in a Dapr configuration store, never hard-coded." This ADR documents how that commitment is implemented, not a new technology choice.

## Decision

Decision reads policy text + the 8 threshold fields from Dapr's Configuration API (`configuration.redis`, a new component scoped to `decision` only) **once at startup**, via the FastAPI lifespan hook - the same pattern already used for Payment's budget seeding and Audit's schema creation. `build_decider()`'s signature now takes `thresholds`/`policy` as explicit parameters (the caller decides, the same principle `RouterNode` already applied) instead of importing `DEFAULT_THRESHOLDS`/`load_policy_text()` internally.

The loader is split into two layers: `_fetch_raw_config()` (the only thing that touches Dapr - I/O, can fail, never raises) and `merge_thresholds()` (pure - a plain `dict[str, str]` in, an `AutonomyThresholds` out, fully unit-testable with no Dapr client at all). Per field: a missing key falls back to the default silently; a present-but-unparseable value (e.g. `ceiling=abc`) falls back for that field only and logs a warning, never invalidating the rest. Every field that actually changed is logged with its old and new value.

A change takes effect by writing directly to the backing store - `redis-cli SET ceiling 500` - followed by `docker compose restart decision`. Dapr's Configuration API is read/subscribe-only from the application's side (no `save_configuration` in the client SDK); operators change values through the store itself, not through Dapr.

## Fetch-once, not live hot-reload (`subscribe_configuration`)

Dapr's SDK also offers `subscribe_configuration()` - a callback that fires immediately when a watched key changes, no restart needed at all. This ADR deliberately does **not** use it.

**Reasoning:**
1. **Consistency.** Every other configuration mechanism in this project - `LLM_PROVIDER`, `policy.md`, `budgets.json`, `PAYMENT_SIMULATE_FAILURE_IDS` - is read once at startup. Thresholds becoming the one live-reloading exception would be an unexplained inconsistency, not a improvement.
2. **What "without redeploy" actually requires.** `docker compose restart` touches neither the image nor the code - it satisfies M13's literal requirement. Hot-reload buys "no restart," which nothing in F7/M13's wording asks for.
3. **YAGNI.** Live hot-reload means turning `Decider`'s thresholds into mutable state shared across concurrently in-flight requests - real synchronization complexity (what does an in-progress `decide()` call see if the value changes mid-flight?) for a config that changes rarely. The cost is concrete and ongoing; the benefit is saving a ~10-second restart.

`ARCHITECTURE.md` §9's original wording ("takes effect immediately") has been corrected to describe the fetch-once behavior actually implemented, so the documentation matches reality rather than overstating it.

## Resilience: a real bug found and fixed during live verification

The design's central promise is that Decision must start and function correctly even if the configuration store was never seeded or is completely unreachable. Live testing (not just unit tests) found a real violation of that promise: `LazyDaprClient`'s underlying `DaprClient()` constructor performs its own blocking wait for the Dapr sidecar's health check (up to 60s) and raises `TimeoutError` if it isn't ready - and that construction was happening *outside* `_fetch_raw_config`'s try/except, in the caller. When the sidecar wasn't ready at startup, this exception propagated uncaught out of the FastAPI lifespan hook and crashed the entire service ("Application startup failed. Exiting.") - the opposite of the intended resilience.

Fixed by moving client resolution *inside* the try block, so any failure during construction - not just failures from an already-constructed client's `get_configuration()` call - falls back to defaults. Covered by a dedicated regression test (`test_fetch_raw_config_falls_back_when_client_construction_itself_raises`) that unit tests alone had not caught, since every other test passed a fake client directly, bypassing the real construction path entirely. Verified live afterward: booting against a config store with no keys set at all logs `redis key ceiling does not exist, ignore it` (Dapr's own message) and the service starts healthy on defaults - no crash.

## Consequences

**Pros:**
- F7/M13 genuinely satisfied: a controller can change a threshold or the policy text without touching the image or the code.
- Consistent with every other configuration mechanism in this project.
- The service is provably resilient to an empty or unreachable configuration store - verified live, not just asserted.

**Cons:**
- A changed value requires a manual `docker compose restart decision` - not instantaneous.
- One more Dapr component (`configuration.yaml`) to maintain.

## Alternatives considered

- **`subscribe_configuration()` (live hot-reload)** - rejected; see "Fetch-once, not live hot-reload" above.
- **A mounted config file instead of Dapr Configuration API** - rejected; `ARCHITECTURE.md` §9 already commits to Dapr Configuration specifically, and a bind-mounted file would leave the M5 Technology Stack table's "Dapr | ... config" claim permanently aspirational rather than true.
- **Environment variables** - rejected for the same reason as above, and because thresholds are structured, multi-field data (8 numeric fields + policy text), a poor fit for flat env vars compared to `policy.md`'s and `budgets.json`'s existing file-based precedent, which the Dapr configuration store now supersedes.
