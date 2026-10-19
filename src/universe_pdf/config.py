import os
from dataclasses import dataclass, field, fields

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    allowed_user_ids: frozenset[int] = frozenset()
    font_path: str = ""
    office_binary: str = "soffice"
    max_files: int = 100
    max_file_bytes: int = 20_000_000
    max_total_bytes: int = 100_000_000
    max_output_bytes: int = 49_000_000
    max_pages: int = 500
    max_sessions: int = 20
    ttl_seconds: int = 3600
    conversion_timeout: int = 180
    office_timeout: int = 60
    conversion_concurrency: int = 1

    @classmethod
    def from_env(cls, *, load_file=True):
        if load_file:
            load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Укажите BOT_TOKEN в .env или переменных окружения.")
        options = {"token": token}
        for item in fields(cls):
            if item.type is int:
                value = int(os.getenv(item.name.upper(), str(item.default)))
                if value <= 0:
                    raise ValueError(f"{item.name.upper()} должно быть больше нуля.")
                options[item.name] = value
        if options["max_file_bytes"] > 20_000_000:
            raise ValueError("MAX_FILE_BYTES не может превышать 20000000.")
        if options["max_output_bytes"] > 49_000_000:
            raise ValueError("MAX_OUTPUT_BYTES не может превышать 49000000.")
        options["allowed_user_ids"] = frozenset(
            int(value.strip())
            for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
            if value.strip()
        )
        options["font_path"] = os.getenv("FONT_PATH", "")
        options["office_binary"] = os.getenv("OFFICE_BINARY", "soffice")
        return cls(**options)
