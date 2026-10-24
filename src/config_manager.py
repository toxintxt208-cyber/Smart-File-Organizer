"""Configuración persistente y validación de Smart File Organizer."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .paths import detect_default_sources, get_config_path

LOGGER = logging.getLogger(__name__)

DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "Documentos": [".pdf", ".doc", ".docx", ".odt", ".txt", ".rtf"],
    "Hojas_de_calculo": [".xls", ".xlsx", ".csv", ".ods"],
    "Presentaciones": [".ppt", ".pptx", ".odp"],
    "Imagenes": [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"],
    "Videos": [".mp4", ".mkv", ".mov", ".avi", ".webm"],
    "Audio": [".mp3", ".wav", ".flac", ".aac", ".ogg"],
    "Comprimidos": [".zip", ".rar", ".7z", ".tar", ".gz"],
    "Instaladores": [".exe", ".msi", ".msix"],
    "Codigo": [".py", ".js", ".ts", ".html", ".css", ".java", ".cpp"],
    "Otros": [],
}

DEFAULT_CONFIG_TEMPLATE: dict[str, Any] = {
    "version": 1,
    "sources": [],
    "categories": DEFAULT_CATEGORIES,
    "default_category": "Otros",
    "automation": {
        "enabled": False,
        "interval_minutes": 30,
        "start_with_windows": False,
        "run_on_start": False,
    },
    "watcher": {
        "enabled": False,
        "settle_seconds": 5.0,
        "poll_interval": 1.0,
    },
    "ui": {
        "theme": "clam",
        "window_width": 1200,
        "window_height": 820,
        "last_source": "",
    },
    "history": {
        "enabled": True,
        "max_operations": 500,
    },
    "safety": {
        "skip_hidden": True,
        "skip_system": True,
        "confirm_before_organize": True,
        "first_organization_done": False,
    },
}

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}
_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_EXTENSION_RE = re.compile(r"^\.[^\s\\/:*?\"<>|]+$")


class ConfigError(RuntimeError):
    """Error al leer o guardar la configuración."""


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)


def _deep_defaults() -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG_TEMPLATE)
    config["sources"] = detect_default_sources()
    return config


def default_config() -> dict[str, Any]:
    """Configuración predeterminada con las carpetas detectadas del usuario."""

    return _deep_defaults()


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "si", "sí", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return default


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, number))


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, number))


def is_valid_category_name(name: str) -> bool:
    """Valida un nombre que se usará como carpeta de destino."""

    if not isinstance(name, str):
        return False
    candidate = name.strip()
    if not candidate or candidate in {".", ".."}:
        return False
    if len(candidate) > 80 or candidate.endswith((".", " ")):
        return False
    if _INVALID_NAME_CHARS.search(candidate):
        return False
    if candidate.split(".")[0].upper() in _WINDOWS_RESERVED:
        return False
    return True


def normalize_extension(value: Any) -> str | None:
    """Normaliza una extensión a minúsculas y con punto inicial."""

    if not isinstance(value, str):
        return None
    extension = value.strip().lower()
    if not extension:
        return None
    if not extension.startswith("."):
        extension = f".{extension}"
    if extension in {".", ".."} or len(extension) > 32:
        return None
    if not _EXTENSION_RE.match(extension):
        return None
    return extension


def _normalize_sources(raw_sources: Any, warnings: list[str]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()

    if not isinstance(raw_sources, list):
        if raw_sources is not None:
            warnings.append("'sources' no es una lista; se usarán las carpetas detectadas.")
        return detect_default_sources()

    for index, entry in enumerate(raw_sources, start=1):
        if isinstance(entry, str):
            raw_path = entry
            display_name = ""
            enabled = True
        elif isinstance(entry, Mapping):
            raw_path = entry.get("path", "")
            display_name = entry.get("name", "")
            enabled = _as_bool(entry.get("enabled", True), True)
        else:
            warnings.append(f"Origen #{index} ignorado: formato no válido.")
            continue

        if not isinstance(raw_path, str) or not raw_path.strip():
            warnings.append(f"Origen #{index} ignorado: ruta vacía.")
            continue

        expanded = os.path.expandvars(os.path.expanduser(raw_path.strip()))
        path = Path(expanded)
        if not path.is_absolute():
            warnings.append(f"Origen #{index} ignorado: la ruta debe ser absoluta ({raw_path}).")
            continue
        try:
            path = path.resolve(strict=False)
            key = os.path.normcase(str(path))
        except OSError:
            warnings.append(f"Origen #{index} ignorado: ruta no resoluble ({raw_path}).")
            continue
        if key in seen:
            warnings.append(f"Origen duplicado omitido: {path}")
            continue
        seen.add(key)

        name = str(display_name).strip() if display_name is not None else ""
        if not name:
            name = path.name or "Personalizada"
        name = name[:80]
        sources.append({"name": name, "path": str(path), "enabled": enabled})

    if not sources:
        detected = detect_default_sources()
        if detected:
            warnings.append("No había orígenes válidos; se usaron las carpetas detectadas.")
            sources = detected
    return sources


def normalize_categories(
    raw_categories: Any, raw_default: Any, warnings: list[str]
) -> tuple[dict[str, list[str]], str]:
    if not isinstance(raw_categories, Mapping) or not raw_categories:
        if raw_categories is not None:
            warnings.append("'categories' no es un objeto válido; se usaron las reglas predeterminadas.")
        categories = copy.deepcopy(DEFAULT_CATEGORIES)
    else:
        categories = {}
        for raw_name, raw_extensions in raw_categories.items():
            name = str(raw_name).strip()
            if not is_valid_category_name(name):
                warnings.append(f"Categoría ignorada por nombre no válido: {raw_name!r}")
                continue
            if name in categories:
                warnings.append(f"Categoría duplicada ignorada: {name}")
                continue

            if raw_extensions is None:
                raw_extensions = []
            if isinstance(raw_extensions, str):
                raw_extensions = [raw_extensions]
            if not isinstance(raw_extensions, (list, tuple, set)):
                warnings.append(f"Extensiones no válidas en '{name}'; se dejó la categoría vacía.")
                raw_extensions = []

            extensions: list[str] = []
            for extension in raw_extensions:
                normalized = normalize_extension(extension)
                if normalized is None:
                    warnings.append(f"Extensión ignorada en '{name}': {extension!r}")
                    continue
                if normalized not in extensions:
                    extensions.append(normalized)
            categories[name] = extensions

    if not categories:
        warnings.append("No quedó ninguna categoría válida; se restauraron las predeterminadas.")
        categories = copy.deepcopy(DEFAULT_CATEGORIES)

    default_category = str(raw_default).strip() if isinstance(raw_default, str) else "Otros"
    if not is_valid_category_name(default_category):
        warnings.append(f"Categoría 'Otros' no válida: {default_category!r}; se usó 'Otros'.")
        default_category = "Otros"
    if default_category not in categories:
        if default_category == "Otros":
            categories["Otros"] = []
        else:
            warnings.append(
                f"La categoría predeterminada '{default_category}' no existía; se añadió vacía."
            )
            categories[default_category] = []

    # Una extensión solo puede pertenecer a una categoría. Se respeta el orden
    # del archivo para que el resultado sea determinista.
    used_extensions: dict[str, str] = {}
    for category, extensions in categories.items():
        filtered: list[str] = []
        for extension in extensions:
            if extension in used_extensions and used_extensions[extension] != category:
                warnings.append(
                    f"Extensión duplicada '{extension}' en '{category}' ignorada; "
                    f"ya pertenece a '{used_extensions[extension]}'."
                )
                continue
            used_extensions[extension] = category
            filtered.append(extension)
        categories[category] = filtered

    return categories, default_category


def normalize_config(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Mezcla, valida y normaliza una configuración sin lanzar excepciones."""

    warnings: list[str] = []
    defaults = _deep_defaults()

    if not isinstance(raw, Mapping):
        warnings.append("La configuración no es un objeto JSON; se usaron los valores predeterminados.")
        return defaults, warnings

    config: dict[str, Any] = copy.deepcopy(defaults)
    config["version"] = _as_int(raw.get("version", 1), 1, 1, 999)

    if "sources" in raw:
        config["sources"] = _normalize_sources(raw.get("sources"), warnings)
    else:
        config["sources"] = defaults["sources"]

    categories, default_category = normalize_categories(
        raw.get("categories"), raw.get("default_category", "Otros"), warnings
    )
    config["categories"] = categories
    config["default_category"] = default_category

    automation = raw.get("automation")
    if not isinstance(automation, Mapping):
        if automation is not None:
            warnings.append("'automation' no es válido; se usaron valores seguros.")
        automation = {}
    config["automation"] = {
        "enabled": _as_bool(automation.get("enabled", False), False),
        "interval_minutes": _as_int(
            automation.get("interval_minutes", 30), 30, 1, 1440
        ),
        "start_with_windows": _as_bool(
            automation.get("start_with_windows", False), False
        ),
        "run_on_start": _as_bool(automation.get("run_on_start", False), False),
    }

    watcher = raw.get("watcher")
    if not isinstance(watcher, Mapping):
        if watcher is not None:
            warnings.append("'watcher' no es válido; se usaron valores seguros.")
        watcher = {}
    config["watcher"] = {
        "enabled": _as_bool(watcher.get("enabled", False), False),
        "settle_seconds": _as_float(
            watcher.get("settle_seconds", 5.0), 5.0, 0.5, 300.0
        ),
        "poll_interval": _as_float(
            watcher.get("poll_interval", 1.0), 1.0, 0.2, 10.0
        ),
    }

    ui = raw.get("ui")
    if not isinstance(ui, Mapping):
        if ui is not None:
            warnings.append("'ui' no es válido; se usaron valores seguros.")
        ui = {}
    theme = str(ui.get("theme", "clam")).strip() or "clam"
    config["ui"] = {
        "theme": theme,
        "window_width": _as_int(ui.get("window_width", 1200), 1200, 900, 3840),
        "window_height": _as_int(ui.get("window_height", 820), 820, 600, 2160),
        "last_source": str(ui.get("last_source", "") or ""),
    }

    history = raw.get("history")
    if not isinstance(history, Mapping):
        if history is not None:
            warnings.append("'history' no es válido; se usaron valores seguros.")
        history = {}
    config["history"] = {
        "enabled": _as_bool(history.get("enabled", True), True),
        "max_operations": _as_int(history.get("max_operations", 500), 500, 1, 10000),
    }

    safety = raw.get("safety")
    if not isinstance(safety, Mapping):
        if safety is not None:
            warnings.append("'safety' no es válido; se usaron valores seguros.")
        safety = {}
    config["safety"] = {
        "skip_hidden": _as_bool(safety.get("skip_hidden", True), True),
        "skip_system": _as_bool(safety.get("skip_system", True), True),
        "confirm_before_organize": _as_bool(
            safety.get("confirm_before_organize", True), True
        ),
        "first_organization_done": _as_bool(
            safety.get("first_organization_done", False), False
        ),
    }

    last_source = config["ui"]["last_source"]
    if last_source:
        try:
            candidate = Path(os.path.expandvars(os.path.expanduser(last_source)))
            if not candidate.is_absolute():
                warnings.append("La última carpeta usada no era absoluta; se descartó.")
                config["ui"]["last_source"] = ""
            else:
                config["ui"]["last_source"] = str(candidate.resolve(strict=False))
        except OSError:
            config["ui"]["last_source"] = ""

    return config, warnings


