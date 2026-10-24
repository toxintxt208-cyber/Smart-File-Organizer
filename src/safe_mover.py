"""Movimiento seguro de archivos, sin sobrescritura ni rutas fuera del origen.

Todas las operaciones se hacen por fases y cada fallo se registra con la ruta
de origen, la ruta de destino y la operación concreta que falló. Así se evita
convertir errores distintos (carpeta de destino no creada, archivo de origen
desaparecido, permisos, bloqueo, etc.) en un único mensaje genérico.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .paths import is_path_within, paths_equal

LOGGER = logging.getLogger(__name__)

STATUS_MOVED = "moved"
STATUS_DRY_RUN = "dry_run"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

_ALLOWED_POLICIES = {"rename", "skip"}


@dataclass
class MoveResult:
    source: Path
    destination: Path | None
    status: str
    message: str = ""
    size: int = 0
    renamed: bool = False
    category: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {STATUS_MOVED, STATUS_DRY_RUN}

    def as_dict(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "destination": str(self.destination) if self.destination else None,
            "status": self.status,
            "message": self.message,
            "size": self.size,
            "renamed": self.renamed,
            "category": self.category,
        }


def _normalized(path: Path) -> str:
    return os.path.normcase(str(path))


def _is_reserved(candidate: Path, reserved: set[str] | None) -> bool:
    return bool(reserved and _normalized(candidate) in reserved)


def _candidate_name(target_name: str, index: int) -> str:
    if index == 0:
        return target_name
    original = Path(target_name)
    suffix = "".join(original.suffixes)
    if suffix:
        stem = original.name[: -len(suffix)]
        return f"{stem}_{index}{suffix}"
    return f"{original.name}_{index}"


def find_available_destination(
    destination_dir: Path | str,
    target_name: str,
    *,
    reserved: set[str] | None = None,
) -> Path:
    """Busca un nombre libre sin crear ni modificar nada.

    ``reserved`` permite simular un lote completo: dos archivos con el mismo
    nombre no deben recibir el mismo destino en una previsualización.
    """

    directory = Path(destination_dir)
    if not target_name or Path(target_name).name != target_name:
        raise ValueError("El nombre de destino no puede contener separadores de ruta")

    index = 0
    while True:
        candidate = directory / _candidate_name(target_name, index)
        if not os.path.lexists(candidate) and not _is_reserved(candidate, reserved):
            return candidate
        index += 1
        if index > 100_000:
            raise OSError("No se encontró un nombre libre para el archivo")


def _validate_target_name(target_name: str) -> None:
    if not isinstance(target_name, str) or not target_name.strip():
        raise ValueError("El nombre de destino está vacío")
    if target_name in {".", ".."} or Path(target_name).name != target_name:
        raise ValueError("El nombre de destino no puede contener rutas")
    if any(character in target_name for character in '<>:"/\\|?*'):
        raise ValueError("El nombre de destino contiene caracteres no válidos")
    if len(target_name) > 255:
        raise ValueError("El nombre de destino es demasiado largo")
    if target_name.endswith((" ", ".")):
        raise ValueError("El nombre de destino no puede terminar en espacio o punto")


def _within_any(path: Path, roots: Iterable[Path] | None) -> bool:
    if roots is None:
        return True
    checked = False
    for root in roots:
        checked = True
        if is_path_within(path, Path(root)):
            return True
    return not checked


def _paths_text(source: Path | None, destination: Path | None) -> str:
    parts: list[str] = []
    if source is not None:
        parts.append(f"origen={source}")
    if destination is not None:
        parts.append(f"destino={destination}")
    return ", ".join(parts)


def _friendly_error(
    exc: BaseException,
    *,
    operation: str,
    source: Path | None = None,
    destination: Path | None = None,
) -> str:
    """Traduce una excepción conservando la operación y las rutas implicadas."""

    location = _paths_text(source, destination)
    suffix = f" ({location})" if location else ""
    if isinstance(exc, PermissionError):
        return (
            f"No se pudo completar «{operation}»: Permiso denegado. "
            "El archivo puede estar abierto por otro programa, la carpeta puede "
            "estar protegida por antivirus/Control de carpetas o faltan permisos "
            f"de escritura.{suffix}"
        )
    if isinstance(exc, FileNotFoundError):
        return (
            f"No se pudo completar «{operation}»: el sistema no encontró una ruta "
            f"necesaria. Puede que el archivo o la carpeta hayan desaparecido, que "
            f"la unidad esté desconectada o que una política de seguridad bloquee "
            f"la escritura.{suffix}"
        )
    if isinstance(exc, OSError):
        winerror = getattr(exc, "winerror", None)
        if winerror in {32, 33} or exc.errno in {errno.EACCES, errno.EBUSY}:
            return (
                f"No se pudo completar «{operation}»: el archivo está bloqueado por "
                f"otro proceso.{suffix}"
            )
        if winerror == 5 or exc.errno == errno.EACCES:
            return (
                f"No se pudo completar «{operation}»: acceso denegado.{suffix}"
            )
        if winerror == 112 or exc.errno == errno.ENOSPC:
            return f"No hay espacio suficiente en el disco durante «{operation}».{suffix}"
        return f"Error del sistema durante «{operation}»: {exc}{suffix}"
    if isinstance(exc, ValueError):
        return f"Datos no válidos durante «{operation}»: {exc}{suffix}"
    return f"Error inesperado durante «{operation}»: {exc}{suffix}"


def _log_failure(
    operation: str,
    source: Path,
    destination: Path | None,
    exc: BaseException | None,
    message: str,
) -> None:
    LOGGER.error(
        "Fallo al %s | origen=%s | destino=%s | detalle=%s | excepción=%r",
        operation,
        source,
        destination if destination is not None else "—",
        message,
        exc,
        exc_info=exc is not None,
    )


def _remove_placeholder(candidate: Path) -> None:
    try:
        candidate.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning(
            "No se pudo eliminar el marcador temporal de destino: %s", candidate
        )


def move_file_safely(
    source: Path | str,
    destination_dir: Path | str,
    *,
    target_name: str | None = None,
    dry_run: bool = False,
    collision_policy: str = "rename",
    allowed_roots: Iterable[Path | str] | None = None,
    reserved: set[str] | None = None,
) -> MoveResult:
    """Mueve un archivo regular a una carpeta, de forma segura.

    Reglas aplicadas:

    * Solo archivos regulares; nunca directorios, enlaces ni dispositivos.
    * No sobrescribe: reserva un nombre libre con ``O_EXCL`` antes de mover.
    * Renombra ante colisiones (``archivo_1.ext``) o las omite.
    * Comprueba que origen y destino estén dentro de ``allowed_roots``.
    * Vuelve a validar el archivo justo antes de moverlo.
    * Con ``dry_run=True`` no crea carpetas ni toca el archivo.
    """

    source_path = Path(source)
    destination_path = Path(destination_dir)
    if collision_policy not in _ALLOWED_POLICIES:
        raise ValueError("collision_policy debe ser 'rename' o 'skip'")

    roots = [Path(root) for root in allowed_roots] if allowed_roots is not None else None

    # ------------------------------------------------------------- 1. Validar origen
    try:
        source_lstat = os.lstat(source_path)
    except FileNotFoundError as exc:
        message = (
            "El archivo de origen ya no existe o su carpeta fue movida: "
            f"{source_path}"
        )
        _log_failure("validar el archivo de origen", source_path, None, exc, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, 0)
    except PermissionError as exc:
        message = _friendly_error(
            exc, operation="validar el archivo de origen", source=source_path
        )
        _log_failure("validar el archivo de origen", source_path, None, exc, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, 0)
    except OSError as exc:
        message = _friendly_error(
            exc, operation="validar el archivo de origen", source=source_path
        )
        _log_failure("validar el archivo de origen", source_path, None, exc, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, 0)

    source_size = int(source_lstat.st_size)
    source_mtime_ns = int(getattr(source_lstat, "st_mtime_ns", 0))

    if stat.S_ISLNK(source_lstat.st_mode):
        return MoveResult(
            source_path,
            None,
            STATUS_SKIPPED,
            "Se omitió un enlace simbólico.",
            source_size,
        )
    if not stat.S_ISREG(source_lstat.st_mode):
        return MoveResult(
            source_path,
            None,
            STATUS_SKIPPED,
            "Se omitió porque no es un archivo regular.",
            source_size,
        )

    if not source_path.parent.is_dir():
        message = f"La carpeta de origen ya no existe: {source_path.parent}"
        _log_failure("validar la carpeta de origen", source_path, None, None, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

    if not _within_any(source_path, roots):
        message = f"El archivo está fuera de las rutas de origen permitidas: {source_path}"
        _log_failure("validar la ruta de origen", source_path, None, None, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

    # ------------------------------------------------------------ 2. Validar destino
    if destination_path.is_symlink():
        message = (
            "La carpeta de destino es un enlace simbólico y se rechazó por "
            f"seguridad: {destination_path}"
        )
        _log_failure("validar la carpeta de destino", source_path, destination_path, None, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

    if paths_equal(source_path.parent, destination_path):
        return MoveResult(
            source_path,
            None,
            STATUS_SKIPPED,
            "El archivo ya está en la carpeta de destino.",
            source_size,
        )

    if not _within_any(destination_path, roots):
        message = f"La carpeta de destino está fuera de las rutas permitidas: {destination_path}"
        _log_failure("validar la ruta de destino", source_path, destination_path, None, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

    requested_name = target_name or source_path.name
    try:
        _validate_target_name(requested_name)
    except ValueError as exc:
        message = _friendly_error(
            exc,
            operation="validar el nombre de destino",
            source=source_path,
            destination=destination_path,
        )
        _log_failure("validar el nombre de destino", source_path, destination_path, exc, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

    destination_file = destination_path / requested_name
    if paths_equal(destination_file, source_path):
        return MoveResult(
            source_path,
            None,
            STATUS_SKIPPED,
            "El origen y el destino son el mismo archivo.",
            source_size,
        )

    if destination_path.exists() and not destination_path.is_dir():
        message = f"La ruta de destino existe y no es una carpeta: {destination_path}"
        _log_failure("validar la carpeta de destino", source_path, destination_path, None, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

    # --------------------------------------------------------------- 3. Simulación
    if dry_run:
        try:
            candidate = find_available_destination(
                destination_path, requested_name, reserved=reserved
            )
        except (OSError, ValueError) as exc:
            message = _friendly_error(
                exc,
                operation="calcular el destino simulado",
                source=source_path,
                destination=destination_path,
            )
            _log_failure("calcular el destino simulado", source_path, destination_path, exc, message)
            return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

        if collision_policy == "skip" and os.path.lexists(destination_file):
            return MoveResult(
                source_path,
                destination_file,
                STATUS_SKIPPED,
                "Ya existe un archivo con ese nombre en el destino.",
                source_size,
            )
        renamed = candidate.name != requested_name
        if reserved is not None:
            reserved.add(_normalized(candidate))
        LOGGER.info(
            "Simulación de movimiento | origen=%s | destino=%s | operación=previsualizar",
            source_path,
            candidate,
        )
        return MoveResult(
            source_path,
            candidate,
            STATUS_DRY_RUN,
            "Se movería a la carpeta de destino.",
            source_size,
            renamed=renamed,
        )

    # ------------------------------------------------------ 4. Crear carpeta destino
    if not destination_path.exists():
        try:
            destination_path.mkdir(parents=True, exist_ok=True)
        except FileNotFoundError as exc:
            message = (
                f"No se pudo crear la carpeta de destino «{destination_path}»: "
                "el sistema informó que una ruta necesaria no existe. Comprueba "
                "que la unidad esté disponible y que la carpeta de origen siga "
                "existiendo."
            )
            _log_failure("crear la carpeta de destino", source_path, destination_path, exc, message)
            return MoveResult(source_path, None, STATUS_ERROR, message, source_size)
        except PermissionError as exc:
            message = (
                f"No se pudo crear la carpeta de destino «{destination_path}»: "
                "permiso denegado. Revisa los permisos de la carpeta de origen, "
                "el antivirus o la protección «Acceso controlado a carpetas» de Windows."
            )
            _log_failure("crear la carpeta de destino", source_path, destination_path, exc, message)
            return MoveResult(source_path, None, STATUS_ERROR, message, source_size)
        except OSError as exc:
            message = _friendly_error(
                exc,
                operation="crear la carpeta de destino",
                source=source_path,
                destination=destination_path,
            )
            _log_failure("crear la carpeta de destino", source_path, destination_path, exc, message)
            return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

    if destination_path.is_symlink():
        message = (
            "La carpeta de destino se convirtió en un enlace simbólico; se canceló: "
            f"{destination_path}"
        )
        _log_failure("verificar la carpeta de destino", source_path, destination_path, None, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, source_size)
    if not destination_path.is_dir():
        message = f"La ruta de destino no quedó como una carpeta válida: {destination_path}"
        _log_failure("verificar la carpeta de destino", source_path, destination_path, None, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, source_size)
    if not _within_any(destination_path, roots):
        message = f"La ruta real del destino quedó fuera de las rutas permitidas: {destination_path}"
        _log_failure("verificar la ruta de destino", source_path, destination_path, None, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

    # ------------------------------------------- 5. Revalidar el origen antes de mover
    try:
        current_lstat = os.lstat(source_path)
    except FileNotFoundError as exc:
        message = f"El archivo de origen desapareció antes de moverlo: {source_path}"
        _log_failure("revalidar el archivo de origen", source_path, destination_path, exc, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, source_size)
    except OSError as exc:
        message = _friendly_error(
            exc,
            operation="revalidar el archivo de origen",
            source=source_path,
            destination=destination_path,
        )
        _log_failure("revalidar el archivo de origen", source_path, destination_path, exc, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

    if not stat.S_ISREG(current_lstat.st_mode):
        message = (
            "El origen dejó de ser un archivo regular durante la operación; "
            f"no se moverá: {source_path}"
        )
        _log_failure("revalidar el archivo de origen", source_path, destination_path, None, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

    current_size = int(current_lstat.st_size)
    current_mtime_ns = int(getattr(current_lstat, "st_mtime_ns", 0))
    if (current_size, current_mtime_ns) != (source_size, source_mtime_ns):
        message = (
            "El archivo cambió de tamaño o fecha durante la operación; se dejó "
            f"intacto para no mover una versión inconsistente: {source_path}"
        )
        _log_failure("detectar cambios en el origen", source_path, destination_path, None, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, current_size)

    # ------------------------------------------------ 6. Reservar nombre de destino
    index = 0
    candidate: Path | None = None
    while True:
        proposed = destination_path / _candidate_name(requested_name, index)
        if collision_policy == "skip" and os.path.lexists(proposed):
            return MoveResult(
                source_path,
                proposed,
                STATUS_SKIPPED,
                "Ya existe un archivo con ese nombre en el destino.",
                source_size,
            )
        try:
            descriptor = os.open(
                str(proposed), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            os.close(descriptor)
            candidate = proposed
            break
        except FileExistsError:
            if collision_policy == "skip":
                return MoveResult(
                    source_path,
                    proposed,
                    STATUS_SKIPPED,
                    "Ya existe un archivo con ese nombre en el destino.",
                    source_size,
                )
            index += 1
            if index > 100_000:
                message = (
                    "No se encontró un nombre libre en el destino: "
                    f"{destination_path}"
                )
                _log_failure("reservar el nombre de destino", source_path, destination_path, None, message)
                return MoveResult(source_path, None, STATUS_ERROR, message, source_size)
        except FileNotFoundError as exc:
            message = (
                f"No se pudo reservar el nombre en «{destination_path}»: la carpeta "
                "de destino dejó de existir o el sistema bloqueó la creación del archivo."
            )
            _log_failure("reservar el nombre de destino", source_path, proposed, exc, message)
            return MoveResult(source_path, None, STATUS_ERROR, message, source_size)
        except OSError as exc:
            message = _friendly_error(
                exc,
                operation="reservar el nombre de destino",
                source=source_path,
                destination=proposed,
            )
            _log_failure("reservar el nombre de destino", source_path, proposed, exc, message)
            return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

    assert candidate is not None
    renamed = candidate.name != requested_name

    # ------------------------------------------------------------ 7. Mover archivo
    LOGGER.debug(
        "Moviendo archivo | origen=%s | destino=%s | operación=os.replace",
        source_path,
        candidate,
    )
    try:
        # La reserva exclusiva evita que otro proceso pueda ocupar el nombre.
        # os.replace reemplaza únicamente el marcador vacío que acabamos de crear.
        os.replace(source_path, candidate)
    except FileNotFoundError as exc:
        _remove_placeholder(candidate)
        source_exists = os.path.lexists(source_path)
        destination_exists = destination_path.is_dir()
        if not source_exists:
            message = (
                "El archivo de origen desapareció justo antes de moverlo: "
                f"{source_path}"
            )
        elif not destination_exists:
            message = (
                "La carpeta de destino desapareció justo antes de moverlo: "
                f"{destination_path}"
            )
        else:
            message = (
                f"El sistema no encontró «{source_path}» o «{candidate}» al mover. "
                "Comprueba que la ruta no sea demasiado larga, que la unidad esté "
                "disponible y que antivirus/Control de carpetas no bloquee la operación."
            )
        _log_failure("mover el archivo con os.replace", source_path, candidate, exc, message)
        return MoveResult(source_path, None, STATUS_ERROR, message, source_size)
    except OSError as exc:
        is_cross_device = (
            exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17
        )
        if is_cross_device:
            LOGGER.info(
                "Movimiento entre volúmenes; se usará copia y borrado | origen=%s | destino=%s",
                source_path,
                candidate,
            )
            try:
                shutil.copy2(source_path, candidate)
            except OSError as copy_error:
                _remove_placeholder(candidate)
                message = _friendly_error(
                    copy_error,
                    operation="copiar el archivo entre volúmenes",
                    source=source_path,
                    destination=candidate,
                )
                _log_failure(
                    "copiar el archivo entre volúmenes",
                    source_path,
                    candidate,
                    copy_error,
                    message,
                )
                return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

            try:
                os.unlink(source_path)
            except OSError as unlink_error:
                # La copia de destino es válida, pero el origen sigue existiendo.
                # Se retira la copia para no dejar duplicados silenciosos.
                _remove_placeholder(candidate)
                message = (
                    "El archivo se copió al destino, pero no se pudo eliminar el "
                    f"origen; se retiró la copia para no duplicarlo: {source_path}"
                )
                _log_failure(
                    "eliminar el origen tras copiar",
                    source_path,
                    candidate,
                    unlink_error,
                    message,
                )
                return MoveResult(source_path, None, STATUS_ERROR, message, source_size)
        else:
            _remove_placeholder(candidate)
            message = _friendly_error(
                exc,
                operation="mover el archivo con os.replace",
                source=source_path,
                destination=candidate,
            )
            _log_failure("mover el archivo con os.replace", source_path, candidate, exc, message)
            return MoveResult(source_path, None, STATUS_ERROR, message, source_size)

    LOGGER.info(
        "Archivo movido | origen=%s | destino=%s | operación=move | renombrado=%s",
        source_path,
        candidate,
        renamed,
    )
    return MoveResult(
        source_path,
        candidate,
        STATUS_MOVED,
        "Archivo movido correctamente.",
        source_size,
        renamed=renamed,
    )
