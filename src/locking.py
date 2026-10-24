"""Bloqueo entre procesos para evitar organizaciones simultáneas."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from .paths import get_lock_path

LOGGER = logging.getLogger(__name__)


class LockBusyError(RuntimeError):
    """Ya existe otra organización en curso."""


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                ctypes.c_uint32,
                ctypes.c_int,
                ctypes.c_uint32,
            ]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            process_query_limited_information = 0x1000
            still_active = 259
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, int(pid)
            )
            if not handle:
                # ERROR_ACCESS_DENIED significa que el proceso existe.
                return ctypes.get_last_error() == 5
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            return True

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_lock(path: Path) -> dict[str, object] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


class FileLock:
    """Bloqueo exclusivo basado en un archivo con detección de bloqueos obsoletos.

    No usa privilegios elevados ni bloqueos del registro de Windows. El archivo
    contiene únicamente el PID, el equipo y una marca temporal; no se guardan
    nombres de archivos ni otros datos personales.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        stale_after_seconds: float = 12 * 60 * 60,
    ) -> None:
        self.path = Path(path) if path else get_lock_path()
        self.stale_after_seconds = max(60.0, float(stale_after_seconds))
        self.token = uuid.uuid4().hex
        self._acquired = False

    def _is_stale(self, data: dict[str, object] | None, age: float) -> bool:
        if age >= self.stale_after_seconds:
            return True
        if not data:
            # Un archivo recién creado puede estar todavía escribiéndose.
            return age > 10
        try:
            pid = int(data.get("pid", -1))
        except (TypeError, ValueError):
            return age > 10
        if pid == os.getpid() and not self._acquired:
            # Puede ser otro hilo/proceso lógico de la misma aplicación.
            return False
        if not _pid_is_running(pid):
            return True
        created = data.get("created_utc")
        if isinstance(created, (int, float)):
            return (time.time() - float(created)) >= self.stale_after_seconds
        return False

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "token": self.token,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "created_utc": time.time(),
            "created_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        encoded = json.dumps(payload, ensure_ascii=True)

        try:
            descriptor = os.open(
                str(self.path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                age = max(0.0, time.time() - self.path.stat().st_mtime)
            except OSError:
                age = 0.0
            data = _read_lock(self.path)
            if self._is_stale(data, age):
                try:
                    self.path.unlink()
                    LOGGER.warning("Se eliminó un bloqueo obsoleto: %s", self.path)
                    return self.acquire()
                except OSError:
                    pass
            holder = ""
            if data:
                holder = f" (PID {data.get('pid', '?')})"
            raise LockBusyError(
                f"Ya hay una organización en curso{holder}. "
                "Espera a que termine o elimina el bloqueo solo si sabes que no hay "
                f"ningún proceso activo: {self.path}"
            )
        except OSError as exc:
            raise LockBusyError(f"No se pudo crear el bloqueo: {exc}") from exc

        try:
            os.write(descriptor, encoded.encode("utf-8"))
            os.fsync(descriptor)
        except OSError as exc:
            try:
                os.close(descriptor)
            finally:
                self.path.unlink(missing_ok=True)
            raise LockBusyError(f"No se pudo escribir el bloqueo: {exc}") from exc
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

        self._acquired = True
        LOGGER.debug("Bloqueo adquirido: %s", self.path)
        return self

    def release(self) -> None:
        if not self._acquired:
            return
        data = _read_lock(self.path)
        if data and data.get("token") == self.token:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("No se pudo liberar el bloqueo %s", self.path)
        self._acquired = False
        LOGGER.debug("Bloqueo liberado: %s", self.path)

    @property
    def acquired(self) -> bool:
        return self._acquired

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
