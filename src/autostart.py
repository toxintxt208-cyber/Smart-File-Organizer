"""Inicio con Windows mediante la carpeta Inicio del usuario.

No se modifica el registro ni se crean tareas programadas. Se prefiere un
acceso directo (.lnk). Si PowerShell no está disponible, se usa un lanzador
.cmd como alternativa claramente identificable.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .paths import startup_folder

LOGGER = logging.getLogger(__name__)
SHORTCUT_NAME = "Smart File Organizer.lnk"
FALLBACK_NAME = "Smart File Organizer.cmd"


@dataclass
class StartupResult:
    installed: bool
    method: str
    path: Path | None
    message: str


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class StartupManager:
    """Instala o elimina el inicio automático en la carpeta Startup del usuario."""

    def __init__(
        self,
        *,
        project_root: Path | str | None = None,
        python_executable: Path | str | None = None,
        startup_dir: Path | str | None = None,
        use_powershell: bool = True,
    ) -> None:
        if project_root:
            self.project_root = Path(project_root).resolve()
        elif getattr(sys, "frozen", False):
            # En un .exe de PyInstaller ``__file__`` apunta a la carpeta temporal
            # de desempaquetado; el acceso directo debe apuntar al ejecutable real.
            self.project_root = Path(sys.executable).resolve().parent
        else:
            self.project_root = Path(__file__).resolve().parent.parent
        self.main_script = self.project_root / "main.py"
        self.python_executable = Path(python_executable) if python_executable else Path(sys.executable)
        self.startup_dir = Path(startup_dir) if startup_dir else startup_folder()
        self.use_powershell = use_powershell
        self.shortcut_path = (
            self.startup_dir / SHORTCUT_NAME if self.startup_dir else None
        )
        self.fallback_path = (
            self.startup_dir / FALLBACK_NAME if self.startup_dir else None
        )

    @property
    def available(self) -> bool:
        return self.startup_dir is not None and os.name == "nt"

    def is_enabled(self) -> bool:
        return bool(
            (self.shortcut_path and self.shortcut_path.exists())
            or (self.fallback_path and self.fallback_path.exists())
        )

    def _launcher_target(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable)
        if self.python_executable.name.lower() == "python.exe":
            pythonw = self.python_executable.with_name("pythonw.exe")
            if pythonw.exists():
                return pythonw
        return self.python_executable

    def _arguments(self) -> str:
        if getattr(sys, "frozen", False):
            return "--autostart"
        return f'"{self.main_script}" --autostart'

    def _create_shortcut(self) -> None:
        if self.shortcut_path is None:
            raise OSError("No se pudo determinar la carpeta Inicio del usuario.")
        self.startup_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
        target = self._launcher_target()
        arguments = self._arguments()
        script = (
            "$ws = New-Object -ComObject WScript.Shell; "
            f"$s = $ws.CreateShortcut({_powershell_quote(str(self.shortcut_path))}); "
            f"$s.TargetPath = {_powershell_quote(str(target))}; "
            f"$s.Arguments = {_powershell_quote(arguments)}; "
            f"$s.WorkingDirectory = {_powershell_quote(str(self.project_root))}; "
            "$s.WindowStyle = 1; "
            "$s.Description = 'Smart File Organizer'; "
            "$s.Save()"
        )
        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=creation_flags,
            check=False,
        )
        if completed.returncode != 0 or not self.shortcut_path.exists():
            detail = (completed.stderr or completed.stdout or "").strip()
            raise OSError(f"PowerShell no pudo crear el acceso directo. {detail}")

    def _create_fallback(self) -> None:
        if self.fallback_path is None:
            raise OSError("No se pudo determinar la carpeta Inicio del usuario.")
        self.startup_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
        target = self._launcher_target()
        if getattr(sys, "frozen", False):
            command = f'start "" "{target}" --autostart'
        else:
            command = f'start "" "{target}" "{self.main_script}" --autostart'
        content = (
            "@echo off\r\n"
            "rem Lanzador creado por Smart File Organizer. Elimínalo para desactivar el inicio.\r\n"
            f"{command}\r\n"
        )
        self.fallback_path.write_text(content, encoding="utf-8")

    def install(self) -> StartupResult:
        """Crea el acceso directo. El llamador debe pedir consentimiento antes."""

        if self.startup_dir is None:
            return StartupResult(
                False,
                "none",
                None,
                "La carpeta Inicio del usuario no está disponible en este sistema.",
            )
        self.startup_dir.mkdir(parents=True, exist_ok=True)
        if self.shortcut_path and self.shortcut_path.exists():
            return StartupResult(
                True,
                "shortcut",
                self.shortcut_path,
                "El inicio automático ya estaba activado.",
            )
        if self.fallback_path and self.fallback_path.exists():
            return StartupResult(
                True,
                "cmd",
                self.fallback_path,
                "El inicio automático ya estaba activado mediante un lanzador .cmd.",
            )

        if self.use_powershell:
            try:
                self._create_shortcut()
                LOGGER.info("Acceso directo de inicio creado: %s", self.shortcut_path)
                return StartupResult(
                    True,
                    "shortcut",
                    self.shortcut_path,
                    "Acceso directo creado en la carpeta Inicio del usuario.",
                )
            except (OSError, subprocess.SubprocessError) as exc:
                LOGGER.warning("No se pudo crear el acceso directo: %s", exc)

        try:
            self._create_fallback()
            LOGGER.info("Lanzador de inicio creado: %s", self.fallback_path)
            return StartupResult(
                True,
                "cmd",
                self.fallback_path,
                "Se creó un lanzador .cmd en la carpeta Inicio porque el acceso directo no pudo crearse.",
            )
        except OSError as exc:
            LOGGER.exception("No se pudo crear el lanzador de inicio")
            return StartupResult(False, "none", None, f"No se pudo activar el inicio: {exc}")

    def uninstall(self) -> StartupResult:
        removed: list[Path] = []
        for path in (self.shortcut_path, self.fallback_path):
            if path is not None:
                try:
                    if path.exists():
                        path.unlink()
                        removed.append(path)
                except OSError as exc:
                    LOGGER.exception("No se pudo eliminar %s", path)
                    return StartupResult(
                        False, "none", path, f"No se pudo eliminar el inicio automático: {exc}"
                    )
        if removed:
            return StartupResult(
                True,
                "removed",
                removed[0],
                "Inicio automático desactivado.",
            )
        return StartupResult(
            True, "none", None, "El inicio automático ya estaba desactivado."
        )

    def status_text(self) -> str:
        if not self.available:
            return "Inicio con Windows no disponible"
        if self.is_enabled():
            return "Inicio con Windows activado"
        return "Inicio con Windows desactivado"
