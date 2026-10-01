from __future__ import annotations

import fcntl
from pathlib import Path
from types import TracebackType
from typing import IO

from paperfrog.domain.types import SafeError


class InstanceLock:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.file: IO[str] = (directory / "service.lock").open("a+")

    def __enter__(self) -> InstanceLock:
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise SafeError("another service owns this state directory") from None
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()
