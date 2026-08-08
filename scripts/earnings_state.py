"""Transactional persistence for earnings runtime state."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar


KNOWN_SECTIONS = (
    "public",
    "private",
    "quotes",
    "signal_queue",
    "manual_signal_drafts",
)

SAFETY_CRITICAL_SECTIONS = (
    "public",
    "private",
    "signal_queue",
    "manual_signal_drafts",
)

T = TypeVar("T")


class EarningsStateError(RuntimeError):
    """Base error for state access and validation failures."""


class EarningsStateValidationError(EarningsStateError):
    """Raised when duplicate-protection state is unsafe to use."""


_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def _thread_lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())

    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _lock_file(file_object) -> None:
    if os.name == "nt":
        import msvcrt

        file_object.seek(0)
        while True:
            try:
                msvcrt.locking(
                    file_object.fileno(),
                    msvcrt.LK_NBLCK,
                    1,
                )
                return
            except OSError:
                time.sleep(0.05)

    import fcntl

    fcntl.flock(file_object.fileno(), fcntl.LOCK_EX)


def _unlock_file(file_object) -> None:
    if os.name == "nt":
        import msvcrt

        file_object.seek(0)
        msvcrt.locking(
            file_object.fileno(),
            msvcrt.LK_UNLCK,
            1,
        )
        return

    import fcntl

    fcntl.flock(file_object.fileno(), fcntl.LOCK_UN)


class EarningsStateStore:
    """Read and update one earnings state file safely across processes."""

    def __init__(self, state_file: Path | str):
        self.state_file = Path(state_file)
        self.lock_file = self.state_file.with_suffix(".lock")
        self._thread_lock = _thread_lock_for(self.lock_file)

    @staticmethod
    def empty_state() -> dict[str, Any]:
        return {
            section: {}
            for section in KNOWN_SECTIONS
        }

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.state_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with self._thread_lock:
            with self.lock_file.open("a+b") as lock_handle:
                lock_handle.seek(0, os.SEEK_END)
                if lock_handle.tell() == 0:
                    lock_handle.write(b"\0")
                    lock_handle.flush()

                _lock_file(lock_handle)
                try:
                    yield
                finally:
                    _unlock_file(lock_handle)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return self.empty_state()

        try:
            raw_state = json.loads(
                self.state_file.read_text(encoding="utf-8")
            )
        except json.JSONDecodeError as exc:
            raise EarningsStateValidationError(
                f"Earnings state is not valid JSON: {self.state_file}"
            ) from exc
        except OSError as exc:
            raise EarningsStateError(
                f"Could not read earnings state: {self.state_file}"
            ) from exc

        if not isinstance(raw_state, dict):
            raise EarningsStateValidationError(
                "Earnings state must be a JSON object."
            )

        state = copy.deepcopy(raw_state)

        for section in SAFETY_CRITICAL_SECTIONS:
            value = state.get(section)
            if value is None and section not in state:
                state[section] = {}
                continue
            if not isinstance(value, dict):
                raise EarningsStateValidationError(
                    f"Earnings state section '{section}' must be an object."
                )

        quotes = state.get("quotes")
        if quotes is None and "quotes" not in state:
            state["quotes"] = {}
        elif not isinstance(quotes, dict):
            state["quotes"] = {}

        return state

    def _write_unlocked(self, state: dict[str, Any]) -> None:
        temporary_path: Path | None = None

        try:
            file_descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{self.state_file.name}.",
                suffix=".tmp",
                dir=self.state_file.parent,
                text=True,
            )
            temporary_path = Path(raw_path)

            with os.fdopen(
                file_descriptor,
                "w",
                encoding="utf-8",
                newline="\n",
            ) as temporary_file:
                json.dump(
                    state,
                    temporary_file,
                    indent=2,
                    sort_keys=True,
                )
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            os.replace(temporary_path, self.state_file)
            temporary_path = None
        except OSError as exc:
            raise EarningsStateError(
                f"Could not write earnings state: {self.state_file}"
            ) from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    def load(self) -> dict[str, Any]:
        with self._locked():
            return self._read_unlocked()

    def replace(self, state: dict[str, Any]) -> dict[str, Any]:
        """Write an explicit snapshot; intended for initialization/tests."""
        if not isinstance(state, dict):
            raise EarningsStateValidationError(
                "Earnings state must be a JSON object."
            )

        replacement = copy.deepcopy(state)
        for section in KNOWN_SECTIONS:
            replacement.setdefault(section, {})

        for section in SAFETY_CRITICAL_SECTIONS:
            if not isinstance(replacement[section], dict):
                raise EarningsStateValidationError(
                    f"Earnings state section '{section}' must be an object."
                )

        if not isinstance(replacement["quotes"], dict):
            replacement["quotes"] = {}

        with self._locked():
            self._write_unlocked(replacement)

        return copy.deepcopy(replacement)

    def transaction(
        self,
        mutation: Callable[[dict[str, Any]], T],
    ) -> tuple[dict[str, Any], T]:
        """Reload, mutate, and atomically persist state under one lock."""
        with self._locked():
            state = self._read_unlocked()
            result = mutation(state)

            for section in SAFETY_CRITICAL_SECTIONS:
                if not isinstance(state.get(section), dict):
                    raise EarningsStateValidationError(
                        f"Earnings state section '{section}' must be an object."
                    )

            if not isinstance(state.get("quotes"), dict):
                state["quotes"] = {}

            self._write_unlocked(state)

        return copy.deepcopy(state), result
