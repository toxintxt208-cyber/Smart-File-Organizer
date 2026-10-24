"""Detección opcional de archivos nuevos con watchdog.

Si watchdog no está instalado, el resto de la aplicación sigue funcionando en
modo manual y programado. Este módulo nunca importa dependencias adicionales.
"""

from __future__ import annotations

import logging
import os
import queue
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:  # dependencia opcional
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover - depende del entorno
    WATCHDOG_AVAILABLE = False
    FileSystemEvent = Any  # type: ignore[assignment,misc]
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)

WatchCallback = Callable[[Path], None]
StatusCallback = Callable[[str, str], None]


class FileWatcher:
    """Vigila solo el primer nivel de una carpeta y espera a que el archivo se estabilice."""

    available = WATCHDOG_AVAILABLE

    def __init__(
        self,
        path: Path | str,
        callback: WatchCallback,
        *,
        settle_seconds: float = 5.0,
        poll_interval: float = 1.0,
        on_event: StatusCallback | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.callback = callback
        self.settle_seconds = max(0.5, float(settle_seconds))
        self.poll_interval = max(0.2, float(poll_interval))
        self.on_event = on_event
        self._events: queue.Queue[str] = queue.Queue()
        self._pending: dict[str, dict[str, Any]] = {}
        self._processed: dict[str, tuple[int, int]] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer: Any = None
        self._paused = False
        self._state_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event

    @property
    def paused(self) -> bool:
        return self._paused

    def _emit(self, level: str, message: str) -> None:
        LOGGER.log(logging.ERROR if level == "error" else logging.INFO, "%s", message)
        if self.on_event is not None:
            try:
                self.on_event(level, message)
            except Exception:
                LOGGER.exception("Error en callback de vigilancia")

    def start(self) -> "FileWatcher":
        if not self.available:
            raise RuntimeError(
                "watchdog no está instalado. Instala la dependencia opcional con "
                "'pip install watchdog'."
            )
        with self._state_lock:
            if self.running:
                return self
            if not self.path.exists() or not self.path.is_dir():
                raise ValueError(f"La carpeta a vigilar no es válida: {self.path}")
            self._stop_event.clear()
            self._paused = False
            self._thread = threading.Thread(
                target=self._worker,
                name="smart-file-organizer-watcher",
                daemon=True,
            )
            self._thread.start()
            try:
                self._start_observer()
            except Exception:
                self._stop_event.set()
                self._events.put("__wake__")
                raise
            self._emit("info", f"Vigilando archivos nuevos en {self.path}")
        return self

    def _start_observer(self) -> None:
        if Observer is None:
            return
        observer = Observer()
        observer.schedule(_EventHandler(self), str(self.path), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer

    def _stop_observer(self) -> None:
        observer = self._observer
        self._observer = None
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=3.0)
            except Exception:
                LOGGER.exception("No se pudo detener el observador de archivos")

    def pause(self) -> None:
        with self._state_lock:
            if self._paused:
                return
            self._paused = True
            self._stop_observer()
            self._emit("info", "Vigilancia en pausa.")
            self._events.put("__wake__")

    def resume(self) -> None:
        with self._state_lock:
            if not self.running:
                raise RuntimeError("La vigilancia no está iniciada.")
            if not self._paused:
                return
            self._paused = False
            self._start_observer()
            self._emit("info", "Vigilancia reanudada.")
            self._events.put("__wake__")

    def stop(self, *, wait: bool = True) -> None:
        self._stop_event.set()
        self._events.put("__wake__")
        self._stop_observer()
        thread = self._thread
        if wait and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._emit("info", "Vigilancia detenida.")

    def status_text(self) -> str:
        if not self.running:
            return "Vigilancia detenida"
        if self._paused:
            return "Vigilancia en pausa"
        return f"Vigilando: {self.path}"

    def _enqueue(self, raw_path: str) -> None:
        if not raw_path or raw_path == "__wake__":
            return
        self._events.put(raw_path)

    def _add_pending(self, raw_path: str) -> None:
        try:
            path = Path(raw_path)
            info = os.lstat(path)
        except (OSError, ValueError):
            return
        if not stat.S_ISREG(info.st_mode):
            return
        key = os.path.normcase(str(path.resolve(strict=False)))
        now = time.monotonic()
        entry = self._pending.get(key)
        signature = (int(info.st_size), int(info.st_mtime_ns))
        if entry is None or entry["signature"] != signature:
            self._pending[key] = {
                "path": path,
                "signature": signature,
                "stable_since": now,
            }
        elif entry["path"] != path:
            entry["path"] = path

    def _check_pending(self) -> None:
        if self._paused:
            return
        now = time.monotonic()
        for key in list(self._pending):
            entry = self._pending[key]
            path = entry["path"]
            try:
                info = os.lstat(path)
            except (OSError, ValueError):
                self._pending.pop(key, None)
                continue
            if not stat.S_ISREG(info.st_mode):
                self._pending.pop(key, None)
                continue
            signature = (int(info.st_size), int(info.st_mtime_ns))
            if signature != entry["signature"]:
                entry["signature"] = signature
                entry["stable_since"] = now
                continue
            if now - entry["stable_since"] < self.settle_seconds:
                continue
            self._pending.pop(key, None)
            if self._processed.get(key) == signature:
                continue
            self._processed[key] = signature
            try:
                self.callback(path)
            except Exception:
                LOGGER.exception("Error al procesar un archivo detectado: %s", path)
                self._emit("error", f"No se pudo procesar el archivo nuevo: {path.name}")

        if len(self._processed) > 5000:
            # Mantiene acotada la memoria en sesiones muy largas.
            self._processed = dict(list(self._processed.items())[-2000:])

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._events.get(timeout=self.poll_interval)
                if item != "__wake__":
                    self._add_pending(item)
            except queue.Empty:
                pass
            self._check_pending()

        self._stop_observer()
        self._emit("info", "Hilo de vigilancia finalizado.")


if WATCHDOG_AVAILABLE:

    class _EventHandler(FileSystemEventHandler):  # type: ignore[misc]
        def __init__(self, watcher: FileWatcher) -> None:
            super().__init__()
            self.watcher = watcher

        def _handle(self, event: FileSystemEvent) -> None:
            if getattr(event, "is_directory", False):
                return
            path = getattr(event, "dest_path", None) or event.src_path
            self.watcher._enqueue(str(path))

        def on_created(self, event: FileSystemEvent) -> None:
            self._handle(event)

        def on_moved(self, event: FileSystemEvent) -> None:
            self._handle(event)

        def on_modified(self, event: FileSystemEvent) -> None:
            # Modificaciones permiten detectar copias que no generan on_created
            # en algunos sistemas de archivos. El estabilizador evita duplicados.
            self._handle(event)

else:

    class _EventHandler:  # pragma: no cover
        def __init__(self, watcher: FileWatcher) -> None:
            self.watcher = watcher
