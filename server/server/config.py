"""Server configuration via environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEMENTO_")

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/memento"

    # Redis
    redis_url: str = "redis://localhost:6380/0"

    # S3 / MinIO
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "memento"

    # Auth
    secret_key: str = "change-me-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440  # 24 hours

    # Collector auth
    collector_token: str = "collector-dev-token"

    # Claude API
    anthropic_api_key: str = ""
    summary_model: str = "claude-sonnet-4-20250514"

    # Large file threshold (bytes) — files bigger go to S3
    large_file_threshold: int = 1_048_576  # 1 MB

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # CORS — which origins the browser is allowed to call the API from.
    # The default regex covers:
    #   - localhost / 127.0.0.1 (dev)
    #   - RFC 1918 private networks (self-hosted LAN: 10.x / 192.168.x /
    #     172.16-31.x)
    #   - mem.ihasy.com (the maintainer's public deployment; harmless to
    #     leave in — it's a literal hostname, not a wildcard)
    # Users with a different public domain set MEMENTO_CORS_ALLOW_ORIGIN_REGEX
    # in .env to override.
    cors_allow_origin_regex: str = (
        r"https?://("
        r"localhost(:\d+)?"
        r"|127\.0\.0\.1(:\d+)?"
        r"|10\.\d+\.\d+\.\d+(:\d+)?"
        r"|192\.168\.\d+\.\d+(:\d+)?"
        r"|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+(:\d+)?"
        r"|mem\.ihasy\.com"
        r")"
    )

    # Registration control:
    #   open        — anyone can self-register (pending, needs admin approval)
    #   invite_only — must provide a valid invite_code at registration
    #   closed      — registration endpoint refuses everyone
    registration_mode: str = "open"

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
