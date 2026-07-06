# ADR-007: API Gateway via Traefik

**Status:** Accepted

## Context
The system needs a single external entry point with routing and rate-limiting (M6). We can build one ourselves or use a managed reverse proxy.

## Decision
Use **Traefik** as the API gateway - a ready-made reverse proxy configured declaratively, not coded. It auto-discovers services in docker-compose and provides rate-limiting out of the box.

## Consequences

**Pros:**
- No gateway code to write or maintain — configuration over code.
- Integrates cleanly with docker-compose (M4); auto-discovers services.
- Rate-limiting (M6) built in.

**Cons:**
- Another component in the stack; some Traefik-specific configuration to learn.

## Alternatives considered
- **Build our own (YARP / FastAPI proxy)** - rejected; reinvents a solved problem, more to maintain.
- **NGINX** - viable, but Traefik's native Docker service-discovery is simpler for this setup.