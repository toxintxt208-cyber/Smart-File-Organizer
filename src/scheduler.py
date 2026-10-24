"""Programador periódico opcional, sin privilegios ni tareas del sistema."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .locking import LockBusyError

LOGGER = logging.getLogger(__name__)

JobCallable = Callable[[threading.Event], Any]
EventCallback = Callable[[str, str], None]


class OrganizationScheduler:
    """Ejecuta un trabajo cada N minutos mientras la aplicación está abierta.

    Solo se inicia cuando la persona usuaria ha habilitado expresamente la
    organización automática. El trabajo recibe un ``threading.Event`` para
    poder detenerse de forma cooperativa.
    """

    def __init__(
        self,
        interval_minutes: int,
        job: JobCallable,
        *,
        run_immediately: bool = False,
        on_event: EventCallback | None = None,
        enabled: bool = True,
    ) -> None:
        self.interval_minutes = max(1, int(interval_minutes))
        self.job = job
        self.run_immediately = bool(run_immediately)
        self.on_event = on_event
        self._enabled = bool(enabled)
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._trigger_requested = False
        self._last_run_started: float | None = None
        self._last_run_finished: float | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------ estado
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def enabled(self) -> bool:
        return self._enabled and not self._stop_event.is_set()

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _emit(self, level: str, message: str) -> None:
        LOGGER.log(
            logging.ERROR if level == "error" else logging.INFO, "Scheduler: %s", message
        )
        if self.on_event is not None:
            try:
                self.on_event(level, message)
            except Exception:
                LOGGER.exception("Error en el callback del programador")

    # ----------------------------------------------------------------- control
    def start(self) -> "OrganizationScheduler":
        with self._state_lock:
            if self.running:
                return self
            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="smart-file-organizer-scheduler",
                daemon=True,
            )
            self._thread.start()
        if self._enabled:
            self._emit(
                "info",
                f"Organización automática activa cada {self.interval_minutes} minuto(s).",
            )
        else:
            self._emit("info", "Organización automática pausada.")
        return self

    def stop(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if wait and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        self._emit("info", "Programador detenido.")

    def pause(self) -> None:
        self._enabled = False
        self._wake_event.set()
        self._emit("info", "Organización automática pausada.")

    def resume(self) -> None:
        self._enabled = True
        self._wake_event.set()
        self._emit(
            "info",
            f"Organización automática reanudada cada {self.interval_minutes} minuto(s).",
        )

    def set_interval(self, interval_minutes: int) -> None:
        self.interval_minutes = max(1, int(interval_minutes))
        self._wake_event.set()
        self._emit(
            "info",
            f"Intervalo automático actualizado a {self.interval_minutes} minuto(s).",
        )

    def trigger_now(self) -> None:
        self._trigger_requested = True
        self._wake_event.set()

    def status_text(self) -> str:
        if not self.running:
            return "Programador detenido"
        if not self._enabled:
            return "Organización automática pausada"
        return f"Automático cada {self.interval_minutes} min"

    # ------------------------------------------------------------------- bucle
    def _run(self) -> None:
        interval_seconds = max(60.0, self.interval_minutes * 60.0)
        next_run = time.monotonic() + (0.0 if self.run_immediately else interval_seconds)
        self.run_immediately = False

        while not self._stop_event.is_set():
            now = time.monotonic()
            timeout = max(0.1, next_run - now)
            notified = self._wake_event.wait(timeout=timeout)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break

            interval_seconds = max(60.0, self.interval_minutes * 60.0)
            if not self._enabled:
                self._trigger_requested = False
                next_run = time.monotonic() + interval_seconds
                continue

            if notified and not self._trigger_requested:
                # Cambios de intervalo, pausa o reanudación: reprogramar sin
                # ejecutar inmediatamente.
                next_run = time.monotonic() + interval_seconds
                continue

            self._trigger_requested = False
            try:
                self._last_run_started = time.time()
                self._emit("info", "Ejecutando organización automática.")
                self.job(self._stop_event)
                self._last_error = None
                self._emit("info", "Organización automática completada.")
            except LockBusyError:
                self._emit(
                    "info",
                    "Otra organización está en curso; se omite esta ejecución.",
                )
            except Exception as exc:
                self._last_error = str(exc)
                LOGGER.exception("Error en la organización automática")
                self._emit("error", f"Error automático: {exc}")
            finally:
                self._last_run_finished = time.time()
                interval_seconds = max(60.0, self.interval_minutes * 60.0)
                next_run = time.monotonic() + interval_seconds

        self._emit("info", "Programador finalizado.")
