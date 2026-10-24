"""Historial persistente y restauración segura de movimientos."""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .locking import FileLock
from .paths import get_history_path, get_lock_path, is_path_within
from .safe_mover import (
    STATUS_MOVED,
    MoveResult,
    move_file_safely,
)

LOGGER = logging.getLogger(__name__)
HISTORY_VERSION = 1
UNDO_POLICIES = {"skip", "rename", "ask"}

CollisionCallback = Callable[["UndoAction"], str]
ProgressCallback = Callable[[int, int], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class UndoAction:
    entry_id: str
    original: Path
    destination: Path
    status: str
    message: str = ""
    size: int = 0

    @property
    def blocking(self) -> bool:
        return self.status == "conflict"


@dataclass
class UndoPlan:
    operation_id: str | None
    timestamp: str = ""
    roots: list[Path] = field(default_factory=list)
    actions: list[UndoAction] = field(default_factory=list)
    message: str = ""

    @property
    def total(self) -> int:
        return len(self.actions)

    @property
    def ready(self) -> int:
        return sum(1 for action in self.actions if action.status == "ready")

    @property
    def conflicts(self) -> int:
        return sum(1 for action in self.actions if action.status == "conflict")

    @property
    def missing(self) -> int:
        return sum(
            1
            for action in self.actions
            if action.status in {"missing_source", "missing_parent", "invalid"}
        )


@dataclass
class UndoReport:
    operation_id: str | None
    restored: list[MoveResult] = field(default_factory=list)
    skipped: list[MoveResult] = field(default_factory=list)
    errors: list[MoveResult] = field(default_factory=list)
    cancelled: bool = False
    message: str = ""

    @property
    def restored_count(self) -> int:
        return len(self.restored)

    def summary(self) -> str:
        return (
            f"Restaurados: {len(self.restored)} · Omitidos: {len(self.skipped)} · "
            f"Errores: {len(self.errors)}"
        )


class HistoryManager:
    """Gestiona ``history.json`` con escrituras atómicas.

    La restauración adquiere el mismo bloqueo que la organización para evitar
    que dos procesos muevan archivos a la vez.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        max_operations: int = 500,
        lock_path: Path | str | None = None,
        lock: FileLock | None = None,
    ) -> None:
        self.path = Path(path) if path else get_history_path()
        self.max_operations = max(1, int(max_operations))
        self.lock_path = Path(lock_path) if lock_path else get_lock_path()
        self._provided_lock = lock
        self._thread_lock = threading.RLock()
        self.recovered = False
        self.last_error: str | None = None

    # ------------------------------------------------------------------ lectura
    def _empty(self) -> dict[str, Any]:
        return {"version": HISTORY_VERSION, "operations": []}

    def _backup_corrupt(self) -> Path | None:
        if not self.path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            import shutil

            shutil.copy2(self.path, backup)
            return backup
        except OSError:
            LOGGER.exception("No se pudo respaldar el historial dañado")
            return None

    def _read(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return self._empty()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            backup = self._backup_corrupt()
            self.recovered = True
            self.last_error = f"Historial dañado: {exc}"
            LOGGER.warning("%s Se creará un historial nuevo.", self.last_error)
            if backup:
                LOGGER.warning("Respaldo del historial: %s", backup)
            return self._empty()
        except OSError as exc:
            self.recovered = True
            self.last_error = f"No se pudo leer el historial: {exc}"
            LOGGER.error(self.last_error)
            return self._empty()

        if not isinstance(raw, dict):
            backup = self._backup_corrupt()
            self.recovered = True
            self.last_error = "El historial no tiene el formato esperado."
            LOGGER.warning("%s Respaldo: %s", self.last_error, backup)
            return self._empty()

        operations = raw.get("operations")
        if not isinstance(operations, list):
            backup = self._backup_corrupt()
            self.recovered = True
            self.last_error = "La lista de operaciones del historial no es válida."
            LOGGER.warning("%s Respaldo: %s", self.last_error, backup)
            return self._empty()

        cleaned: list[dict[str, Any]] = []
        for operation in operations:
            normalized = self._normalize_operation(operation)
            if normalized is not None:
                cleaned.append(normalized)
        return {
            "version": HISTORY_VERSION,
            "operations": cleaned[-self.max_operations :],
        }

    def _normalize_operation(self, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, Mapping):
            return None
        operation_id = raw.get("id")
        if not isinstance(operation_id, str) or not operation_id:
            operation_id = uuid.uuid4().hex
        entries_raw = raw.get("entries")
        if not isinstance(entries_raw, list):
            entries_raw = []
        entries: list[dict[str, Any]] = []
        for entry in entries_raw:
            if not isinstance(entry, Mapping):
                continue
            source = entry.get("source")
            destination = entry.get("destination")
            if not source and not destination:
                continue
            entries.append(
                {
                    "id": str(entry.get("id") or uuid.uuid4().hex),
                    "source": str(source or ""),
                    "destination": str(destination or ""),
                    "size": self._safe_int(entry.get("size", 0)),
                    "status": str(entry.get("status") or "error"),
                    "error": str(entry.get("error")) if entry.get("error") else None,
                    "category": str(entry.get("category")) if entry.get("category") else None,
                    "restored_at": entry.get("restored_at"),
                    "restore_status": entry.get("restore_status"),
                    "restore_destination": entry.get("restore_destination"),
                    "restored_operation_id": entry.get("restored_operation_id"),
                }
            )
        roots_raw = raw.get("roots", [])
        roots = [str(root) for root in roots_raw] if isinstance(roots_raw, list) else []
        return {
            "id": operation_id,
            "timestamp": str(raw.get("timestamp") or utc_now()),
            "finished_at": raw.get("finished_at"),
            "kind": str(raw.get("kind") or "organize"),
            "roots": roots,
            "entries": entries,
            "undone_at": raw.get("undone_at"),
            "last_undo_at": raw.get("last_undo_at"),
            "cancelled": bool(raw.get("cancelled", False)),
        }

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data["version"] = HISTORY_VERSION
        data["operations"] = data.get("operations", [])[-self.max_operations :]
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(self.path.parent),
                prefix=".history-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise OSError(f"No se pudo guardar el historial: {exc}") from exc

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------ API
    def load(self) -> dict[str, Any]:
        with self._thread_lock:
            return copy.deepcopy(self._read())

    def get_operations(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._thread_lock:
            data = self._read()
            operations = data["operations"]
            if limit is not None:
                operations = operations[-max(0, int(limit)) :]
            return copy.deepcopy(list(reversed(operations)))

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._thread_lock:
            for operation in reversed(self._read()["operations"]):
                if operation.get("id") == operation_id:
                    return copy.deepcopy(operation)
        return None

    def record_operation(
        self,
        *,
        operation_id: str | None = None,
        kind: str = "organize",
        roots: Iterable[Path | str] = (),
        entries: Iterable[Mapping[str, Any]] = (),
        started_at: str | None = None,
        finished_at: str | None = None,
        cancelled: bool = False,
    ) -> str:
        """Añade una operación. Solo se guardan rutas, tamaños y estados."""

        entry_list: list[dict[str, Any]] = []
        for entry in entries:
            source = str(entry.get("source", ""))
            destination = entry.get("destination")
            if not source and not destination:
                continue
            entry_list.append(
                {
                    "id": str(entry.get("id") or uuid.uuid4().hex),
                    "source": source,
                    "destination": str(destination) if destination else "",
                    "size": self._safe_int(entry.get("size", 0)),
                    "status": str(entry.get("status", "error")),
                    "error": str(entry["error"]) if entry.get("error") else None,
                    "category": str(entry["category"]) if entry.get("category") else None,
                    "restored_at": None,
                    "restore_status": None,
                    "restore_destination": None,
                    "restored_operation_id": None,
                }
            )
        if not entry_list:
            return operation_id or ""

        operation = {
            "id": operation_id or uuid.uuid4().hex,
            "timestamp": started_at or utc_now(),
            "finished_at": finished_at or utc_now(),
            "kind": kind,
            "roots": [str(Path(root)) for root in roots],
            "entries": entry_list,
            "undone_at": None,
            "last_undo_at": None,
            "cancelled": bool(cancelled),
        }
        with self._thread_lock:
            data = self._read()
            data["operations"].append(operation)
            data["operations"] = data["operations"][-self.max_operations :]
            self._write(data)
        LOGGER.info(
            "Operación %s registrada (%s, %d archivos)",
            operation["id"],
            kind,
            len(entry_list),
        )
        return str(operation["id"])

    def get_statistics(self) -> dict[str, Any]:
        with self._thread_lock:
            operations = self._read()["operations"]
        organize_ops = [op for op in operations if op.get("kind") == "organize"]
        restore_ops = [op for op in operations if op.get("kind") == "restore"]
        moved = 0
        errors = 0
        restored = 0
        for operation in operations:
            for entry in operation.get("entries", []):
                if operation.get("kind") == "organize" and entry.get("status") == STATUS_MOVED:
                    moved += 1
                if entry.get("status") == "error":
                    errors += 1
                if entry.get("restored_at"):
                    restored += 1
        last = operations[-1]["timestamp"] if operations else None
        return {
            "operations": len(organize_ops),
            "restore_operations": len(restore_ops),
            "files_moved": moved,
            "errors": errors,
            "restored": restored,
            "last_operation": last,
            "history_path": str(self.path),
        }

    # ------------------------------------------------------------- restauración
    def _operation_success_entries(self, operation: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            entry
            for entry in operation.get("entries", [])
            if entry.get("status") == STATUS_MOVED and not entry.get("restored_at")
        ]

    def get_last_undoable_operation(self) -> dict[str, Any] | None:
        with self._thread_lock:
            for operation in reversed(self._read()["operations"]):
                if operation.get("kind") != "organize":
                    continue
                if operation.get("undone_at"):
                    continue
                if self._operation_success_entries(operation):
                    return copy.deepcopy(operation)
        return None

    def plan_undo(self, operation_id: str | None = None) -> UndoPlan:
        with self._thread_lock:
            data = self._read()
            operation = None
            if operation_id:
                operation = next(
                    (op for op in data["operations"] if op.get("id") == operation_id),
                    None,
                )
            else:
                for candidate in reversed(data["operations"]):
                    if candidate.get("kind") != "organize" or candidate.get("undone_at"):
                        continue
                    if self._operation_success_entries(candidate):
                        operation = candidate
                        break

            if operation is None:
                return UndoPlan(None, message="No hay ningún movimiento reciente que se pueda restaurar.")

            roots = [Path(root) for root in operation.get("roots", []) if root]
            actions: list[UndoAction] = []
            for entry in self._operation_success_entries(operation):
                original = Path(entry.get("source", ""))
                destination = Path(entry.get("destination", ""))
                size = self._safe_int(entry.get("size", 0))
                action = UndoAction(
                    entry_id=str(entry.get("id", "")),
                    original=original,
                    destination=destination,
                    status="ready",
                    size=size,
                )

                if not str(entry.get("source")) or not str(entry.get("destination")):
                    action.status = "invalid"
                    action.message = "El registro no contiene ambas rutas."
                elif roots and not any(is_path_within(original, root) for root in roots):
                    action.status = "invalid"
                    action.message = "La ubicación original ya no pertenece a las raíces registradas."
                elif roots and not any(is_path_within(destination, root) for root in roots):
                    action.status = "invalid"
                    action.message = "El destino ya no pertenece a las raíces registradas."
                elif not os.path.lexists(destination):
                    action.status = "missing_source"
                    action.message = "El archivo ya no existe en su ubicación de destino."
                elif destination.is_symlink():
                    action.status = "invalid"
                    action.message = "El archivo de destino es un enlace simbólico."
                elif not destination.is_file():
                    action.status = "invalid"
                    action.message = "El destino ya no es un archivo regular."
                elif not original.parent.exists() or not original.parent.is_dir():
                    action.status = "missing_parent"
                    action.message = "La carpeta original ya no existe."
                elif os.path.lexists(original):
                    action.status = "conflict"
                    action.message = "Ya existe un archivo en la ubicación original."
                actions.append(action)

            return UndoPlan(
                operation_id=operation.get("id"),
                timestamp=str(operation.get("timestamp", "")),
                roots=roots,
                actions=actions,
                message="",
            )

    def _resolve_collisions(
        self,
        plan: UndoPlan,
        collision_policy: str,
        on_collision: CollisionCallback | None,
    ) -> tuple[dict[str, str], bool]:
        if collision_policy not in UNDO_POLICIES:
            raise ValueError("collision_policy debe ser 'skip', 'rename' o 'ask'")
        decisions: dict[str, str] = {}
        for action in plan.actions:
            if action.status != "conflict":
                decisions[action.entry_id] = "rename" if collision_policy == "rename" else "skip"
                continue
            if collision_policy == "ask":
                if on_collision is None:
                    decisions[action.entry_id] = "skip"
                else:
                    answer = str(on_collision(action)).strip().lower()
                    if answer not in {"skip", "rename", "cancel"}:
                        answer = "skip"
                    if answer == "cancel":
                        return decisions, True
                    decisions[action.entry_id] = answer
            else:
                decisions[action.entry_id] = collision_policy
        return decisions, False

    def undo_last_operation(
        self,
        collision_policy: str = "skip",
        *,
        operation_id: str | None = None,
        on_collision: CollisionCallback | None = None,
        progress: ProgressCallback | None = None,
    ) -> UndoReport:
        """Restaura la última operación de organización.

        No sobrescribe archivos. Si el nombre original está ocupado se puede
        omitir el archivo o restaurarlo con un nombre alternativo. La operación
        solo se marca como deshecha cuando todos los archivos se han restaurado.
        """

        if collision_policy not in UNDO_POLICIES:
            raise ValueError("collision_policy debe ser 'skip', 'rename' o 'ask'")

        lock = self._provided_lock or FileLock(self.lock_path)
        with lock:
            with self._thread_lock:
                plan = self.plan_undo(operation_id)
                if plan.operation_id is None:
                    return UndoReport(None, message=plan.message)

                decisions, cancelled = self._resolve_collisions(
                    plan, collision_policy, on_collision
                )
                if cancelled:
                    return UndoReport(
                        plan.operation_id,
                        cancelled=True,
                        message="Restauración cancelada por el usuario.",
                    )

                data = self._read()
                operation = next(
                    (
                        op
                        for op in data["operations"]
                        if op.get("id") == plan.operation_id
                    ),
                    None,
                )
                if operation is None:
                    return UndoReport(
                        plan.operation_id,
                        message="La operación ya no está en el historial.",
                    )

                roots = [Path(root) for root in operation.get("roots", []) if root]
                report = UndoReport(plan.operation_id)
                restore_entries: list[dict[str, Any]] = []
                total = len(plan.actions)
                done = 0

                for action in plan.actions:
                    decision = decisions.get(action.entry_id, "skip")
                    if action.status == "conflict" and decision == "skip":
                        result = MoveResult(
                            action.destination,
                            action.original,
                            "skipped",
                            "El nombre original está ocupado; se omitió sin sobrescribir.",
                            action.size,
                        )
                        report.skipped.append(result)
                        done += 1
                        if progress:
                            progress(done, total)
                        continue
                    if action.status != "ready" and not (
                        action.status == "conflict" and decision == "rename"
                    ):
                        result = MoveResult(
                            action.destination,
                            action.original,
                            "skipped",
                            action.message,
                            action.size,
                        )
                        report.skipped.append(result)
                        done += 1
                        if progress:
                            progress(done, total)
                        continue

                    result = move_file_safely(
                        action.destination,
                        action.original.parent,
                        target_name=action.original.name,
                        dry_run=False,
                        collision_policy="rename" if decision == "rename" else "skip",
                        allowed_roots=roots or None,
                    )
                    result.category = "Restauración"
                    if result.status == STATUS_MOVED:
                        report.restored.append(result)
                        restored_path = str(result.destination) if result.destination else ""
                        for entry in operation.get("entries", []):
                            if entry.get("id") == action.entry_id:
                                entry["restored_at"] = utc_now()
                                entry["restore_status"] = "restored"
                                entry["restore_destination"] = restored_path
                                entry["restored_operation_id"] = plan.operation_id
                                break
                        restore_entries.append(
                            {
                                "id": uuid.uuid4().hex,
                                "source": str(action.destination),
                                "destination": restored_path,
                                "size": result.size,
                                "status": STATUS_MOVED,
                                "error": None,
                                "category": "Restauración",
                            }
                        )
                    elif result.status == "error":
                        report.errors.append(result)
                        restore_entries.append(
                            {
                                "id": uuid.uuid4().hex,
                                "source": str(action.destination),
                                "destination": str(action.original),
                                "size": result.size,
                                "status": "error",
                                "error": result.message,
                                "category": "Restauración",
                            }
                        )
                    else:
                        report.skipped.append(result)
                    done += 1
                    if progress:
                        progress(done, total)

                remaining = self._operation_success_entries(operation)
                operation["last_undo_at"] = utc_now()
                if not remaining:
                    operation["undone_at"] = utc_now()

                if restore_entries:
                    data["operations"].append(
                        {
                            "id": uuid.uuid4().hex,
                            "timestamp": utc_now(),
                            "finished_at": utc_now(),
                            "kind": "restore",
                            "roots": [str(root) for root in roots],
                            "entries": self._normalize_operation(
                                {
                                    "id": uuid.uuid4().hex,
                                    "timestamp": utc_now(),
                                    "kind": "restore",
                                    "roots": [str(root) for root in roots],
                                    "entries": restore_entries,
                                }
                            )["entries"],
                            "undone_at": None,
                            "last_undo_at": None,
                            "cancelled": False,
                        }
                    )
                    data["operations"] = data["operations"][-self.max_operations :]

                self._write(data)
                report.message = report.summary()
                LOGGER.info(
                    "Restauración de %s: %s", plan.operation_id, report.message
                )
                return report

# Nombre explícito exigido por la especificación funcional.
def undo_last_operation(
    history: HistoryManager,
    collision_policy: str = "skip",
    **kwargs: Any,
) -> UndoReport:
    """Función de conveniencia sobre ``HistoryManager.undo_last_operation``."""

    return history.undo_last_operation(collision_policy, **kwargs)