class ConfigManager:
    """Carga y guarda ``config.json`` de forma atómica y tolerante a fallos."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else get_config_path()
        self.last_warnings: list[str] = []
        self.last_error: str | None = None
        self.recovered = False
        self._last_config: dict[str, Any] | None = None
        self._last_mtime: float | None = None

    def _read_json(self) -> tuple[Any, str | None]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return json.load(handle), None
        except FileNotFoundError:
            return None, None
        except json.JSONDecodeError as exc:
            return None, f"JSON dañado en la línea {exc.lineno}, columna {exc.colno}."
        except OSError as exc:
            return None, f"No se pudo leer la configuración: {exc}"

    def _backup_corrupt_file(self) -> Path | None:
        if not self.path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            import shutil

            shutil.copy2(self.path, backup)
            return backup
        except OSError:
            LOGGER.exception("No se pudo respaldar la configuración dañada")
            return None

    def load(self) -> dict[str, Any]:
        """Carga la configuración. Nunca deja la aplicación sin valores válidos."""

        self.last_warnings = []
        self.last_error = None
        self.recovered = False
        raw, read_error = self._read_json()

        if read_error:
            self.last_error = read_error
            self.recovered = True
            backup = self._backup_corrupt_file()
            if backup:
                self.last_warnings.append(f"Configuración dañada respaldada en {backup}.")
            config = default_config()
            LOGGER.warning("%s Se usarán los valores predeterminados.", read_error)
            preserved_warnings = list(self.last_warnings)
            try:
                self.save(config)
            except ConfigError:
                LOGGER.exception("No se pudo guardar la configuración recuperada")
            self.last_warnings = preserved_warnings + self.last_warnings
            self.last_error = read_error
            self._last_config = config
            self.last_warnings.append("Se restauró una configuración segura por defecto.")
            return copy.deepcopy(config)

        if raw is None:
            self.last_warnings.append(
                "No existía config.json; se creó una configuración inicial."
            )
            config = default_config()
            preserved_warnings = list(self.last_warnings)
            try:
                self.save(config)
            except ConfigError:
                LOGGER.exception("No se pudo crear la configuración inicial")
            self.last_warnings = preserved_warnings + self.last_warnings
            self._last_config = config
            return copy.deepcopy(config)

        config, warnings = normalize_config(raw)
        self.last_warnings.extend(warnings)
        self.last_error = None
        self._last_config = config
        try:
            self._last_mtime = self.path.stat().st_mtime
        except OSError:
            self._last_mtime = None
        return copy.deepcopy(config)

    def save(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """Valida y guarda de forma atómica. Devuelve la configuración normalizada."""

        normalized, warnings = normalize_config(dict(config))
        self.last_warnings = warnings
        self.last_error = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(self.path.parent),
                prefix=".config-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(normalized, handle, ensure_ascii=False, indent=2)
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
            self.last_error = f"No se pudo guardar la configuración: {exc}"
            raise ConfigError(self.last_error) from exc

        self._last_config = copy.deepcopy(normalized)
        try:
            self._last_mtime = self.path.stat().st_mtime
        except OSError:
            self._last_mtime = None
        LOGGER.info("Configuración guardada en %s", self.path)
        return copy.deepcopy(normalized)

    def validate(self) -> ValidationResult:
        """Inspecciona el archivo sin escribirlo y devuelve errores/advertencias."""

        raw, read_error = self._read_json()
        if raw is None and read_error is None:
            config, warnings = normalize_config(default_config())
            warnings.insert(0, "No existe config.json; se usarán los valores predeterminados.")
            return ValidationResult(True, [], warnings, config)

        if read_error:
            config, warnings = normalize_config(default_config())
            return ValidationResult(False, [read_error], warnings, config)

        config, warnings = normalize_config(raw)
        return ValidationResult(True, [], warnings, config)
