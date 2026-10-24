"""Rutas de la aplicación y detección segura de carpetas de Windows.

La configuración, el historial y los registros se guardan fuera del repositorio,
en el directorio de datos del usuario. Así se evita publicar información personal
por accidente y se mantiene el proyecto limpio para GitHub.
"""

from __future__ import annotations

import ctypes
import os
import uuid
from pathlib import Path
from typing import Iterable

APP_NAME = "SmartFileOrganizer"
APP_DISPLAY_NAME = "Smart File Organizer"

# GUID de las carpetas conocidas de Windows. La API SHGetKnownFolderPath es la
# forma recomendada de obtenerlas cuando el usuario las ha redirigido a OneDrive.
_FOLDER_ID_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"
_FOLDER_ID_DOWNLOADS = "374DE290-123F-4565-9164-39C4925E467B"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def get_app_home() -> Path:
    """Devuelve el directorio de datos de la aplicación.

    Se puede cambiar con ``SMART_FILE_ORGANIZER_HOME``. Es útil para pruebas y
    para una instalación portable, pero nunca se usa para guardar credenciales.
    """

    override = os.environ.get("SMART_FILE_ORGANIZER_HOME")
    if override:
        return Path(os.path.expandvars(override)).expanduser().resolve()

    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if not base:
            base = str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        if not base:
            base = str(Path.home() / ".config")

    return (Path(base) / APP_NAME).resolve()


def ensure_app_dirs(home: Path | None = None) -> Path:
    """Crea el directorio de datos y el de registros si no existen."""

    root = Path(home).expanduser() if home else get_app_home()
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return root


def get_config_path(home: Path | None = None) -> Path:
    return (Path(home) if home else get_app_home()) / "config.json"


def get_history_path(home: Path | None = None) -> Path:
    return (Path(home) if home else get_app_home()) / "history.json"


def get_lock_path(home: Path | None = None) -> Path:
    return (Path(home) if home else get_app_home()) / "organizer.lock"


def get_log_path(home: Path | None = None) -> Path:
    return (Path(home) if home else get_app_home()) / "logs" / "smart_file_organizer.log"


def _guid_from_string(value: str) -> _GUID:
    raw = uuid.UUID(value).bytes_le
    return _GUID.from_buffer_copy(raw)


def _known_folder(guid: str) -> Path | None:
    if os.name != "nt":
        return None

    try:
        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
        folder_id = _guid_from_string(guid)
        raw_path = ctypes.c_wchar_p()
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(_GUID),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(raw_path)
        )
        try:
            if result != 0 or not raw_path.value:
                return None
            return Path(raw_path.value)
        finally:
            if raw_path.value:
                ole32.CoTaskMemFree(ctypes.cast(raw_path, ctypes.c_void_p))
    except (AttributeError, OSError, ValueError):
        return None


def _registry_folder(value_name: str) -> Path | None:
    if os.name != "nt":
        return None

    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return None

    locations = (
        (
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ),
        (
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ),
    )
    for hive, key_path in locations:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
            if not isinstance(value, str) or not value.strip():
                continue
            candidate = Path(os.path.expandvars(value)).expanduser()
            if candidate.exists():
                return candidate
        except (OSError, ValueError):
            continue
    return None


def _volume_candidates(folder_name: str) -> Iterable[Path]:
    home = Path.home()
    yield home / folder_name
    if folder_name == "Downloads":
        yield home / "Descargas"

    for env_name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(env_name)
        if value:
            yield Path(os.path.expandvars(value)).expanduser() / folder_name

    # Algunas cuentas conservan la carpeta de OneDrive en el perfil pero sin la
    # variable de entorno. Se revisan solo directorios de primer nivel.
    try:
        for entry in home.iterdir():
            if entry.is_dir() and entry.name.lower().startswith("onedrive"):
                yield entry / folder_name
    except OSError:
        return


def _first_existing(candidates: Iterable[Path]) -> Path | None:
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = os.path.normcase(str(candidate.resolve(strict=False)))
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def detect_known_folders() -> dict[str, Path | None]:
    """Devuelve las rutas reales de Descargas y Escritorio.

    Orden de preferencia: API de carpetas conocidas, registro de Windows,
    variables de OneDrive y, por último, ubicaciones habituales. No se asume
    que las carpetas estén en ``C:\\Users\\<usuario>``.
    """

    desktop = _known_folder(_FOLDER_ID_DESKTOP)
    downloads = _known_folder(_FOLDER_ID_DOWNLOADS)

    if desktop is None or not desktop.is_dir():
        desktop = _registry_folder("Desktop")
    if downloads is None or not downloads.is_dir():
        downloads = _registry_folder("{374DE290-123F-4565-9164-39C4925E467B}")

    if desktop is None or not desktop.is_dir():
        desktop = _first_existing(_volume_candidates("Desktop"))
    if downloads is None or not downloads.is_dir():
        downloads = _first_existing(_volume_candidates("Downloads"))

    return {
        "downloads": downloads.resolve() if downloads else None,
        "desktop": desktop.resolve() if desktop else None,
    }


def detect_default_sources() -> list[dict[str, object]]:
    """Crea las entradas iniciales de origen sin duplicar rutas."""

    detected = detect_known_folders()
    sources: list[dict[str, object]] = []
    seen: set[str] = set()

    for key, display_name in (("downloads", "Descargas"), ("desktop", "Escritorio")):
        path = detected.get(key)
        if path is None:
            continue
        try:
            normalized = os.path.normcase(str(path.resolve()))
        except OSError:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        sources.append({"name": display_name, "path": str(path), "enabled": True})

    return sources


def is_path_within(path: Path | str, root: Path | str) -> bool:
    """Comprueba de forma segura si ``path`` está dentro de ``root``."""

    try:
        path_abs = os.path.normcase(str(Path(path).resolve(strict=False)))
        root_abs = os.path.normcase(str(Path(root).resolve(strict=False)))
        return os.path.commonpath([path_abs, root_abs]) == root_abs
    except (OSError, ValueError):
        return False


def paths_equal(first: Path | str, second: Path | str) -> bool:
    try:
        return os.path.normcase(str(Path(first).resolve(strict=False))) == os.path.normcase(
            str(Path(second).resolve(strict=False))
        )
    except OSError:
        return False


def human_size(size: int | float | None) -> str:
    """Formatea un tamaño de archivo para consola e interfaz."""

    if size is None:
        return "-"
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def startup_folder() -> Path | None:
    """Carpeta Inicio del usuario, sin tocar el registro ni elevar permisos."""

    if os.name != "nt":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
