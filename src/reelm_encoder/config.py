"""Settings, read from environment variables / reelm-encoder/.env."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Cloudflare R2 (R2 > Manage API tokens > "Object Read & Write" on the bucket).
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_bucket: str = "reelm-videos"
    # Where the bucket is served from: the r2.dev "Public Development URL" for
    # testing, or your own domain (e.g. https://media.reelm.tv) in production.
    r2_public_url: str | None = None

    # Folder containing ffmpeg/ffprobe, if they aren't on PATH.
    ffmpeg_dir: Path | None = None

    # Where encoded files are written before uploading.
    work_dir: Path = PROJECT_ROOT / "output"

    @field_validator(
        "r2_account_id",
        "r2_access_key_id",
        "r2_secret_access_key",
        "r2_public_url",
        "ffmpeg_dir",
        mode="before",
    )
    @classmethod
    def empty_is_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def r2_ready(self) -> bool:
        return bool(
            self.r2_account_id
            and self.r2_access_key_id
            and self.r2_secret_access_key
            and self.r2_public_url
        )

    def missing_r2(self) -> list[str]:
        names = {
            "R2_ACCOUNT_ID": self.r2_account_id,
            "R2_ACCESS_KEY_ID": self.r2_access_key_id,
            "R2_SECRET_ACCESS_KEY": self.r2_secret_access_key,
            "R2_PUBLIC_URL": self.r2_public_url,
        }
        return [name for name, value in names.items() if not value]


@lru_cache
def get_settings() -> Settings:
    return Settings()
