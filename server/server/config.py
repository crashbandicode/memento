"""Server configuration via environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEMENTO_")

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/memento"
    # Optional read/search database. Leave blank to use the primary engine.
    # Point this at a streaming replica or independently maintained search
    # database only when its replication lag is acceptable for search.
    search_database_url: str = ""

    # Phase 2 raw realtime-ingest canary.  The writer is off unless at least
    # one selector matches; selectors are comma-separated authenticated owner
    # UUIDs, collector device IDs, or tool IDs respectively.  Keeping this
    # opt-in lets an operator roll out one source domain at a time while the
    # synchronous SQLAlchemy writer remains the safe fallback.
    realtime_ingest_raw_writer_owners: str = ""
    realtime_ingest_raw_writer_devices: str = ""
    realtime_ingest_raw_writer_tools: str = ""

    # Phase 5 defaults capability-negotiated conversation DELTAs to durable
    # spool admission. Setting this false is the kill-switch that reverts them
    # to the synchronous path (full revert with env change plus recreate).
    realtime_ingest_spool_deltas: bool = True
    realtime_ingest_drain_poll_seconds: float = 0.10

    # Phase 5 hard-requires deferred projections with a running projector.
    # Turning this off after raw-supported shapes are live does not restore
    # synchronous Canvas/search (see docs/realtime-ingest-phase45-handoff.md),
    # so it is not a kill-switch.
    realtime_ingest_deferred_projections: bool = True
    realtime_ingest_projector_poll_seconds: float = 0.10

    # Redis
    redis_url: str = "redis://localhost:6380/0"

    # S3 / MinIO
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "memento"
    # Require immutable object-store writes. All deployed data has had the
    # former PostgreSQL body nulled and the contract migration drops that
    # column, so turning this off is *not* a read rollback: readers always use
    # verified pointers. It is only an emergency S3-bypass for new writes,
    # whose raw source can then be recovered by reprocessing client sources.
    document_content_minio_enabled: bool = False
    # Delayed object-GC grace period. Candidates are timed from first observed
    # unreferenced, never from mutable object-storage timestamps.
    document_content_gc_grace_hours: int = 48

    # Auth
    secret_key: str = "change-me-in-production"
    algorithm: str = "HS256"
    # 30 days. The web client also calls /api/auth/refresh on mount and
    # every 12 hours while open, so as long as the user opens the app at
    # least once a month they stay logged in indefinitely. Override via
    # MEMENTO_ACCESS_TOKEN_EXPIRE_MINUTES for shared / kiosk deploys
    # where you want shorter sessions.
    access_token_expire_minutes: int = 60 * 24 * 30

    # Collector auth
    collector_token: str = "collector-dev-token"

    # Claude API
    anthropic_api_key: str = ""
    summary_model: str = "claude-sonnet-4-20250514"

    # Optional read-only spend-dashboard integration. The external dashboard
    # remains authoritative for upstream auth, pricing, billing periods,
    # projections, and graph-ready stacking. Memento only caches and renders
    # its canonical /api/snapshot payload.
    spend_dashboard_url: str = ""
    spend_dashboard_access_token: str = ""
    spend_dashboard_timeout_seconds: float = 45.0
    spend_dashboard_cache_ttl_seconds: int = 300
    spend_dashboard_max_stale_seconds: int = 86_400

    # Large file threshold (bytes) — files bigger go to S3
    large_file_threshold: int = 1_048_576  # 1 MB

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # CORS — which origins the browser is allowed to call the API from.
    # Default `.*` accepts ANY origin. Convenient for self-hosted users
    # who put their server on whatever LAN IP / DDNS hostname / Tailscale
    # tailnet they happen to have — no .env tweak needed.
    #
    # Security caveat: this means any site a logged-in user visits can
    # make authenticated requests to their Memento API via the user's
    # browser cookies/JWT. The JWT lives in localStorage (not a cookie),
    # so it's not auto-sent by the browser, which mitigates most of the
    # classic CSRF risk — but if you serve this on the public internet,
    # set MEMENTO_CORS_ALLOW_ORIGIN_REGEX in .env to your domain(s) only.
    cors_allow_origin_regex: str = r".*"

    # On-demand request profiling (pyinstrument). Off by default and gated by a
    # secret so it can never be toggled on from outside: an operator sets both
    # MEMENTO_PROFILING_ENABLED=1 and a MEMENTO_PROFILING_TOKEN, then adds
    # `?profile=1` + the token header to any request to get a flame graph back
    # instead of the normal response. When disabled (the default) the middleware
    # is a single-boolean passthrough with no measurable overhead.
    profiling_enabled: bool = False
    profiling_token: str = ""

    # Registration control:
    #   open        — anyone can self-register (pending, needs admin approval)
    #   invite_only — must provide a valid invite_code at registration
    #   closed      — registration endpoint refuses everyone
    registration_mode: str = "open"

    # Restrict a deployment to its owner/admin account(s). This is stronger
    # than registration_mode=closed: it also blocks existing viewer tokens,
    # collector tokens, password logins, and GitHub OAuth identities.
    single_user_mode: bool = False

    # GitHub OAuth login — set both to enable "Continue with GitHub".
    github_client_id: str = ""
    github_client_secret: str = ""
    github_oauth_enabled: bool = True
    # Public base URL of this deployment (e.g. https://mem.ihasy.com),
    # used to build the OAuth redirect_uri {public_url}/api/auth/github/callback.
    # When unset, the redirect_uri is derived from the incoming request.
    public_url: str = ""

    def validate_production(self) -> None:
        """Refuse to start with dev defaults when debug is off."""
        bad = []
        if self.secret_key == "change-me-in-production":
            bad.append("MEMENTO_SECRET_KEY")
        if self.collector_token == "collector-dev-token":
            bad.append("MEMENTO_COLLECTOR_TOKEN")
        if self.s3_access_key == "minioadmin" or self.s3_secret_key == "minioadmin":
            bad.append("MEMENTO_S3_ACCESS_KEY/MEMENTO_S3_SECRET_KEY")
        if bad and not self.debug:
            raise RuntimeError(
                "Insecure defaults detected in non-debug mode: "
                + ", ".join(bad)
                + ". Set these in .env or export MEMENTO_DEBUG=1 for local dev."
            )


settings = Settings()
