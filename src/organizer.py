"""Orquestación: análisis, planificación y organización segura por lotes."""

from __future__ import annotations

import logging
import os
import stat
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config_manager import ConfigManager, is_valid_category_name
from .file_classifier import FileClassifier
from .history_manager import HistoryManager
from .locking import FileLock
from .paths import is_path_within, paths_equal
from .safe_mover import (
    STATUS_DRY_RUN,
    STATUS_MOVED,
    STATUS_SKIPPED,
    MoveResult,
    find_available_destination,
    move_file_safely,
)

LOGGER = logging.getLogger(__name__)

PLAN_READY = "ready"
PLAN_SKIPPED = "skipped"
PLAN_ERROR = "error"

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4

ProgressCallback = Callable[[int, int, str], None]

STATUS_LABELS = {
    PLAN_READY: "Listo",
    PLAN_SKIPPED: "Omitido",
    PLAN_ERROR: "Error",
    STATUS_DRY_RUN: "Simulación",
    STATUS_MOVED: "Movido",
    "error": "Error",
    "skipped": "Omitido",
}


@dataclass
class FilePlan:
    source: Path
    root: Path
    category: str
    extension: str
    size: int
    destination_dir: Path | None
    destination: Path | None
    status: str
    message: str = ""

    @property
    def name(self) -> str:
        return self.source.name

    @property
    def ready(self) -> bool:
        return self.status == PLAN_READY

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": str(self.source),
            "root": str(self.root),
            "extension": self.extension,
            "category": self.category,
            "size": self.size,
            "destination": str(self.destination) if self.destination else "",
            "status": self.status,
            "message": self.message,
        }


@dataclass
class ScanReport:
    source: Path
    plans: list[FilePlan] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ignored_directories: int = 0
    cancelled: bool = False
    duration_seconds: float = 0.0
    root: Path | None = None

    @property
    def total(self) -> int:
        return len(self.plans)

    @property
    def to_move(self) -> int:
        return sum(1 for plan in self.plans if plan.status == PLAN_READY)

    @property
    def skipped(self) -> int:
        return sum(1 for plan in self.plans if plan.status == PLAN_SKIPPED)

    @property
    def file_errors(self) -> int:
        return sum(1 for plan in self.plans if plan.status == PLAN_ERROR)

    @property
    def error_count(self) -> int:
        return len(self.errors) + self.file_errors

    @property
    def ready_plans(self) -> list[FilePlan]:
        return [plan for plan in self.plans if plan.status == PLAN_READY]

    @property
    def categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for plan in self.plans:
            counts[plan.category] = counts.get(plan.category, 0) + 1
        return counts

    def all_errors(self) -> list[str]:
        messages = list(self.errors)
        for plan in self.plans:
            if plan.status == PLAN_ERROR:
                messages.append(f"{plan.source}: {plan.message}")
        return messages


