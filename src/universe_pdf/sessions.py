import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .formats import SUPPORTED


class SessionError(Exception):
    pass


@dataclass
class Session:
    directory: Path
    files: list[Path] = field(default_factory=list)
    total_bytes: int = 0
    touched: float = field(default_factory=time.monotonic)
    busy: bool = False
    cancelled: bool = False
    sending: bool = False


class SessionStore:
    def __init__(
        self,
        root,
        *,
        max_files=100,
        max_file_bytes=20_000_000,
        max_total_bytes=100_000_000,
        max_sessions=20,
        ttl_seconds=3600,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self.sessions: dict[tuple[int, int], Session] = {}

    def get(self, key):
        if key not in self.sessions:
            self.expire()
            if len(self.sessions) >= self.max_sessions:
                raise SessionError("Слишком много активных очередей. Попробуйте позже.")
            directory = self.root / uuid4().hex
            directory.mkdir()
            self.sessions[key] = Session(directory)
        session = self.sessions[key]
        session.touched = time.monotonic()
        return session

    def allocate(self, session, name, size):
        if session.busy:
            raise SessionError("PDF уже собирается. Дождитесь результата или нажмите /cancel.")
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED:
            raise SessionError("Этот формат не поддерживается. Список форматов: /help.")
        if len(session.files) >= self.max_files:
            raise SessionError(f"Лимит очереди: {self.max_files} файлов. Нажмите /compile.")
        if size is not None and (size > self.max_file_bytes or size < 0):
            raise SessionError(
                f"Превышен размер файла: максимум {self.max_file_bytes // 1_000_000} МБ."
            )
        if session.total_bytes + (size or 0) > self.max_total_bytes:
            raise SessionError("Превышен общий размер очереди. Соберите PDF или нажмите /cancel.")
        return session.directory / f"{uuid4().hex}{suffix}"

    def accept(self, session, path):
        size = path.stat().st_size
        if (
            size == 0
            or size > self.max_file_bytes
            or session.total_bytes + size > self.max_total_bytes
        ):
            path.unlink(missing_ok=True)
            raise SessionError("Недопустимый размер файла или превышен общий размер очереди.")
        session.files.append(path)
        session.total_bytes += size
        session.touched = time.monotonic()

    def reset(self, key):
        session = self.sessions.pop(key, None)
        if session:
            shutil.rmtree(session.directory)

    def expire(self, now=None):
        now = time.monotonic() if now is None else now
        for key, session in list(self.sessions.items()):
            if not session.busy and now - session.touched > self.ttl_seconds:
                self.reset(key)
