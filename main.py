#!/usr/bin/env python3
"""Punto de entrada de Smart File Organizer.

Ejemplos:
    python main.py
    python main.py --source "C:\\Users\\Usuario\\Downloads" --dry-run
    python main.py --source "C:\\Users\\Usuario\\Downloads" --organize
    python main.py --validate-config
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence

if getattr(sys, "frozen", False):
    # En un .exe, los módulos se desempaquetan en _MEIPASS. Nunca se usan esa
    # carpeta ni la del ejecutable para guardar configuración o historial.
    PROJECT_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import __app_name__, __version__  # noqa: E402
from src.autostart import StartupManager  # noqa: E402
from src.config_manager import ConfigError, ConfigManager  # noqa: E402
from src.file_watcher import WATCHDOG_AVAILABLE, FileWatcher  # noqa: E402
from src.history_manager import HistoryManager  # noqa: E402
from src.locking import LockBusyError  # noqa: E402
from src.logging_config import setup_logging  # noqa: E402
from src.organizer import (  # noqa: E402
    OperationResult,
    Organizer,
    ScanReport,
)
from src.scheduler import OrganizationScheduler  # noqa: E402

LOGGER = logging.getLogger("smart_file_organizer")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DEPENDENCY = 3
EXIT_CANCELLED = 130


class CliError(RuntimeError):
    pass


class _NullStream:
    """Sustituto de stdout/stderr cuando el .exe es ``--windowed``."""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def reconfigure(self, **_kwargs: Any) -> None:
        return None


def _configure_console() -> None:
    # PyInstaller con --windowed deja stdout/stderr en None. Sin estos objetos,
    # cualquier print o argparse falla con AttributeError al ejecutar la CLI.
    if sys.stdout is None:
        sys.stdout = _NullStream()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _NullStream()  # type: ignore[assignment]
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _show_gui_error(title: str, message: str) -> None:
    """Muestra un error fatal incluso si no hay consola (modo .exe)."""

    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message, parent=root)
        root.destroy()
    except Exception:
        LOGGER.debug("No se pudo mostrar el diálogo de error", exc_info=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smart-file-organizer",
        description=(
            "Organiza de forma segura los archivos del primer nivel de Descargas, "
            "Escritorio o una carpeta personalizada."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            '  python main.py --source "C:\\Users\\Usuario\\Downloads" --dry-run\n'
            '  python main.py --source "C:\\Users\\Usuario\\Downloads" --organize\n'
            '  python main.py --source "C:\\Users\\Usuario\\Desktop" --dry-run\n'
            "  python main.py --validate-config\n"
            "  python main.py --stats\n"
            "  python main.py --undo --collision rename\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    parser.add_argument(
        "--source",
        action="append",
        metavar="RUTA",
        help="Carpeta de origen. Puede repetirse. Si se omite, se usan los orígenes habilitados.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="ARCHIVO",
        help="Ruta alternativa del archivo de configuración.",
    )
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="Marca la ejecución como iniciada por Windows (uso interno del acceso directo).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirma sin preguntar en comandos de inicio con Windows o restauración.",
    )

    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--gui", action="store_true", help="Abrir la interfaz gráfica (predeterminado).")
    modes.add_argument("--analyze", action="store_true", help="Analizar archivos y mostrar el plan.")
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="Simular la organización sin mover ni crear nada.",
    )
    modes.add_argument("--organize", action="store_true", help="Ejecutar la organización real.")
    modes.add_argument("--stats", action="store_true", help="Mostrar estadísticas del historial.")
    modes.add_argument(
        "--validate-config", action="store_true", help="Validar config.json y mostrar sus valores."
    )
    modes.add_argument("--watch", action="store_true", help="Vigilar archivos nuevos con watchdog.")
    modes.add_argument(
        "--schedule",
        action="store_true",
        help="Ejecutar la organización periódica en primer plano (debe estar habilitada).",
    )
    modes.add_argument("--undo", action="store_true", help="Restaurar el último movimiento.")
    modes.add_argument(
        "--install-startup", action="store_true", help="Crear el inicio con Windows."
    )
    modes.add_argument(
        "--remove-startup", action="store_true", help="Eliminar el inicio con Windows."
    )
    parser.add_argument(
        "--collision",
        choices=("ask", "skip", "rename"),
        default="ask",
        help="Decisión al restaurar si el nombre original está ocupado (por defecto: preguntar).",
    )
    return parser


def _manager_from_args(args: argparse.Namespace) -> ConfigManager:
    path = args.config
    if path is not None:
        path = Path(path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
    return ConfigManager(path)


def _home_for(manager: ConfigManager) -> Path:
    return manager.path.parent


def _setup_logging_for(manager: ConfigManager) -> Path:
    home = _home_for(manager)
    return setup_logging(
        log_path=home / "logs" / "smart_file_organizer.log",
        console=False,
    )


def _build_history(config: dict[str, Any], home: Path) -> HistoryManager:
    settings = config.get("history", {})
    return HistoryManager(
        home / "history.json",
        max_operations=int(settings.get("max_operations", 500)),
        lock_path=home / "organizer.lock",
    )


def _build_organizer(
    config: dict[str, Any], home: Path, history: HistoryManager | None = None
) -> Organizer:
    return Organizer(
        config,
        history=history or _build_history(config, home),
        lock_path=home / "organizer.lock",
    )


def _resolve_sources(
    args: argparse.Namespace, config: dict[str, Any], *, require: bool = True
) -> list[Path]:
    raw_sources: list[str]
    if args.source:
        raw_sources = list(args.source)
    else:
        raw_sources = [
            str(entry.get("path", ""))
            for entry in config.get("sources", [])
            if entry.get("enabled", True) and entry.get("path")
        ]

    sources: list[Path] = []
    seen: set[str] = set()
    for raw in raw_sources:
        expanded = os.path.expandvars(os.path.expanduser(str(raw)))
        path = Path(expanded)
        if not path.is_absolute():
            raise CliError(f"La ruta de origen debe ser absoluta: {raw}")
        try:
            key = os.path.normcase(str(path.resolve(strict=False)))
        except OSError:
            key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        if not path.exists():
            raise CliError(f"La carpeta de origen no existe: {path}")
        if not path.is_dir():
            raise CliError(f"La ruta de origen no es una carpeta: {path}")
        sources.append(path.resolve())

    if require and not sources:
        raise CliError(
            "No hay carpetas de origen. Usa --source RUTA o configura orígenes habilitados."
        )
    return sources


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    stdin = sys.stdin
    if stdin is None or not getattr(stdin, "isatty", lambda: False)():
        raise CliError(
            "Se requiere confirmación interactiva. Repite el comando con --yes si estás seguro."
        )
    answer = input(f"{prompt} [s/N]: ").strip().lower()
    return answer in {"s", "si", "sí", "y", "yes"}


def _print_scan(report: ScanReport) -> None:
    print(f"\nOrigen: {report.source}")
    if report.errors:
        print("  Errores de lectura:")
        for error in report.errors:
            print(f"    ! {error}")
    if not report.plans:
        print("  No se detectaron archivos en el primer nivel.")
        return
    for plan in report.plans:
        destination = str(plan.destination) if plan.destination else "—"
        extra = f" ({plan.message})" if plan.message else ""
        print(
            f"  [{plan.status_label:<9}] {plan.name} -> {destination}"
            f"{extra}"
        )
    print(
        f"\nResumen: {report.total} detectados · {report.to_move} por mover · "
        f"{report.skipped} omitidos · {report.error_count} errores"
    )


def _print_operation(result: OperationResult) -> None:
    for row in result.display_rows():
        destination = row.get("destination") or "—"
        message = row.get("message") or ""
        extra = f" ({message})" if message and row.get("status") == "error" else ""
        print(
            f"  [{str(row.get('status_label', '')).strip():<9}] "
            f"{row.get('name', '')} -> {destination}{extra}"
        )
    print("\n" + result.summary_text(result.dry_run))
    if result.cancelled:
        print("El proceso se detuvo de forma segura.")
    if result.warnings:
        for warning in result.warnings:
            print(f"AVISO: {warning}")
    for error in result.all_errors:
        print(f"ERROR: {error}")


def command_analyze(args: argparse.Namespace, config: dict[str, Any], home: Path) -> int:
    sources = _resolve_sources(args, config)
    organizer = _build_organizer(config, home)
    errors = 0
    for source in sources:
        report = organizer.scan(source)
        _print_scan(report)
        errors += report.error_count
    return EXIT_OK if errors == 0 else EXIT_ERROR


def command_organize(
    args: argparse.Namespace, config: dict[str, Any], home: Path, *, dry_run: bool
) -> int:
    sources = _resolve_sources(args, config)
    organizer = _build_organizer(config, home)
    try:
        result = organizer.organize_sources(sources, dry_run=dry_run)
    except LockBusyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print("Simulación (no se ha modificado nada)" if dry_run else "Organización ejecutada")
    print("=" * 70)
    _print_operation(result)
    print(f"Historial guardado: {'sí' if result.history_recorded else 'no'}")
    return EXIT_OK if result.errors == 0 else EXIT_ERROR


def command_stats(args: argparse.Namespace, config: dict[str, Any], home: Path) -> int:
    history = _build_history(config, home)
    stats = history.get_statistics()
    print("Estadísticas de Smart File Organizer")
    print("=" * 70)
    print(f"Archivo de historial : {stats['history_path']}")
    print(f"Operaciones          : {stats['operations']}")
    print(f"Restauraciones       : {stats['restore_operations']}")
    print(f"Archivos movidos     : {stats['files_moved']}")
    print(f"Archivos restaurados : {stats['restored']}")
    print(f"Errores registrados  : {stats['errors']}")
    print(f"Última operación     : {stats['last_operation'] or '—'}")
    if history.recovered:
        print("AVISO: el historial estaba dañado y se creó uno nuevo.")
    return EXIT_OK


def command_validate(
    args: argparse.Namespace, config: dict[str, Any], manager: ConfigManager
) -> int:
    result = manager.validate()
    print("Validación de configuración")
    print("=" * 70)
    print(f"Archivo : {manager.path}")
    print(f"Estado  : {'válida' if result.valid else 'con errores'}")
    for error in result.errors:
        print(f"ERROR: {error}")
    for warning in result.warnings:
        print(f"AVISO: {warning}")

    normalized = result.config
    print(f"Orígenes configurados : {len(normalized.get('sources', []))}")
    for entry in normalized.get("sources", []):
        path = Path(str(entry.get("path", "")))
        state = "existe" if path.is_dir() else "NO EXISTE"
        enabled = "habilitado" if entry.get("enabled", True) else "deshabilitado"
        print(f"  - {entry.get('name', '')}: {path} ({state}, {enabled})")
    print(f"Categorías            : {len(normalized.get('categories', {}))}")
    print(f"Historial             : {normalized.get('history', {}).get('max_operations', 500)} operaciones")
    automation = normalized.get("automation", {})
    print(
        f"Automático            : {'activado' if automation.get('enabled') else 'desactivado'} "
        f"(cada {automation.get('interval_minutes', 30)} min)"
    )
    print(f"Vigilancia            : {'activada' if normalized.get('watcher', {}).get('enabled') else 'desactivada'}")
    return EXIT_OK if result.valid else EXIT_ERROR


def command_undo(args: argparse.Namespace, config: dict[str, Any], home: Path) -> int:
    history = _build_history(config, home)
    plan = history.plan_undo()
    if plan.operation_id is None:
        print(plan.message or "No hay movimientos restaurables.")
        return EXIT_OK
    print("Última operación restaurable")
    print("=" * 70)
    print(f"Identificador : {plan.operation_id}")
    print(f"Listos        : {plan.ready}")
    print(f"Conflictos    : {plan.conflicts}")
    print(f"No disponibles: {plan.missing}")
    if not _confirm("¿Restaurar los archivos de forma segura?", args.yes):
        print("Restauración cancelada.")
        return EXIT_CANCELLED

    policy = args.collision
    if policy == "ask":
        if not plan.conflicts:
            policy = "skip"
        elif not sys.stdin.isatty():
            print(
                "Hay nombres ocupados y no hay terminal interactiva. "
                "Se omitirán los conflictos; usa --collision rename para renombrar."
            )
            policy = "skip"
        else:
            answer = input(
                "Hay nombres ocupados. [r]enombrar, [o]mitir o [c]ancelar: "
            ).strip().lower()
            if answer.startswith("c"):
                print("Restauración cancelada.")
                return EXIT_CANCELLED
            policy = "rename" if answer.startswith("r") else "skip"

    try:
        report = history.undo_last_operation(policy)
    except LockBusyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print("\nResultado")
    print("=" * 70)
    print(report.summary())
    for result in report.errors:
        print(f"ERROR: {result.source}: {result.message}")
        print("  El archivo se dejó en su ubicación actual.")
    for result in report.skipped:
        print(f"OMITIDO: {result.source}: {result.message}")
    return EXIT_OK if not report.errors else EXIT_ERROR


def command_watch(args: argparse.Namespace, config: dict[str, Any], home: Path) -> int:
    if not WATCHDOG_AVAILABLE:
        print(
            "watchdog no está instalado. Instala la dependencia opcional:\n"
            "  pip install watchdog",
            file=sys.stderr,
        )
        return EXIT_DEPENDENCY
    sources = _resolve_sources(args, config)
    stop_event = threading.Event()
    watchers: list[FileWatcher] = []
    watcher_settings = config.get("watcher", {})
    lock_path = home / "organizer.lock"

    def on_new(path: Path, root: Path) -> None:
        print(f"\nArchivo nuevo detectado: {path.name}")
        organizer = _build_organizer(config, home)
        for attempt in range(30):
            if stop_event.is_set():
                return
            try:
                result = organizer.organize_sources([root], stop_event=stop_event)
            except LockBusyError:
                if attempt == 0:
                    print("Otra organización está en curso; se reintentará.")
                if stop_event.wait(2.0):
                    return
                continue
            except Exception as exc:
                LOGGER.exception("Error en vigilancia")
                print(f"ERROR de vigilancia: {exc}", file=sys.stderr)
                return
            print(result.summary_text(result.dry_run))
            for error in result.all_errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return

    def make_callback(root: Path):
        return lambda path: on_new(path, root)

    try:
        for source in sources:
            watcher = FileWatcher(
                source,
                make_callback(source),
                settle_seconds=float(watcher_settings.get("settle_seconds", 5)),
                poll_interval=float(watcher_settings.get("poll_interval", 1)),
                on_event=lambda level, message: print(
                    f"{'ERROR' if level == 'error' else 'INFO'}: {message}"
                ),
            )
            watcher.start()
            watchers.append(watcher)
            print(f"Vigilando: {source}")
        print("Pulsa Ctrl+C para detener la vigilancia.")
        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\nDeteniendo vigilancia…")
    finally:
        stop_event.set()
        for watcher in watchers:
            watcher.stop(wait=True)
    return EXIT_OK


def command_schedule(args: argparse.Namespace, config: dict[str, Any], home: Path) -> int:
    automation = config.get("automation", {})
    if not automation.get("enabled", False):
        print(
            "La organización automática está desactivada. Actívala de forma expresa "
            "desde la interfaz o editando config.json; no se inicia sola.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    sources = _resolve_sources(args, config)
    interval = int(automation.get("interval_minutes", 30))
    lock_path = home / "organizer.lock"

    def job(stop_event: threading.Event) -> None:
        from src.config_manager import ConfigManager as _ConfigManager

        current_manager = _ConfigManager(args.config) if args.config else ConfigManager()
        current_config = current_manager.load()
        organizer = _build_organizer(current_config, home)
        result = organizer.organize_sources(sources, stop_event=stop_event)
        print(time.strftime("[%H:%M:%S] ") + result.summary_text(result.dry_run), flush=True)
        for error in result.all_errors:
            print(f"ERROR: {error}", file=sys.stderr, flush=True)

    scheduler = OrganizationScheduler(
        interval,
        job,
        run_immediately=bool(automation.get("run_on_start", False)),
        on_event=lambda level, message: print(
            f"{'ERROR' if level == 'error' else 'INFO'}: {message}", flush=True
        ),
        enabled=True,
    )
    scheduler.start()
    print(
        f"Organización automática en primer plano cada {interval} minuto(s). "
        "Pulsa Ctrl+C para detener."
    )
    try:
        while scheduler.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nDeteniendo programador…")
    finally:
        scheduler.stop(wait=True)
    return EXIT_OK


def command_startup(
    args: argparse.Namespace, config_manager: ConfigManager, *, install: bool
) -> int:
    manager = StartupManager()
    if not manager.available:
        print("El inicio con Windows solo está disponible en Windows.", file=sys.stderr)
        return EXIT_USAGE
    if install:
        target = manager.shortcut_path
        print(
            "Se creará un acceso directo en la carpeta Inicio del usuario:\n"
            f"  {target}\n"
            "No se modifica el registro ni se usan tareas programadas."
        )
        if not _confirm("¿Crear el inicio automático?", args.yes):
            print("Operación cancelada.")
            return EXIT_CANCELLED
        result = manager.install()
        if not result.installed:
            print(f"ERROR: {result.message}", file=sys.stderr)
            return EXIT_ERROR
        config = config_manager.load()
        config.setdefault("automation", {})["start_with_windows"] = True
        config_manager.save(config)
        print(f"OK: {result.message}")
        print(f"Ruta: {result.path}")
        if not config.get("automation", {}).get("enabled", False):
            print(
                "AVISO: el acceso directo abrirá la aplicación, pero la organización "
                "automática sigue desactivada."
            )
        return EXIT_OK

    print(f"Se eliminará el inicio automático de: {manager.shortcut_path}")
    if not _confirm("¿Eliminar el inicio automático?", args.yes):
        print("Operación cancelada.")
        return EXIT_CANCELLED
    result = manager.uninstall()
    if not result.installed:
        print(f"ERROR: {result.message}", file=sys.stderr)
        return EXIT_ERROR
    config = config_manager.load()
    config.setdefault("automation", {})["start_with_windows"] = False
    config_manager.save(config)
    print(f"OK: {result.message}")
    return EXIT_OK


def _run_gui(args: argparse.Namespace, manager: ConfigManager) -> int:
    try:
        from src.gui import run_gui
    except ImportError as exc:
        print(f"ERROR: no se pudo cargar tkinter: {exc}", file=sys.stderr)
        return EXIT_DEPENDENCY
    try:
        run_gui(manager, autostart_invocation=args.autostart)
    except Exception as exc:
        LOGGER.exception("Error fatal en la interfaz gráfica")
        message = f"No se pudo iniciar la interfaz gráfica:\n\n{exc}"
        print(f"ERROR: {message}", file=sys.stderr)
        if getattr(sys, "frozen", False):
            _show_gui_error("Smart File Organizer", message)
        return EXIT_ERROR
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.source and not (
        args.analyze or args.dry_run or args.organize or args.watch or args.schedule
    ):
        parser.error(
            "--source requiere uno de: --analyze, --dry-run, --organize, --watch o --schedule."
        )
    if args.collision != "ask" and not args.undo:
        parser.error("--collision solo se puede usar con --undo.")

    manager = _manager_from_args(args)
    try:
        _setup_logging_for(manager)
    except OSError as exc:
        print(f"AVISO: no se pudo configurar el registro: {exc}", file=sys.stderr)

    try:
        config = manager.load()
    except ConfigError as exc:
        LOGGER.exception("No se pudo cargar la configuración")
        print(f"ERROR: {exc}", file=sys.stderr)
        if getattr(sys, "frozen", False):
            _show_gui_error("Configuración", str(exc))
        return EXIT_ERROR

    home = _home_for(manager)
    try:
        if args.analyze:
            return command_analyze(args, config, home)
        if args.dry_run:
            return command_organize(args, config, home, dry_run=True)
        if args.organize:
            return command_organize(args, config, home, dry_run=False)
        if args.stats:
            return command_stats(args, config, home)
        if args.validate_config:
            return command_validate(args, config, manager)
        if args.undo:
            return command_undo(args, config, home)
        if args.watch:
            return command_watch(args, config, home)
        if args.schedule:
            return command_schedule(args, config, home)
        if args.install_startup:
            return command_startup(args, manager, install=True)
        if args.remove_startup:
            return command_startup(args, manager, install=False)
        if args.gui or args.autostart or not any(
            (
                args.analyze,
                args.dry_run,
                args.organize,
                args.stats,
                args.validate_config,
                args.undo,
                args.watch,
                args.schedule,
                args.install_startup,
                args.remove_startup,
            )
        ):
            return _run_gui(args, manager)
        parser.print_help()
        return EXIT_USAGE
    except CliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\nOperación cancelada por el usuario.", file=sys.stderr)
        return EXIT_CANCELLED
    except Exception as exc:
        LOGGER.exception("Error no controlado")
        print(f"ERROR inesperado: {exc}", file=sys.stderr)
        if getattr(sys, "frozen", False) and not any(
            (
                args.analyze,
                args.dry_run,
                args.organize,
                args.stats,
                args.validate_config,
                args.undo,
                args.watch,
                args.schedule,
                args.install_startup,
                args.remove_startup,
            )
        ):
            _show_gui_error(
                "Smart File Organizer",
                f"La aplicación encontró un error inesperado:\n\n{exc}\n\n"
                "Consulta el registro en %APPDATA%\\SmartFileOrganizer\\logs.",
            )
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
