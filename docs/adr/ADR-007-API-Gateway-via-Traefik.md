# ADR-007: API Gateway via Traefik

**Status:** Accepted (updated - see "Provider mechanism" below)

## Context
The system needs a single external entry point with routing and rate-limiting (M6). We can build one ourselves or use a managed reverse proxy.

## Decision
Use **Traefik** as the API gateway - a ready-made reverse proxy configured declaratively, not coded. This part of the decision is unchanged.

## Provider mechanism: file-based, not Docker-label auto-discovery

The original plan was Traefik's **Docker provider** (`--providers.docker=true`) - auto-discovering
routes from `traefik.*` labels on each service in `docker-compose.yml`, no separate config file.
**This was implemented and then reverted after empirical testing**: Traefik's Docker provider
could not talk to this environment's Docker Engine (29.0.1, API 1.52, minimum supported 1.44) -
every attempt failed with a bare `Error response from daemon: API returned a 400 (Bad Request)
but provided no error-message`, regardless of Traefik version. Confirmed directly, not assumed:

- The socket itself is fine - `docker run` with the official `docker:cli` image and the identical
  bind mount ran `docker version` successfully against the same daemon.
- Tried `traefik:v3.1.2` (Aug 2024 build) and `traefik:v3.5.6` (Nov 2025 build) in isolated
  containers on the same network - both produced the identical 400 error.
- Tried forcing `DOCKER_API_VERSION=1.44` (the daemon's own stated minimum) as an env var - no
  change.

This points to a genuine incompatibility between Traefik's vendored Docker client library and
this specific (very new) Docker Engine release, not a configuration mistake - and not something
fixable by picking a different Traefik tag, since two tags roughly 15 months apart both failed
identically.

**Switched to Traefik's file provider** instead: a static YAML file (`traefik/dynamic.yml`)
declares the routers/services/rate-limit middleware directly, and Traefik reads it via
`--providers.file.directory=...` - no Docker API calls at all, so the incompatibility is
sidestepped entirely rather than worked around. Backend addresses in that file
(`http://intake:8000`, etc.) rely on Docker Compose's own internal DNS, which re-resolves
automatically when a container is recreated - confirmed live by force-recreating `intake`
mid-session and confirming the gateway routed to it correctly immediately after, with no gateway
restart or config change needed.

## Consequences

**Pros:**
- No gateway code to write or maintain — configuration over code (still true - a static YAML
  file is still declarative config, not a Python service; "logic free" per `ARCHITECTURE.md`).
- Rate-limiting (M6) built in.
- The file provider needs **no Docker socket access at all** - a smaller attack surface than the
  originally-planned label-based approach, and one less thing to explain/justify in review.

**Cons:**
- Another component in the stack; some Traefik-specific configuration to learn.
- Route/service/middleware definitions are static in `traefik/dynamic.yml`, not auto-discovered
  from labels - a real trade-off in general, but not a practical one here: this project's
  topology (`docker-compose`, fixed service names, no scaling/rescheduling) never needed dynamic
  discovery to begin with.

## Alternatives considered
- **Build our own (YARP / FastAPI proxy)** - rejected; reinvents a solved problem, more to maintain.
- **NGINX** - viable, but Traefik's file provider config (labels-equivalent, just centralized in
  one YAML file) is still simpler to maintain than an `nginx.conf` for this setup.
- **Traefik Docker provider (labels)** - the original plan; abandoned after empirical testing
  showed it's incompatible with this environment's Docker Engine version (see above). Not
  rejected on principle - would likely work fine against an older or more common Docker Engine
  release; documented here so the reasoning survives if revisited later.