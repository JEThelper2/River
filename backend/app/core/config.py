from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    # App runtime traffic goes through Supabase's *transaction-mode* pooler
    # (port 6543) in production — many short-lived REST/WS connections. Keep the
    # asyncpg driver; pgbouncer transaction mode needs prepared statements off
    # (see app/db/session.py). Defaults to a local instance for dev.
    database_url: str = "postgresql+asyncpg://spacepad:spacepad@localhost:5432/spacepad"
    # Migrations run on a *non-transaction-pooled* connection (Supabase's direct
    # endpoint, or its session-mode pooler on port 5432 where the direct host is
    # IPv6-only). Alembic needs session-level features that transaction pooling
    # breaks. Falls back to database_url when unset.
    database_url_direct: str = ""
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: str = "http://localhost:5173"

    @property
    def migration_database_url(self) -> str:
        return self.database_url_direct or self.database_url

    # Object storage: Supabase Storage (Phase 3, re-platformed onto Supabase).
    # Private bucket; access goes through the FastAPI permission layer using the
    # service-role key (reuses supabase_url / supabase_service_role_key below).
    supabase_storage_bucket: str = "pad-files"

    # Malware scanning (Phase 3). If clamd is unreachable, scanning fails CLOSED:
    # uploads are marked failed and never served. See DECISIONS.md.
    clamav_host: str = "localhost"
    clamav_port: int = 3310
    clamav_enabled: bool = False

    # Auth (Phase 4 → migrated to Supabase Auth in the Supabase phase).
    # Supabase Auth (gotrue) is now the identity provider. FastAPI calls its REST
    # API for signup/login/refresh/reset and verifies the ES256 access tokens it
    # issues against the project JWKS. The legacy HS256 self-minting path
    # (jwt_secret) is retained only so the test harness can forge tokens offline.
    jwt_secret: str = "change-me-in-production"
    jwt_access_ttl_seconds: int = 900
    jwt_refresh_ttl_seconds: int = 2592000
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # Where the Google callback sends the browser back to after login.
    frontend_base_url: str = "http://localhost:5173"

    # Supabase project. supabase_url like https://<ref>.supabase.co. The anon key
    # is the public `apikey` header on gotrue calls; the service-role key is used
    # for admin operations (e.g. confirming users). JWKS verifies user tokens.
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_aud: str = "authenticated"

    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_anon_key)

    @property
    def supabase_jwks_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

    # Whether auth cookies (the refresh-token + PIN-unlock cookies) carry the
    # ``Secure`` flag. Browsers DROP ``Secure`` cookies received over plain HTTP,
    # so this must be False whenever the SPA is served over http:// — e.g. a
    # staging/testing setup where the frontend runs on http://localhost:3000 and
    # proxies /api to an external HTTPS backend. It is deliberately decoupled from
    # ``environment`` (which still governs prod-only behaviour like rejecting
    # legacy HS256 tokens): you can run a production-configured backend behind an
    # HTTP frontend during testing. Leave unset to derive the safe default from
    # ``environment`` (Secure everywhere except development).
    cookie_secure: bool | None = None  # env: COOKIE_SECURE

    @property
    def cookies_secure(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.environment != "development"

    # Rate limiting (Phase 6). Enforced via Redis when reachable; fails open
    # otherwise (see app/services/ratelimit.py). Figures from PRD §5.4.
    rate_limit_enabled: bool = True
    ip_hash_salt: str = "change-me-in-production"
    # Number of trusted reverse-proxy hops between the public internet and the
    # app. The rate-limit client IP is read from the X-Forwarded-For chain
    # accordingly (the entry added by the outermost trusted proxy). 0 (default)
    # means trust NO forwarded headers and use the direct peer IP — un-spoofable,
    # the safe default. Production MUST set this to the real hop count (e.g. 1
    # behind a single load balancer) or clients can forge X-Forwarded-For to mint
    # fresh rate-limit buckets (AUDIT H1). See PRODUCTION_READINESS.md topology.
    trusted_proxy_hops: int = 0
    rl_create_per_hour: int = 10
    rl_create_burst_seconds: int = 5
    rl_edit_per_min: int = 60

    # Stale-pad cold-storage flagging (Phase 6). Not deletion — a storage-cost
    # marker for pads untouched for this many days. Default 365 (12 months).
    cold_storage_after_days: int = 365

    # PIN-protected pads. Unlock is time-boxed (a session window), not permanent
    # per device and not required every visit. Attempts are strictly rate-limited
    # (small keyspace) per pad per IP — reuses the Phase 6 token-bucket limiter.
    pin_unlock_window_seconds: int = 4 * 60 * 60  # 4 hours
    pin_min_length: int = 4
    pin_max_length: int = 12
    pin_argon2_time_cost: int = 4
    pin_argon2_memory_cost: int = 102_400
    pin_argon2_parallelism: int = 8
    rl_pin_attempts_per_window: int = 5
    rl_pin_window_seconds: int = 300  # 5 attempts / 5 min / (pad, IP)

    # Pad content limits. CRDT and REST saves share the same maximum to avoid
    # ever storing unbounded text in the DB.
    max_pad_content_chars: int = 100_000

    # Pad claim tokens. Time-bound (not single-use): a token can be submitted
    # repeatedly until it expires; a *successful* claim consumes it. Generating a
    # new token invalidates any still-live one for that pad (one active per pad).
    claim_token_ttl_seconds: int = 600  # 10 minutes

    # Email delivery (Phase 7). If no provider is configured, transactional
    # emails are logged instead of sent (see app/services/email.py).
    email_provider: str = ""  # "" => console/log stub; "smtp" => real SMTP send
    email_from: str = "no-reply@spacepad.app"
    password_reset_ttl_seconds: int = 3600  # 1 hour, single-use (PRD §5.6)
    # SMTP transport (used when EMAIL_PROVIDER=smtp). Vendor-neutral — works with
    # SES / Postmark / Mailgun via their SMTP endpoints, so no SDK lock-in.
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_smtp_username: str = ""
    email_smtp_password: str = ""
    email_smtp_starttls: bool = True

    # Upload caps (Phase 3). Kept here as configurable constants, not magic numbers.
    anon_max_files_per_pad: int = 5
    anon_max_file_bytes: int = 10 * 1024 * 1024
    anon_max_total_bytes: int = 25 * 1024 * 1024
    auth_max_files_per_pad: int = 50
    auth_max_file_bytes: int = 50 * 1024 * 1024
    auth_max_total_bytes: int = 500 * 1024 * 1024

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
