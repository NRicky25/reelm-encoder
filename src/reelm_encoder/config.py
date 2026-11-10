"""Settings, read from environment variables / reelm-encoder/.env (all optional)."""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Folder containing ffmpeg/ffprobe, if they aren't on PATH.
    ffmpeg_dir: Path | None = None

    # Where `reelm-encoder encode` writes when no --out is given.
    work_dir: Path = PROJECT_ROOT / "output"

    # Where you host the uploaded folders (e.g. https://media.reelm.tv). Only used to
    # show each video's full link; R2_PUBLIC_URL from older setups is accepted too.
    public_url: str | None = Field(
        default=None, validation_alias=AliasChoices("public_url", "r2_public_url")
    )

    @field_validator("ffmpeg_dir", "public_url", mode="before")
    @classmethod
    def empty_is_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    def video_link(self, folder_name: str) -> str | None:
        """The link a folder will have once uploaded under videos/."""
        if not self.public_url:
            return None
        return f"{self.public_url.rstrip('/')}/videos/{folder_name}/master.m3u8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
