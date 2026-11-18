"""Dashboard settings loaded from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Optional PostHog configuration for the dashboard server."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    posthog_project_token: str | None = None
    posthog_host: str | None = None

    @property
    def posthog_configured(self) -> bool:
        """Whether both values needed to send PostHog events are available."""
        return bool(self.posthog_project_token and self.posthog_host)

    @property
    def missing_posthog_variables(self) -> tuple[str, ...]:
        """Return the PostHog variables that have no usable value."""
        return tuple(
            name
            for name, value in (
                ("POSTHOG_PROJECT_TOKEN", self.posthog_project_token),
                ("POSTHOG_HOST", self.posthog_host),
            )
            if not value
        )

    @property
    def is_production(self) -> bool:
        """Avoid development configuration warnings in production."""
        return self.environment.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """Get cached settings so every caller shares one configuration."""
    return Settings()
