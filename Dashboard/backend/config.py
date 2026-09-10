# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Configuration module for AI-Powered Digital Operations Cockpit.
# Date: 2025-07-14
# ---------------------------------------------------------------------------
"""
Configuration module for AI-Powered Digital Operations Cockpit.
Loads settings from Dashboard/.env via pydantic-settings.
"""

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from security.crypto import decrypt_value


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent.parent / ".env"),
        extra="ignore",
    )

    # ServiceNow connection
    SERVICENOW_BASE_URL: str = Field(
        default="",
        description="Base URL of the ServiceNow instance, e.g. https://dev12345.service-now.com",
    )
    SERVICENOW_USERNAME: str = Field(default="", description="ServiceNow basic-auth username")
    SERVICENOW_PASSWORD: str = Field(default="", description="ServiceNow basic-auth password")
    SERVICENOW_PASSWORD_ENCRYPTED: str = Field(
        default="",
        description="Fernet-encrypted ServiceNow password; preferred over SERVICENOW_PASSWORD",
    )
    SERVICENOW_VERIFY_SSL: bool = Field(
        default=True, description="Whether to verify SSL certificates when calling ServiceNow"
    )
    SERVICENOW_TIMEOUT_SECONDS: int = Field(
        default=60, description="HTTP request timeout in seconds"
    )

    # Optional offline fallback
    XLSX_DATA_PATH: Optional[Path] = Field(
        default=None,
        description="Path to an XLSX workbook used as an offline data fallback",
    )

    # Caching
    CACHE_TTL_SECONDS: int = Field(
        default=3600, description="How long (seconds) a cached data-set is considered fresh"
    )

    # Ollama LLM (local GPU inference)
    OLLAMA_BASE_URL: str = Field(
        default="http://localhost:11434",
        description="Base URL for the Ollama API server",
    )
    OLLAMA_MODEL: str = Field(
        default="qwen3.5:9b",
        description="Ollama model name to use for insight generation (e.g. llama3.1:8b, llama3, mistral)",
    )
    OLLAMA_ENABLED: bool = Field(
        default=True,
        description="Enable Ollama LLM insights; falls back to rule-based if False or unreachable",
    )
    OLLAMA_TIMEOUT_SECONDS: int = Field(
        default=120,
        description="Timeout for Ollama API requests in seconds",
    )

    # Qdrant vector store (used for critical alert persistence)
    QDRANT_URL: str = Field(
        default="http://localhost:6333",
        description="Qdrant server URL for critical alert persistence",
    )
    QDRANT_COLLECTION: str = Field(
        default="dashboard_critical_alerts",
        description="Qdrant collection name for critical incidents",
    )
    QDRANT_TIMEOUT_SECONDS: int = Field(
        default=10,
        description="Timeout for Qdrant API calls in seconds",
    )
    QDRANT_ENABLED: bool = Field(
        default=True,
        description="Enable Qdrant persistence for critical incidents",
    )

    # Auto-sync scheduler
    AUTO_SYNC_INTERVAL_MINUTES: int = Field(
        default=10,
        description="How often (minutes) to automatically re-sync data from ServiceNow",
    )

    # Browser CORS origins (comma-separated)
    CORS_ORIGINS: str = Field(
        default=(
            "https://stratapp.org,https://www.stratapp.org,https://api.stratapp.org,"
            "http://localhost,http://127.0.0.1,http://localhost:8090,http://127.0.0.1:8090"
        ),
        description="Comma-separated allowed CORS origins for browser calls",
    )

    # PostgreSQL — encrypted settings persistence (ServiceNow connection).
    # Required, no default: shares the same default postgres/postgres role
    # already used by Novastra-ITSM on this box (own table, dashboard_ prefix).
    POSTGRES_DSN: str = Field(
        ...,
        description="PostgreSQL DSN for encrypted settings persistence. Required — no hardcoded default.",
    )
    # 0, not 1: see db.py's _get_pool() for why a floor here causes background
    # reconnect-loop churn against an unconfigured/placeholder password.
    POSTGRES_POOL_MIN_SIZE: int = Field(default=0, description="Minimum pool size")
    POSTGRES_POOL_MAX_SIZE: int = Field(default=5, description="Maximum pool size")
    # Kept short deliberately: the lifespan startup hook (main.py) reads persisted
    # settings before Uvicorn binds its port, so a slow/failing DB (e.g. POSTGRES_DSN
    # not yet configured with a real password) delays the port coming up — observed
    # in production racing the watchdog's liveness check and causing a spurious extra
    # restart. 10s was too patient for a value on the hot startup path.
    POSTGRES_POOL_TIMEOUT_SECONDS: float = Field(default=3.0, description="Pool checkout timeout (seconds)")

    def model_post_init(self, __context: object) -> None:
        """Resolve an encrypted deployment password only inside the backend process."""
        if self.SERVICENOW_PASSWORD_ENCRYPTED:
            self.SERVICENOW_PASSWORD = decrypt_value(self.SERVICENOW_PASSWORD_ENCRYPTED) or ""


# Module-level singleton – import this throughout the app
settings = Settings()
