"""Environment-backed application settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

DEFAULT_IMPORT_MAX_FILES = 1000
DEFAULT_IMPORT_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_IMPORT_MAX_TOTAL_BYTES = 200 * 1024 * 1024
DEFAULT_IMPORT_MAX_COMPRESSION_RATIO = 100

_STRICT_INTEGER_ENV_FIELDS = {
    "import_max_files",
    "import_max_file_bytes",
    "import_max_total_bytes",
    "import_max_compression_ratio",
}


class _StrictIntegerEnvSettingsSource(EnvSettingsSource):
    """Parse integer environment syntax before strict model validation."""

    def prepare_field_value(
        self,
        field_name: str,
        field: FieldInfo,
        value: Any,
        value_is_complex: bool,
    ) -> Any:
        if (
            field_name in _STRICT_INTEGER_ENV_FIELDS
            and isinstance(value, str)
            and value.isascii()
            and value.isdecimal()
        ):
            value = int(value)
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class Settings(BaseSettings):
    """Runtime settings loaded independently for each application instance."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)

    data_dir: Path = Field(default=Path("data"), validation_alias="NOVEL_DATA_DIR")
    static_dir: Path = Field(default=Path("static"), validation_alias="NOVEL_STATIC_DIR")
    ai_provider: Literal["demo", "local"] = Field(
        default="demo", validation_alias="NOVEL_AI_PROVIDER"
    )
    local_model_url: str = Field(
        default="http://127.0.0.1:11434", validation_alias="NOVEL_LOCAL_MODEL_URL"
    )
    local_text_model: str = Field(default="", validation_alias="NOVEL_LOCAL_TEXT_MODEL")
    local_embedding_model: str = Field(default="", validation_alias="NOVEL_LOCAL_EMBEDDING_MODEL")
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    text_model: str = Field(default="gpt-5.6-terra", validation_alias="NOVEL_TEXT_MODEL")
    image_model: str = Field(default="gpt-image-2", validation_alias="NOVEL_IMAGE_MODEL")
    cors_origins: str = Field(
        default=(
            "http://localhost:4173,http://127.0.0.1:4173,"
            "http://localhost:5173,http://127.0.0.1:5173"
        ),
        validation_alias="NOVEL_CORS_ORIGINS",
    )
    import_max_files: int = Field(
        default=DEFAULT_IMPORT_MAX_FILES,
        strict=True,
        ge=1,
        le=DEFAULT_IMPORT_MAX_FILES,
        validation_alias="NOVEL_IMPORT_MAX_FILES",
    )
    import_max_file_bytes: int = Field(
        default=DEFAULT_IMPORT_MAX_FILE_BYTES,
        strict=True,
        ge=1024,
        le=DEFAULT_IMPORT_MAX_FILE_BYTES,
        validation_alias="NOVEL_IMPORT_MAX_FILE_BYTES",
    )
    import_max_total_bytes: int = Field(
        default=DEFAULT_IMPORT_MAX_TOTAL_BYTES,
        strict=True,
        ge=1024,
        le=DEFAULT_IMPORT_MAX_TOTAL_BYTES,
        validation_alias="NOVEL_IMPORT_MAX_TOTAL_BYTES",
    )
    import_max_compression_ratio: int = Field(
        default=DEFAULT_IMPORT_MAX_COMPRESSION_RATIO,
        strict=True,
        ge=1,
        le=DEFAULT_IMPORT_MAX_COMPRESSION_RATIO,
        validation_alias="NOVEL_IMPORT_MAX_COMPRESSION_RATIO",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del cls, env_settings
        return (
            init_settings,
            _StrictIntegerEnvSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    @model_validator(mode="after")
    def validate_import_limits(self) -> Settings:
        if self.import_max_file_bytes > self.import_max_total_bytes:
            raise ValueError("import_max_file_bytes cannot exceed import_max_total_bytes")
        return self

    @property
    def database_path(self) -> Path:
        return self.data_dir / "novel-harness.db"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    def prepare_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
