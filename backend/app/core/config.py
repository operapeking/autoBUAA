from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="AUTOBUAA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Path("./data")
    port: int = 8080
    log_level: str = "DEBUG"
    timezone: str = "Asia/Shanghai"
    secret_key_file: Path | None = None
    cookie_secure: bool = False
    scheduler_poll_seconds: float = Field(default=1.0, ge=0.2, le=60)
    scheduler_concurrency: int = Field(default=4, ge=1, le=16)
    frontend_dist_dir: Path = Path("frontend/dist")
    admin_session_ttl_hours: int = 168

    @property
    def db_path(self) -> Path:
        return self.data_dir / "autobuaa.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.resolve().as_posix()}"

    @property
    def captcha_dir(self) -> Path:
        return self.data_dir / "captcha"

    @property
    def resolved_secret_key_file(self) -> Path:
        return self.secret_key_file or (self.data_dir / "secret.key")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.captcha_dir.mkdir(parents=True, exist_ok=True)
        self.resolved_secret_key_file.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
