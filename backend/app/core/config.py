"""Typed settings, loaded once and cached.

Nothing in the codebase reads os.environ directly; everything goes through
`settings`, so a missing variable fails at startup rather than at the first
request that happens to need it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]
LLMProvider = Literal["gemini", "bedrock", "fallback"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- environment ----
    indradhanu_env: Environment = "development"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173"
    # A regex matched against the whole Origin header, for origins that cannot
    # be enumerated. Every Vercel preview deploy gets its own hostname, so the
    # alternative is editing an environment variable on every push.
    cors_origin_regex: str = ""
    # Where the PWA is served, for the phone app to open `/citizen` (residents) and
    # `/field` (crews and staff) in its online mode; the path is added per role.
    # Empty means "the first CORS origin that is not localhost".
    public_app_url: str = "https://distro-ruddy.vercel.app"

    # ---- database ----
    supabase_url: str = ""
    supabase_anon_public_key: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_key: str = ""
    supabase_transaction_pooler: str = ""
    supabase_direct_connection_string: str = ""
    db_pool_min: int = 2
    db_pool_max: int = 10

    # ---- development convenience ----
    # When set AND the environment is development, an unauthenticated request is
    # treated as this role instead of as an anonymous citizen. It exists so the
    # API can be exercised before any Supabase user has been created, which is
    # otherwise a chicken-and-egg problem: POST /runs needs a staff token, and
    # the only way to get one is to have already set up a staff profile.
    #
    # The validator below refuses to start if this is set outside development.
    # A convenience that can be left switched on by accident in production is
    # not a convenience, it is an unauthenticated admin endpoint.
    dev_auth_role: str = ""

    # ---- event router ----
    # Always on: whether the system reacts to a report must not depend on the
    # demo runner. Off only for tests that want a silent database.
    event_router_enabled: bool = True
    event_router_poll_s: float = 1.5
    #: Run the router's re-plans through the LangGraph agent graph
    #: (app/agents/graph.py). Falls back to a direct re-plan when langgraph is
    #: not installed.
    agent_graph_enabled: bool = True
    #: Ask an officer before a plan pulls a unit off an incident of this
    #: severity or worse. 0 disables the human-approval step.
    agent_graph_approval_severity: int = 4
    #: Seconds an approval may wait before the graph proceeds on the safe
    #: default (keep current assignments).
    agent_graph_approval_timeout_s: int = 300
    #: memory | postgres. Postgres needs langgraph-checkpoint-postgres.
    agent_graph_checkpointer: str = "memory"
    #: Read each report's severity from its text with the zero-shot classifier
    #: and then the LLM (bounded; app/incidents/severity.py). Off = keywords only.
    severity_models_enabled: bool = True

    # ---- llm ----
    llm_provider: LLMProvider = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    aws_region: str = "ap-south-1"
    aws_api_key_bedrock_for_xai: str = ""
    bedrock_model_id: str = ""

    # ---- llm cost controls (app/agents/llm_cost.py) ----
    #: A cheaper model for the short structured tasks (routing, category,
    #: severity, duplicate adjudication). Empty = use the main model for all.
    gemini_model_small: str = ""
    bedrock_model_id_small: str = ""
    #: Bedrock prompt caching (cachePoint after the system prompt). Only some
    #: Bedrock models support it; leave off unless yours does.
    bedrock_prompt_cache: bool = False
    #: Token budget, shared by every model call in this process (or the whole
    #: fleet when REDIS_URL is set). Over budget, calls get the deterministic
    #: answer: low priority sheds first, life-safety last.
    llm_tokens_per_minute: int = 300_000
    llm_tokens_per_day: int = 20_000_000
    #: Model calls in flight at once (bulkhead). Low-priority calls degrade
    #: rather than queue once it is three-quarters full.
    llm_max_concurrency: int = 32
    llm_cache_enabled: bool = True
    llm_cache_max_entries: int = 20_000
    #: USD per 1M tokens, "model=in/out,model=in/out". Unknown models report
    #: tokens only, never a guessed cost.
    llm_prices: str = "gemini-2.5-flash=0.30/2.50,gemini-2.5-flash-lite=0.10/0.40"

    # ---- scale (app/core/shared.py, app/core/ingest.py) ----
    #: Share of fast, successful requests that get an access-log line. Errors,
    #: 4xx/5xx and slow requests are always logged. 1.0 in development; at
    #: thousands of requests a second, logging every one is the bottleneck.
    access_log_sample: float = 1.0
    #: Shared state for more than one replica: rate limits, LLM cache and
    #: budget, ingest streams, the router's leader lease. Empty = in-process,
    #: which is correct for one replica and wrong for many.
    redis_url: str = ""
    #: Ingest queue sizes per lane. A full bulk lane sheds (503 + Retry-After);
    #: the life-safety lane never sheds, it writes through instead.
    ingest_critical_max: int = 50_000
    ingest_standard_max: int = 100_000
    ingest_bulk_max: int = 200_000
    ingest_batch_size: int = 500
    ingest_flush_ms: int = 50
    ingest_workers: int = 4
    #: The event router runs on one replica: the one holding this lease.
    router_lease_s: int = 15

    # ---- feeds ----
    open_meteo_forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_flood_url: str = "https://flood-api.open-meteo.com/v1/flood"
    open_meteo_air_url: str = "https://air-quality-api.open-meteo.com/v1/air-quality"
    osrm_url: str = "https://router.project-osrm.org"
    #: Mapbox is the preferred router: the public OSRM demo server is rate
    #: limited and refuses the Matrix sizes this needs, and Mapbox returns turn
    #: instructions with street names, which is the difference between "go 2.1 km
    #: north" and "left onto Karve Road". Same token the map already uses.
    mapbox_token: str = ""
    mapbox_directions_url: str = "https://api.mapbox.com/directions/v5/mapbox"
    mapbox_matrix_url: str = "https://api.mapbox.com/directions-matrix/v1/mapbox"
    #: Hugging Face Inference API, for the multilingual zero-shot classifier that
    #: backs the report parser when its keyword pass is not confident.
    hf_api_token: str = ""
    hf_zero_shot_model: str = "joeddav/xlm-roberta-large-xnli"
    #: Sarvam AI, for speech to text. Trained on Indian languages including the
    #: code-mixed speech people actually use, which is the reason to prefer it
    #: over a general multilingual recogniser here: the general ones drop
    #: exactly the English loan words that carry the location.
    #: Both spellings are accepted because both are plausible names for it.
    sarvam_api_key: str = ""
    sarvam_ai_api_key: str = ""
    sarvam_stt_model: str = "saarika:v2.5"
    sarvam_stt_translate_model: str = "saaras:v2.5"

    #: A vision model hosted elsewhere. Ours only has to post an image and read
    #: back the JSON in `app/incidents/vision.py`. Empty means photos are
    #: accepted and simply not looked at, which is a fine state to ship in.
    vlm_url: str = ""
    vlm_api_key: str = ""

    #: Offline mesh (bitchat). `mesh_hmac_key` signs and verifies IDX1 packets:
    #: the camera node, the citizen PWA's mesh mode and this API share it. Empty
    #: means packets are accepted unsigned and scored as such (never auto-
    #: dispatch on their own). `mesh_gateway_key` is what a gateway phone or the
    #: bridge script sends as `X-Mesh-Gateway-Key`; empty disables the mesh
    #: endpoints entirely rather than leaving them open.
    mesh_hmac_key: str = ""
    mesh_gateway_key: str = ""
    vlm_model: str = ""
    #: 120s, not 25. A 3B answers a 1024 px image in about six seconds, but the
    #: first call after the model server starts spends up to a minute reading
    #: weights off disk, and the GPU serves one request at a time so a second
    #: photo queues behind the first. A 25s ceiling turned both of those normal
    #: states into "vision unavailable".
    vlm_timeout_seconds: float = 120.0

    #: Google Flood Hub, if a key is present. Without one the flood forecast
    #: comes from GloFAS through Open-Meteo, which needs no key.
    google_flood_hub_key: str = ""
    google_flood_hub_url: str = "https://floodforecasting.googleapis.com/v1"
    nasa_firms_key: str = ""
    data_gov_in_key: str = ""
    feed_timeout_seconds: float = 12.0
    feed_cache_ttl_seconds: int = 900

    @field_validator("dev_auth_role")
    @classmethod
    def _dev_role_is_development_only(cls, v: str, info) -> str:
        v = (v or "").strip()
        if not v:
            return ""
        env = (info.data or {}).get("indradhanu_env", "development")
        if env != "development":
            raise ValueError(
                "DEV_AUTH_ROLE is set but INDRADHANU_ENV is "
                f"{env!r}. This bypasses authentication entirely and is refused "
                "outside development. Unset it."
            )
        allowed = {"citizen", "field_operator", "ward_officer", "commissioner", "admin"}
        if v not in allowed:
            raise ValueError(
                f"DEV_AUTH_ROLE must be one of {sorted(allowed)}; got {v!r}."
            )
        return v

    @field_validator("cors_origins", "cors_origin_regex")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip().strip("\"'")

    @property
    def cors_origin_list(self) -> list[str]:
        """The allowed origins, normalised.

        An origin is a scheme, a host and an optional port -- nothing else. A
        browser sends exactly that in the `Origin` header, and CORSMiddleware
        compares it by string equality, so `https://app.vercel.app/` with its
        trailing slash never matches and `"https://app.vercel.app"` carrying the
        quotes a dashboard let someone paste never matches either. Both are
        invisible server-side: the API answers 200 and the browser throws the
        response away. Normalise here rather than asking every deployment to get
        the punctuation right.
        """
        out: list[str] = []
        for raw in self.cors_origins.split(","):
            o = raw.strip().strip("\"'").rstrip("/")
            if o:
                out.append(o)
        return out

    @property
    def is_production(self) -> bool:
        return self.indradhanu_env == "production"

    @property
    def dsn(self) -> str:
        """Pooled connection for the API. Falls back to the direct string so a
        developer with only one of the two configured still boots."""
        return self.supabase_transaction_pooler or self.supabase_direct_connection_string

    @property
    def migration_dsn(self) -> str:
        """Direct connection: pgbouncer in transaction mode cannot run DDL or
        hold advisory locks."""
        return self.supabase_direct_connection_string or self.supabase_transaction_pooler


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