@dataclass
class OperationResult:
    operation_id: str | None
    scans: list[ScanReport] = field(default_factory=list)
    plans: list[FilePlan] = field(default_factory=list)
    results: list[MoveResult] = field(default_factory=list)
    dry_run: bool = False
    cancelled: bool = False
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    history_recorded: bool = False

    @property
    def moved(self) -> int:
        return sum(1 for item in self.results if item.status == STATUS_MOVED)

    @property
    def skipped(self) -> int:
        skipped_plans = sum(1 for plan in self.plans if plan.status == PLAN_SKIPPED)
        skipped_results = sum(1 for item in self.results if item.status == STATUS_SKIPPED)
        return skipped_plans + skipped_results

    @property
    def errors(self) -> int:
        plan_errors = sum(1 for plan in self.plans if plan.status == PLAN_ERROR)
        result_errors = sum(1 for item in self.results if item.status == "error")
        scan_errors = sum(len(scan.errors) for scan in self.scans)
        return plan_errors + result_errors + scan_errors

    @property
    def total(self) -> int:
        return len(self.plans)

    @property
    def planned(self) -> int:
        return sum(1 for plan in self.plans if plan.status == PLAN_READY)

    @property
    def all_errors(self) -> list[str]:
        messages: list[str] = []
        for scan in self.scans:
            messages.extend(scan.all_errors())
        for result in self.results:
            if result.status == "error":
                messages.append(f"{result.source}: {result.message}")
        return messages

    def display_rows(self) -> list[dict[str, Any]]:
        """Combina planes y resultados para mostrar una tabla coherente."""

        results_by_source = {str(result.source): result for result in self.results}
        rows: list[dict[str, Any]] = []
        for plan in self.plans:
            result = results_by_source.get(str(plan.source))
            destination = (
                str(result.destination)
                if result is not None and result.destination
                else str(plan.destination) if plan.destination else ""
            )
            status = result.status if result is not None else plan.status
            message = result.message if result is not None else plan.message
            size = result.size if result is not None and result.size else plan.size
            rows.append(
                {
                    "name": plan.name,
                    "extension": plan.extension,
                    "category": plan.category,
                    "source": str(plan.source),
                    "destination": destination,
                    "size": size,
                    "status": status,
                    "status_label": STATUS_LABELS.get(status, status),
                    "message": message,
                }
            )
        return rows

    def summary_text(self, dry_run: bool = False) -> str:
        prefix = "Simulación" if dry_run else "Organización"
        state = "detenida" if self.cancelled else "completada"
        if dry_run:
            return (
                f"{prefix}: {self.planned} archivos se moverían · "
                f"{self.skipped} omitidos · {self.errors} errores."
            )
        return (
            f"{prefix} {state}: {self.moved} movidos · {self.skipped} omitidos · "
            f"{self.errors} errores."
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Organizer:
    """Coordina clasificación, movimiento, historial y bloqueo."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        history: HistoryManager | None = None,
        lock_path: Path | str | None = None,
        lock: FileLock | None = None,
    ) -> None:
        if not isinstance(config, Mapping):
            raise ValueError("La configuración no es válida")
        self.config = dict(config)
        categories = config.get("categories", {})
        default_category = str(config.get("default_category", "Otros"))
        self.classifier = FileClassifier(categories, default_category)
        self.safety = dict(config.get("safety", {}))
        self.history_settings = dict(config.get("history", {}))
        self.lock_path = Path(lock_path) if lock_path else None
        self._provided_lock = lock
        self.history_enabled = bool(self.history_settings.get("enabled", True))
        self.history = history
        if self.history is None and self.history_enabled:
            self.history = HistoryManager(
                max_operations=int(self.history_settings.get("max_operations", 500)),
                lock_path=lock_path,
            )

    # ------------------------------------------------------------------ análisis
    @staticmethod
    def _file_attributes(stat_result: os.stat_result) -> int:
        try:
            return int(getattr(stat_result, "st_file_attributes", 0))
        except (TypeError, ValueError):
            return 0

    def _skip_reason(
        self, stat_result: os.stat_result, name: str
    ) -> str | None:
        attributes = self._file_attributes(stat_result)
        if self.safety.get("skip_hidden", True):
            if name.startswith(".") or attributes & FILE_ATTRIBUTE_HIDDEN:
                return "Archivo oculto omitido por seguridad."
        if self.safety.get("skip_system", True) and attributes & FILE_ATTRIBUTE_SYSTEM:
            return "Archivo de sistema omitido por seguridad."
        return None

    def _build_plan(
        self,
        *,
        source: Path,
        root: Path,
        stat_result: os.stat_result,
        reserved: set[str],
    ) -> FilePlan:
        category = self.classifier.classify(source)
        extension = self.classifier.get_extension(source)
        size = int(stat_result.st_size)

        skip_reason = self._skip_reason(stat_result, source.name)
        if skip_reason:
            return FilePlan(
                source=source,
                root=root,
                category=category,
                extension=extension,
                size=size,
                destination_dir=None,
                destination=None,
                status=PLAN_SKIPPED,
                message=skip_reason,
            )

        if not is_valid_category_name(category):
            return FilePlan(
                source=source,
                root=root,
                category=category,
                extension=extension,
                size=size,
                destination_dir=None,
                destination=None,
                status=PLAN_ERROR,
                message="La categoría no es un nombre de carpeta seguro.",
            )

        destination_dir = root / category
        if paths_equal(destination_dir, root) or not is_path_within(destination_dir, root):
            return FilePlan(
                source=source,
                root=root,
                category=category,
                extension=extension,
                size=size,
                destination_dir=None,
                destination=None,
                status=PLAN_ERROR,
                message="La carpeta de destino quedaría fuera de la carpeta de origen.",
            )
        if destination_dir.is_symlink():
            return FilePlan(
                source=source,
                root=root,
                category=category,
                extension=extension,
                size=size,
                destination_dir=destination_dir,
                destination=None,
                status=PLAN_ERROR,
                message="La carpeta de destino es un enlace simbólico.",
            )
        if destination_dir.exists() and not destination_dir.is_dir():
            return FilePlan(
                source=source,
                root=root,
                category=category,
                extension=extension,
                size=size,
                destination_dir=destination_dir,
                destination=None,
                status=PLAN_ERROR,
                message="La ruta de destino existe y no es una carpeta.",
            )

        try:
            destination = find_available_destination(
                destination_dir, source.name, reserved=reserved
            )
        except (OSError, ValueError) as exc:
            return FilePlan(
                source=source,
                root=root,
                category=category,
                extension=extension,
                size=size,
                destination_dir=destination_dir,
                destination=None,
                status=PLAN_ERROR,
                message=str(exc),
            )
        reserved.add(os.path.normcase(str(destination)))
        LOGGER.debug(
            "Plan de análisis | origen=%s | categoría=%s | destino=%s | tamaño=%s",
            source,
            category,
            destination,
            size,
        )
        return FilePlan(
            source=source,
            root=root,
            category=category,
            extension=extension,
            size=size,
            destination_dir=destination_dir,
            destination=destination,
            status=PLAN_READY,
        )

    def scan(
        self,
        source: Path | str,
        *,
        stop_event: Any | None = None,
    ) -> ScanReport:
        """Analiza únicamente el primer nivel de una carpeta, sin modificarla."""

        started = time.monotonic()
        raw_source = Path(source).expanduser()
        report = ScanReport(source=raw_source)
        LOGGER.info("Iniciando análisis (solo lectura) | origen=%s", raw_source)
        try:
            if not raw_source.exists():
                message = f"La carpeta de origen no existe: {raw_source}"
                report.errors.append(message)
                LOGGER.error("Análisis cancelado | %s", message)
                return report
            if not raw_source.is_dir():
                message = f"La ruta de origen no es una carpeta: {raw_source}"
                report.errors.append(message)
                LOGGER.error("Análisis cancelado | %s", message)
                return report
            root = raw_source.resolve(strict=True)
            report.root = root
        except PermissionError as exc:
            message = f"Permiso denegado al acceder a la carpeta de origen: {raw_source} ({exc})"
            report.errors.append(message)
            LOGGER.error("Análisis cancelado | %s", message)
            return report
        except OSError as exc:
            message = f"No se pudo validar la carpeta de origen {raw_source}: {exc}"
            report.errors.append(message)
            LOGGER.error("Análisis cancelado | %s", message)
            return report

        reserved: set[str] = set()
        try:
            entries = sorted(root.iterdir(), key=lambda item: item.name.casefold())
        except PermissionError:
            message = f"Permiso denegado al leer la carpeta de origen: {root}"
            report.errors.append(message)
            LOGGER.error("Análisis cancelado | %s", message)
            report.duration_seconds = time.monotonic() - started
            return report
        except OSError as exc:
            message = f"No se pudo leer la carpeta de origen {root}: {exc}"
            report.errors.append(message)
            LOGGER.error("Análisis cancelado | %s", message)
            report.duration_seconds = time.monotonic() - started
            return report

        for entry in entries:
            if stop_event is not None and stop_event.is_set():
                report.cancelled = True
                break
            try:
                entry_stat = os.lstat(entry)
            except PermissionError:
                report.errors.append(f"Permiso denegado al inspeccionar: {entry.name}")
                continue
            except FileNotFoundError:
                continue
            except OSError as exc:
                report.errors.append(f"No se pudo inspeccionar {entry.name}: {exc}")
                continue

            if stat.S_ISDIR(entry_stat.st_mode):
                report.ignored_directories += 1
                continue

            if not stat.S_ISREG(entry_stat.st_mode):
                report.plans.append(
                    FilePlan(
                        source=entry,
                        root=root,
                        category="—",
                        extension=self.classifier.get_extension(entry),
                        size=int(entry_stat.st_size),
                        destination_dir=None,
                        destination=None,
                        status=PLAN_SKIPPED,
                        message="No es un archivo regular; no se moverá.",
                    )
                )
                continue

            try:
                plan = self._build_plan(
                    source=entry,
                    root=root,
                    stat_result=entry_stat,
                    reserved=reserved,
                )
            except (OSError, ValueError) as exc:
                message = f"No se pudo planificar {entry.name}: {exc}"
                LOGGER.exception(
                    "Error al planificar | origen=%s | detalle=%s", entry, message
                )
                plan = FilePlan(
                    source=entry,
                    root=root,
                    category="—",
                    extension=self.classifier.get_extension(entry),
                    size=int(entry_stat.st_size),
                    destination_dir=None,
                    destination=None,
                    status=PLAN_ERROR,
                    message=message,
                )
            report.plans.append(plan)

        report.duration_seconds = time.monotonic() - started
        for plan in report.plans:
            if plan.status == PLAN_ERROR:
                LOGGER.error(
                    "Plan con error | origen=%s | destino=%s | detalle=%s",
                    plan.source,
                    plan.destination if plan.destination else plan.destination_dir,
                    plan.message,
                )
        LOGGER.info(
            "Análisis completado (sin modificar archivos) | origen=%s | detectados=%s | "
            "por_mover=%s | omitidos=%s | errores=%s | duración=%.3fs",
            root,
            report.total,
            report.to_move,
            report.skipped,
            report.error_count,
            report.duration_seconds,
        )
        return report

    def analyze(self, sources: Sequence[Path | str] | None = None) -> list[ScanReport]:
        reports: list[ScanReport] = []
        for source in sources or []:
            reports.append(self.scan(source))
        return reports

    def scan_sources(
        self, sources: Sequence[Path | str] | None = None
    ) -> list[ScanReport]:
        return self.analyze(sources)

    # ------------------------------------------------------------------ ejecución
    def _source_list(self, sources: Sequence[Path | str] | None) -> list[Path]:
        if sources is None:
            sources = [
                entry.get("path", "")
                for entry in self.config.get("sources", [])
                if entry.get("enabled", True)
            ]
        result: list[Path] = []
        seen: set[str] = set()
        for source in sources:
            if not source:
                continue
            path = Path(source).expanduser()
            try:
                resolved = path.resolve(strict=False)
                key = os.path.normcase(str(resolved))
            except OSError:
                resolved = path
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
        return result

    def _history_record(
        self,
        *,
        operation_id: str,
        roots: list[Path],
        attempts: list[MoveResult],
        started_at: str,
        cancelled: bool,
    ) -> bool:
        if not self.history_enabled or self.history is None or not attempts:
            return False
        entries = [
            {
                "source": str(result.source),
                "destination": str(result.destination) if result.destination else "",
                "size": result.size,
                "status": result.status,
                "error": result.message if result.status == "error" else None,
                "category": result.category,
            }
            for result in attempts
        ]
        try:
            self.history.record_operation(
                operation_id=operation_id,
                kind="organize",
                roots=roots,
                entries=entries,
                started_at=started_at,
                cancelled=cancelled,
            )
            return True
        except (OSError, ValueError):
            LOGGER.exception("No se pudo guardar el historial")
            return False

    def organize_sources(
        self,
        sources: Sequence[Path | str] | None = None,
        *,
        dry_run: bool = False,
        stop_event: Any | None = None,
        progress: ProgressCallback | None = None,
    ) -> OperationResult:
        """Organiza una o varias carpetas, o simula el resultado."""

        result = OperationResult(
            operation_id=uuid.uuid4().hex,
            dry_run=dry_run,
            started_at=_utc_now(),
        )
        started = time.monotonic()
        source_paths = self._source_list(sources)
        LOGGER.info(
            "Operación iniciada | modo=%s | orígenes=%s | id=%s",
            "simulación" if dry_run else "movimiento",
            [str(path) for path in source_paths],
            result.operation_id,
        )

        lock: FileLock | None = None
        if not dry_run:
            lock = self._provided_lock or (
                FileLock(self.lock_path) if self.lock_path else FileLock()
            )
            lock.acquire()

        try:
            roots: list[Path] = []
            for source in source_paths:
                if stop_event is not None and stop_event.is_set():
                    result.cancelled = True
                    break
                report = self.scan(source, stop_event=stop_event)
                result.scans.append(report)
                result.plans.extend(report.plans)
                if report.root:
                    roots.append(report.root)
                if report.cancelled:
                    result.cancelled = True

            ready_plans = [plan for plan in result.plans if plan.status == PLAN_READY]
            total = len(ready_plans)
            done = 0

            if dry_run:
                for plan in ready_plans:
                    if stop_event is not None and stop_event.is_set():
                        result.cancelled = True
                        break
                    simulated = MoveResult(
                        plan.source,
                        plan.destination,
                        STATUS_DRY_RUN,
                        "Se movería a la carpeta de destino.",
                        plan.size,
                        renamed=bool(
                            plan.destination and plan.destination.name != plan.source.name
                        ),
                    )
                    simulated.category = plan.category
                    result.results.append(simulated)
                    done += 1
                    if progress:
                        progress(done, total, plan.name)
            else:
                attempts: list[MoveResult] = []
                for plan in ready_plans:
                    if stop_event is not None and stop_event.is_set():
                        result.cancelled = True
                        break
                    if plan.destination_dir is None:
                        move_result = MoveResult(
                            plan.source,
                            None,
                            "error",
                            "El plan no tenía una carpeta de destino válida.",
                            plan.size,
                        )
                    else:
                        LOGGER.info(
                            "Moviendo archivo planificado | origen=%s | carpeta_destino=%s | "
                            "destino_previsto=%s | categoría=%s",
                            plan.source,
                            plan.destination_dir,
                            plan.destination,
                            plan.category,
                        )
                        move_result = move_file_safely(
                            plan.source,
                            plan.destination_dir,
                            collision_policy="rename",
                            allowed_roots=[plan.root],
                        )
                    move_result.category = plan.category
                    result.results.append(move_result)
                    attempts.append(move_result)
                    done += 1
                    if move_result.status == "error":
                        LOGGER.error(
                            "Movimiento fallido, se continúa con el resto | origen=%s | "
                            "destino=%s | detalle=%s",
                            move_result.source,
                            move_result.destination,
                            move_result.message,
                        )
                    if progress:
                        progress(done, total, plan.name)

                if attempts:
                    recorded = self._history_record(
                        operation_id=result.operation_id or "",
                        roots=roots,
                        attempts=attempts,
                        started_at=result.started_at,
                        cancelled=result.cancelled,
                    )
                    result.history_recorded = recorded
                    if not recorded and self.history_enabled:
                        result.warnings.append(
                            "No se pudo guardar el historial de esta operación."
                        )

            result.cancelled = result.cancelled or any(
                scan.cancelled for scan in result.scans
            )
            result.finished_at = _utc_now()
            result.duration_seconds = time.monotonic() - started
            LOGGER.info(
                "Operación finalizada | id=%s | movidos=%s | omitidos=%s | errores=%s | "
                "cancelada=%s | duración=%.3fs",
                result.operation_id,
                result.moved,
                result.skipped,
                result.errors,
                result.cancelled,
                result.duration_seconds,
            )
            return result
        finally:
            if lock is not None:
                lock.release()

    def organize_source(
        self,
        source: Path | str,
        *,
        dry_run: bool = False,
        stop_event: Any | None = None,
        progress: ProgressCallback | None = None,
    ) -> OperationResult:
        return self.organize_sources(
            [source], dry_run=dry_run, stop_event=stop_event, progress=progress
        )

    def sources_from_config(self) -> list[Path]:
        return self._source_list(None)

    def category_destination(self, source: Path | str, category: str) -> Path:
        return Path(source).expanduser().resolve(strict=False) / category


def make_organizer_from_manager(
    manager: ConfigManager | None = None,
    *,
    lock_path: Path | str | None = None,
) -> tuple[Organizer, dict[str, Any]]:
    """Crea un organizador con la configuración actual."""

    config_manager = manager or ConfigManager()
    config = config_manager.load()
    return Organizer(config, lock_path=lock_path), config
