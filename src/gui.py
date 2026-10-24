"""Interfaz gráfica nativa de Smart File Organizer (tkinter/ttk)."""

from __future__ import annotations

import copy
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from . import __app_name__, __version__
from .autostart import StartupManager
from .config_manager import ConfigError, ConfigManager
from .file_watcher import WATCHDOG_AVAILABLE, FileWatcher
from .gui_dialogs import CategoryDialog, HistoryWindow, PreviewWindow, center_window
from .history_manager import HistoryManager
from .locking import LockBusyError
from .organizer import STATUS_LABELS, OperationResult, Organizer, ScanReport
from .paths import human_size, paths_equal
from .scheduler import OrganizationScheduler

LOGGER = logging.getLogger(__name__)


def _open_folder(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"La carpeta no existe: {path}")
    if not path.is_dir():
        path = path.parent
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class SmartFileOrganizerApp:
    """Ventana principal. Toda actualización de widgets ocurre en el hilo de Tk."""

    def __init__(
        self,
        config_manager: ConfigManager | None = None,
        *,
        autostart_invocation: bool = False,
    ) -> None:
        self.config_manager = config_manager or ConfigManager()
        self.config = self.config_manager.load()
        self.home_dir = self.config_manager.path.parent
        self.history_path = self.home_dir / "history.json"
        self.lock_path = self.home_dir / "organizer.lock"
        history_settings = self.config.get("history", {})
        self.history_manager = HistoryManager(
            self.history_path,
            max_operations=int(history_settings.get("max_operations", 500)),
            lock_path=self.lock_path,
        )
        self.startup_manager = StartupManager()

        self.root = tk.Tk()
        self.root.title(f"{__app_name__} {__version__}")
        self.root.minsize(980, 680)
        width = int(self.config.get("ui", {}).get("window_width", 1200))
        height = int(self.config.get("ui", {}).get("window_height", 820))
        center_window(self.root, None, max(980, width), max(680, height))

        self.queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._closing = False
        self._busy = False
        self._task_callback: Callable[[Any], None] | None = None
        self._task_label = ""
        self._stop_event: threading.Event | None = None
        self._last_scan: ScanReport | None = None
        self._last_scan_source: str | None = None
        self._pending_after_scan: str | None = None
        self._rows: dict[str, dict[str, Any]] = {}
        self._scheduler: OrganizationScheduler | None = None
        self._watcher: FileWatcher | None = None
        self._autostart_invocation = autostart_invocation

        self._configure_style()
        self._build_ui()
        self._load_sources()
        self._refresh_categories()
        self._sync_controls_from_config()
        self._update_watch_button()
        self._update_scheduler_status()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<F5>", lambda _event: self._analyze())
        self.root.bind("<Control-o>", lambda _event: self._browse_source())
        self.root.bind("<Control-r>", lambda _event: self._analyze())

        self._log("info", "Smart File Organizer iniciado.")
        self._log(
            "info",
            "Recomendación: usa «Previsualizar cambios» antes de organizar por primera vez.",
        )
        for warning in self.config_manager.last_warnings:
            self._log("warning", warning)
        if self.config_manager.recovered:
            self._log("warning", "La configuración dañada se recuperó con valores seguros.")
        if autostart_invocation:
            self._log("info", "Aplicación iniciada desde el arranque de Windows.")

        self.root.after(80, self._drain_queue)
        if self.config.get("automation", {}).get("enabled", False):
            self.root.after(250, self._start_scheduler)
        if self.config.get("watcher", {}).get("enabled", False):
            self.root.after(500, self._start_watcher_from_config)

    # ------------------------------------------------------------------- estilo
    def _configure_style(self) -> None:
        try:
            available = set(tkfont.families(self.root))
        except tk.TclError:
            available = set()
        family = "Segoe UI" if "Segoe UI" in available else "TkDefaultFont"
        self.font_family = family

        self.root.option_add("*Font", (family, 10))
        self.root.configure(background="#edf2f7")
        style = ttk.Style(self.root)
        preferred = str(self.config.get("ui", {}).get("theme", "clam"))
        themes = style.theme_names()
        style.theme_use(preferred if preferred in themes else "clam")
        style.configure(".", font=(family, 10), background="#edf2f7")
        style.configure("TFrame", background="#edf2f7")
        style.configure("TLabel", background="#edf2f7")
        style.configure("TLabelframe", background="#edf2f7")
        style.configure("TLabelframe.Label", foreground="#17324d", font=(family, 10, "bold"))
        style.configure("Title.TLabel", font=(family, 25, "bold"), foreground="#12324a")
        style.configure(
            "Subtitle.TLabel", font=(family, 10), foreground="#5d6d7c"
        )
        style.configure(
            "Section.TLabel", font=(family, 11, "bold"), foreground="#17324d"
        )
        style.configure(
            "DialogTitle.TLabel", font=(family, 16, "bold"), foreground="#12324a"
        )
        style.configure(
            "Summary.TLabel", font=(family, 10, "bold"), foreground="#17324d"
        )
        style.configure("Muted.TLabel", font=(family, 9), foreground="#667684")
        style.configure("Status.TLabel", font=(family, 9), foreground="#4b5c6b")
        style.configure("Accent.TButton", font=(family, 10, "bold"))
        style.configure("Danger.TButton", foreground="#9b1c1c")
        style.configure("Treeview", rowheight=26, font=(family, 9))
        style.configure("Treeview.Heading", font=(family, 9, "bold"))
        style.configure("TButton", padding=(9, 5))
        style.configure("TCheckbutton", background="#edf2f7")
        style.configure("TRadiobutton", background="#edf2f7")
        style.configure("TNotebook", background="#edf2f7")

    # ------------------------------------------------------------------- diseño
    def _build_ui(self) -> None:
        self._action_widgets: list[Any] = []
        main = ttk.Frame(self.root, padding=(18, 14, 18, 10))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=__app_name__, style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="Organiza Descargas y Escritorio con reglas seguras, historial y restauración.",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(
            header, text=f"v{__version__} · Windows", style="Muted.TLabel"
        ).grid(row=0, column=1, rowspan=2, sticky="e")

        source_card = ttk.LabelFrame(main, text="Carpeta de origen", padding=10)
        source_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        source_card.columnconfigure(1, weight=1)

        ttk.Label(source_card, text="Ruta:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.source_var = tk.StringVar()
        self.source_combo = ttk.Combobox(
            source_card, textvariable=self.source_var, state="normal"
        )
        self.source_combo.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.source_combo.bind("<Return>", lambda _event: self._analyze())
        self.source_combo.bind("<<ComboboxSelected>>", self._on_source_selected)
        self.browse_button = ttk.Button(
            source_card, text="Examinar…", command=self._browse_source
        )
        self.browse_button.grid(row=0, column=2, sticky="e")

        toolbar = ttk.Frame(source_card)
        toolbar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.analyze_button = ttk.Button(
            toolbar, text="Analizar archivos", command=self._analyze
        )
        self.analyze_button.pack(side="left", padx=(0, 6))
        self.preview_button = ttk.Button(
            toolbar, text="Previsualizar cambios", command=self._preview_changes
        )
        self.preview_button.pack(side="left", padx=6)
        self.organize_button = ttk.Button(
            toolbar,
            text="Organizar ahora",
            style="Accent.TButton",
            command=self._organize_now,
        )
        self.organize_button.pack(side="left", padx=6)
        self.stop_button = ttk.Button(
            toolbar,
            text="Detener proceso",
            style="Danger.TButton",
            command=self._stop_process,
            state="disabled",
        )
        self.stop_button.pack(side="left", padx=6)

        ttk.Label(
            toolbar,
            text="Solo se procesa el primer nivel; no se elimina ni se sobrescribe nada.",
            style="Muted.TLabel",
        ).pack(side="right")

        body = ttk.Panedwindow(main, orient="horizontal")
        body.grid(row=2, column=0, sticky="nsew")
        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        right = ttk.Frame(body, padding=(8, 0, 0, 0))
        body.add(left, weight=1)
        body.add(right, weight=4)

        self._build_left_panel(left)
        self._build_right_panel(right)

        messages = ttk.LabelFrame(main, text="Mensajes y errores", padding=8)
        messages.grid(row=3, column=0, sticky="ew", pady=(12, 8))
        messages.columnconfigure(0, weight=1)
        self.log_text = tk.Text(messages, height=5, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="ew")
        self.log_text.tag_configure("error", foreground="#a61b1b")
        self.log_text.tag_configure("warning", foreground="#8a5a00")
        self.log_text.tag_configure("info", foreground="#243746")
        log_scroll = ttk.Scrollbar(messages, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        ttk.Button(messages, text="Limpiar", command=self._clear_log).grid(
            row=0, column=2, sticky="ns", padx=(8, 0)
        )

        status = ttk.Frame(main)
        status.grid(row=4, column=0, sticky="ew")
        status.columnconfigure(1, weight=1)
        self.progress = ttk.Progressbar(status, mode="determinate", length=230)
        self.progress.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.status_var = tk.StringVar(value="Listo.")
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        self.auto_status_var = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.auto_status_var, style="Status.TLabel").grid(
            row=0, column=2, sticky="e"
        )

        self._action_widgets.extend(
            [
                self.analyze_button,
                self.preview_button,
                self.organize_button,
                self.browse_button,
                self.source_combo,
            ]
        )

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        categories = ttk.LabelFrame(parent, text="Categorías y extensiones", padding=8)
        categories.grid(row=0, column=0, sticky="nsew")
        categories.columnconfigure(0, weight=1)
        categories.rowconfigure(0, weight=1)
        self.category_tree = ttk.Treeview(
            categories,
            columns=("category", "extensions"),
            show="headings",
            height=9,
            selectmode="browse",
        )
        self.category_tree.heading("category", text="Categoría")
        self.category_tree.heading("extensions", text="Extensiones")
        self.category_tree.column("category", width=115, minwidth=80, anchor="w")
        self.category_tree.column("extensions", width=210, minwidth=120, anchor="w")
        self.category_tree.grid(row=0, column=0, sticky="nsew")
        cat_scroll = ttk.Scrollbar(
            categories, orient="vertical", command=self.category_tree.yview
        )
        cat_scroll.grid(row=0, column=1, sticky="ns")
        self.category_tree.configure(yscrollcommand=cat_scroll.set)
        self.category_tree.bind(
            "<Double-1>", lambda _event: self._open_selected_category()
        )
        self.manage_categories_button = ttk.Button(
            categories, text="Gestionar categorías…", command=self._manage_categories
        )
        self.manage_categories_button.grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
        self._action_widgets.append(self.manage_categories_button)

        automation = ttk.LabelFrame(parent, text="Automatización y vigilancia", padding=8)
        automation.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        automation.columnconfigure(1, weight=1)

        self.auto_var = tk.BooleanVar(value=False)
        self.auto_check = ttk.Checkbutton(
            automation,
            text="Organización automática",
            variable=self.auto_var,
            command=self._toggle_automation,
        )
        self.auto_check.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))

        ttk.Label(automation, text="Cada").grid(row=1, column=0, sticky="w", padx=(22, 4))
        self.interval_var = tk.StringVar(value="30")
        self.interval_combo = ttk.Combobox(
            automation,
            textvariable=self.interval_var,
            values=("15", "30", "60", "120", "240"),
            width=7,
            state="readonly",
        )
        self.interval_combo.grid(row=1, column=1, sticky="w")
        self.interval_combo.bind("<<ComboboxSelected>>", self._on_interval_changed)
        ttk.Label(automation, text="minutos").grid(row=1, column=1, sticky="w", padx=(70, 0))

        self.startup_var = tk.BooleanVar(value=False)
        self.startup_check = ttk.Checkbutton(
            automation,
            text="Iniciar con Windows",
            variable=self.startup_var,
            command=self._toggle_startup,
        )
        self.startup_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.watch_var = tk.BooleanVar(value=False)
        self.watch_check = ttk.Checkbutton(
            automation,
            text="Vigilar archivos nuevos (watchdog)",
            variable=self.watch_var,
            command=self._toggle_watch,
        )
        self.watch_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        watch_row = ttk.Frame(automation)
        watch_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(watch_row, text="Espera").pack(side="left", padx=(22, 4))
        self.settle_var = tk.StringVar(value="5")
        self.settle_spin = ttk.Spinbox(
            watch_row, from_=1, to=300, width=5, textvariable=self.settle_var
        )
        self.settle_spin.pack(side="left")
        self.settle_spin.bind("<FocusOut>", self._apply_settle_seconds)
        self.settle_spin.bind("<Return>", self._apply_settle_seconds)
        ttk.Label(watch_row, text="segundos").pack(side="left", padx=(4, 8))
        self.watch_pause_button = ttk.Button(
            watch_row,
            text="Pausar vigilancia",
            command=self._toggle_watch_pause,
            state="disabled",
        )
        self.watch_pause_button.pack(side="left")

        self.watch_status_var = tk.StringVar(value="Vigilancia detenida")
        ttk.Label(
            automation,
            textvariable=self.watch_status_var,
            style="Muted.TLabel",
            wraplength=290,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(5, 0))
        self.scheduler_status_var = tk.StringVar(value="Automático desactivado")
        ttk.Label(
            automation,
            textvariable=self.scheduler_status_var,
            style="Muted.TLabel",
            wraplength=290,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 0))

        ttk.Label(
            parent,
            text=(
                "La app no elimina archivos, no ejecuta descargas y no solicita "
                "permisos de administrador. La restauración no sustituye una copia de seguridad."
            ),
            style="Muted.TLabel",
            wraplength=300,
        ).grid(row=2, column=0, sticky="w", pady=(10, 0))
        parent.rowconfigure(0, weight=1)

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        table_card = ttk.LabelFrame(parent, text="Archivos detectados", padding=8)
        table_card.grid(row=0, column=0, sticky="nsew")
        table_card.columnconfigure(0, weight=1)
        table_card.rowconfigure(0, weight=1)

        columns = (
            "name",
            "extension",
            "origin",
            "category",
            "destination",
            "size",
            "status",
        )
        self.file_tree = ttk.Treeview(
            table_card, columns=columns, show="headings", selectmode="browse"
        )
        headings = {
            "name": "Archivo",
            "extension": "Ext.",
            "origin": "Ubicación original",
            "category": "Categoría",
            "destination": "Destino",
            "size": "Tamaño",
            "status": "Estado",
        }
        widths = {
            "name": 205,
            "extension": 60,
            "origin": 285,
            "category": 110,
            "destination": 290,
            "size": 75,
            "status": 90,
        }
        for column in columns:
            self.file_tree.heading(column, text=headings[column])
            self.file_tree.column(
                column, width=widths[column], minwidth=55, anchor="w", stretch=True
            )
        self.file_tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(table_card, orient="vertical", command=self.file_tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(
            table_card, orient="horizontal", command=self.file_tree.xview
        )
        xscroll.grid(row=1, column=0, sticky="ew")
        self.file_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.file_tree.tag_configure("error", foreground="#a61b1b")
        self.file_tree.tag_configure("skipped", foreground="#6b7280")
        self.file_tree.tag_configure("moved", foreground="#166534")
        self.file_tree.tag_configure("dry_run", foreground="#1d4ed8")

        self.summary_vars: dict[str, tk.StringVar] = {}
        summary = ttk.Frame(parent)
        summary.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        for index, (key, label) in enumerate(
            (
                ("detected", "Detectados"),
                ("planned", "A mover"),
                ("skipped", "Omitidos"),
                ("errors", "Errores"),
                ("moved", "Movidos"),
            )
        ):
            cell = ttk.Frame(summary)
            cell.grid(row=0, column=index, sticky="w", padx=(0, 24))
            ttk.Label(cell, text=label, style="Muted.TLabel").pack(anchor="w")
            value = tk.StringVar(value="0")
            self.summary_vars[key] = value
            ttk.Label(cell, textvariable=value, style="Summary.TLabel").pack(anchor="w")

        actions = ttk.Frame(parent)
        actions.grid(row=2, column=0, sticky="ew")
        self.open_destination_button = ttk.Button(
            actions, text="Abrir carpeta de destino", command=self._open_destination
        )
        self.open_destination_button.pack(side="left", padx=(0, 6))
        self.history_button = ttk.Button(
            actions, text="Ver historial", command=self._show_history
        )
        self.history_button.pack(side="left", padx=6)
        self.undo_button = ttk.Button(
            actions,
            text="Restaurar último movimiento",
            command=self._restore_last,
        )
        self.undo_button.pack(side="left", padx=6)
        ttk.Button(actions, text="Resumen del historial", command=self._update_history_status).pack(
            side="right"
        )

    # ------------------------------------------------------------- configuración
    def _load_sources(self) -> None:
        values = self._configured_source_paths()
        last_source = str(self.config.get("ui", {}).get("last_source", "") or "")
        selected = ""
        if last_source and Path(last_source).expanduser().is_dir():
            selected = last_source
        elif values:
            selected = next((value for value in values if Path(value).is_dir()), values[0])
        self.source_var.set(selected)
        self._refresh_source_values()
        if not selected:
            self._log(
                "warning",
                "No hay una carpeta de origen configurada. Usa «Examinar…» para elegir una.",
            )

    def _configured_source_paths(self) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for entry in self.config.get("sources", []):
            raw = str(entry.get("path", "") or "")
            if not raw:
                continue
            try:
                resolved = str(Path(raw).expanduser().resolve(strict=False))
                key = os.path.normcase(resolved)
            except OSError:
                resolved = raw
                key = raw
            if key in seen:
                continue
            seen.add(key)
            values.append(resolved)
        return values

    def _refresh_source_values(self) -> None:
        values = self._configured_source_paths()
        current = self.source_var.get().strip()
        if current and not any(paths_equal(current, value) for value in values):
            values.insert(0, current)
        self.source_combo.configure(values=values)

    def _on_source_selected(self, _event: tk.Event | None = None) -> None:
        if self._busy:
            return
        self._last_scan = None
        self._last_scan_source = None
        self._clear_table()
        self._set_summary()
        self._status(f"Carpeta seleccionada: {self.source_var.get()}")
        if self.watch_var.get():
            self._start_watcher()

    def _browse_source(self) -> None:
        if self._busy:
            return
        initial = self.source_var.get().strip() or str(Path.home())
        if not Path(initial).is_dir():
            initial = str(Path.home())
        selected = filedialog.askdirectory(title="Seleccionar carpeta de origen", initialdir=initial)
        if not selected:
            return
        path = Path(selected).resolve()
        self.source_var.set(str(path))
        already_saved = any(
            paths_equal(path, value) for value in self._configured_source_paths()
        )
        if not already_saved:
            remember = messagebox.askyesno(
                "Guardar carpeta",
                "¿Añadir esta carpeta a las carpetas de origen guardadas?\n\n"
                f"{path}",
                parent=self.root,
            )
            if remember:
                self._remember_source(path)
        self._refresh_source_values()
        self._on_source_selected()

    def _remember_source(self, path: Path) -> None:
        candidate = copy.deepcopy(self.config)
        sources = candidate.setdefault("sources", [])
        for entry in sources:
            try:
                if paths_equal(entry.get("path", ""), path):
                    return
            except OSError:
                continue
        sources.append(
            {"name": path.name or "Personalizada", "path": str(path), "enabled": True}
        )
        candidate.setdefault("ui", {})["last_source"] = str(path)
        self.config = candidate
        if self._save_config():
            self._log("info", f"Carpeta guardada como origen: {path}")

    def _selected_source(self, *, show_message: bool = True) -> Path | None:
        raw = self.source_var.get().strip()
        if not raw:
            if show_message:
                messagebox.showinfo(
                    "Carpeta de origen",
                    "Selecciona o escribe una carpeta de origen.",
                    parent=self.root,
                )
            return None
        try:
            path = Path(os.path.expandvars(raw)).expanduser()
        except (OSError, ValueError):
            path = Path(raw)
        if not path.is_absolute():
            if show_message:
                messagebox.showerror(
                    "Ruta no válida",
                    "La carpeta de origen debe ser una ruta absoluta.",
                    parent=self.root,
                )
            return None
        if not path.exists():
            if show_message:
                messagebox.showerror(
                    "Carpeta no encontrada",
                    f"La carpeta no existe:\n{path}",
                    parent=self.root,
                )
            return None
        if not path.is_dir():
            if show_message:
                messagebox.showerror(
                    "Ruta no válida",
                    f"La ruta no es una carpeta:\n{path}",
                    parent=self.root,
                )
            return None
        try:
            return path.resolve(strict=True)
        except OSError as exc:
            if show_message:
                messagebox.showerror("Ruta no válida", str(exc), parent=self.root)
            return None

    def _refresh_categories(self) -> None:
        for item in self.category_tree.get_children():
            self.category_tree.delete(item)
        categories = self.config.get("categories", {})
        for category, extensions in categories.items():
            text = ", ".join(extensions) if extensions else "—"
            self.category_tree.insert("", "end", values=(category, text))
        children = self.category_tree.get_children()
        if children:
            self.category_tree.selection_set(children[0])

    def _manage_categories(self) -> None:
        if self._busy:
            return
        CategoryDialog(
            self.root,
            self.config.get("categories", {}),
            str(self.config.get("default_category", "Otros")),
            self._save_categories,
        )

    def _save_categories(self, categories: dict[str, list[str]], default: str) -> bool:
        candidate = copy.deepcopy(self.config)
        candidate["categories"] = categories
        candidate["default_category"] = default
        try:
            self.config = self.config_manager.save(candidate)
        except ConfigError as exc:
            messagebox.showerror("Configuración", str(exc), parent=self.root)
            return False
        self._refresh_categories()
        self._log("info", "Reglas de clasificación actualizadas.")
        for warning in self.config_manager.last_warnings:
            self._log("warning", warning)
        return True

    def _save_config(self) -> bool:
        try:
            self.config = self.config_manager.save(self.config)
            self.history_manager.max_operations = int(
                self.config.get("history", {}).get("max_operations", 500)
            )
            return True
        except ConfigError as exc:
            messagebox.showerror("Configuración", str(exc), parent=self.root)
            self._log("error", str(exc))
            return False

    def _sync_controls_from_config(self) -> None:
        automation = self.config.get("automation", {})
        watcher = self.config.get("watcher", {})
        self.auto_var.set(bool(automation.get("enabled", False)))
        interval = int(automation.get("interval_minutes", 30))
        interval_text = str(interval)
        values = [str(value) for value in self.interval_combo.cget("values")]
        if interval_text not in values:
            values.append(interval_text)
            self.interval_combo.configure(values=tuple(values))
        self.interval_var.set(interval_text)
        self.settle_var.set(str(watcher.get("settle_seconds", 5)))

        actual_startup = self.startup_manager.is_enabled()
        self.startup_var.set(actual_startup)
        if bool(automation.get("start_with_windows", False)) != actual_startup:
            self.config.setdefault("automation", {})["start_with_windows"] = actual_startup
            self._save_config()
        if not self.startup_manager.available:
            self.startup_check.configure(state="disabled")
            self.startup_var.set(False)

        if not WATCHDOG_AVAILABLE:
            self.watch_check.configure(state="disabled")
            self.watch_var.set(False)
            if bool(watcher.get("enabled", False)):
                self.config.setdefault("watcher", {})["enabled"] = False
                self._save_config()
            self.watch_status_var.set("watchdog no instalado")
            self._log(
                "warning",
                "watchdog no está instalado: la vigilancia en tiempo real está deshabilitada. "
                "El modo manual y el programado siguen disponibles.",
            )
        else:
            self.watch_var.set(bool(watcher.get("enabled", False)))
        self._update_scheduler_status()
        self._update_watch_button()

    # ---------------------------------------------------------------- automático
    def _toggle_automation(self) -> None:
        if self._busy:
            self.auto_var.set(not self.auto_var.get())
            return
        desired = bool(self.auto_var.get())
        if desired:
            interval = self._read_interval()
            if not messagebox.askyesno(
                "Activar organización automática",
                "La aplicación organizará las carpetas configuradas cada "
                f"{interval} minuto(s) mientras esté abierta.\n\n"
                "Solo se procesarán los orígenes habilitados y la app no se "
                "iniciará sola salvo que actives «Iniciar con Windows».\n\n"
                "¿Activar la organización automática?",
                parent=self.root,
            ):
                self.auto_var.set(False)
                return
            self.config.setdefault("automation", {})["enabled"] = True
            self.config["automation"]["interval_minutes"] = interval
            if not self._save_config():
                self.auto_var.set(False)
                return
            self._start_scheduler()
            self._log("info", "Organización automática activada.")
        else:
            self._stop_scheduler()
            self.config.setdefault("automation", {})["enabled"] = False
            self._save_config()
            self._update_scheduler_status()
            self._log("info", "Organización automática desactivada.")
        self._update_scheduler_status()

    def _read_interval(self) -> int:
        try:
            interval = int(str(self.interval_var.get()).strip())
        except ValueError:
            interval = 30
        return min(1440, max(1, interval))

    def _on_interval_changed(self, _event: tk.Event | None = None) -> None:
        interval = self._read_interval()
        self.interval_var.set(str(interval))
        self.config.setdefault("automation", {})["interval_minutes"] = interval
        if not self._save_config():
            return
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.set_interval(interval)
        self._update_scheduler_status()
        self._log("info", f"Intervalo automático: {interval} minuto(s).")

    def _start_scheduler(self) -> None:
        if not self.config.get("automation", {}).get("enabled", False):
            self._update_scheduler_status()
            return
        self._stop_scheduler()
        automation = self.config.get("automation", {})
        interval = int(automation.get("interval_minutes", 30))
        scheduler = OrganizationScheduler(
            interval,
            self._scheduled_job,
            run_immediately=bool(automation.get("run_on_start", False)),
            on_event=self._scheduler_event,
            enabled=True,
        )
        self._scheduler = scheduler
        scheduler.start()
        self._update_scheduler_status()

    def _stop_scheduler(self) -> None:
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is not None:
            try:
                scheduler.stop(wait=False)
            except Exception:
                LOGGER.exception("No se pudo detener el programador")
        self._update_scheduler_status()

    def _scheduled_job(self, stop_event: threading.Event) -> None:
        """Se ejecuta en el hilo del programador; nunca toca widgets."""

        if stop_event.is_set():
            return
        config_copy = copy.deepcopy(self.config)
        sources = [
            entry.get("path", "")
            for entry in config_copy.get("sources", [])
            if entry.get("enabled", True) and entry.get("path")
        ]
        if not sources:
            self._emit("log", ("warning", "Organización automática omitida: no hay orígenes."))
            return
        organizer = Organizer(
            config_copy,
            history=self.history_manager,
            lock_path=self.lock_path,
        )
        result = organizer.organize_sources(sources, stop_event=stop_event)
        self._emit(
            "scheduled_result",
            (result.summary_text(result.dry_run), result.all_errors),
        )

    def _scheduler_event(self, level: str, message: str) -> None:
        self._emit("log", (level, message))

    def _update_scheduler_status(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            text = self._scheduler.status_text()
            self.scheduler_status_var.set(text)
            interval = self._scheduler.interval_minutes
            self.auto_status_var.set(f"Automático: cada {interval} min")
        elif self.config.get("automation", {}).get("enabled", False):
            self.scheduler_status_var.set("Automático activo (iniciando…)")
            self.auto_status_var.set("Automático activado")
        else:
            self.scheduler_status_var.set("Automático desactivado")
            self.auto_status_var.set("")

    # ------------------------------------------------------------------ watchdog
    def _start_watcher_from_config(self) -> None:
        if not self.config.get("watcher", {}).get("enabled", False):
            return
        if not WATCHDOG_AVAILABLE:
            return
        if not self._start_watcher():
            self.watch_var.set(False)
            self.config.setdefault("watcher", {})["enabled"] = False
            self._save_config()

    def _start_watcher(self) -> bool:
        if not WATCHDOG_AVAILABLE:
            self.watch_var.set(False)
            self.watch_status_var.set("watchdog no instalado")
            messagebox.showwarning(
                "Vigilancia no disponible",
                "watchdog no está instalado.\n\n"
                "Instala la dependencia opcional con:\n\npip install watchdog\n\n"
                "Mientras tanto puedes usar los modos manual y programado.",
                parent=self.root,
            )
            return False
        source = self._selected_source()
        if source is None:
            self.watch_var.set(False)
            return False
        self._stop_watcher()
        watcher_config = self.config.get("watcher", {})
        try:
            watcher = FileWatcher(
                source,
                lambda path: self._watcher_callback(path, source),
                settle_seconds=float(watcher_config.get("settle_seconds", 5)),
                poll_interval=float(watcher_config.get("poll_interval", 1)),
                on_event=lambda level, message: self._emit("log", (level, message)),
            )
            watcher.start()
        except Exception as exc:
            LOGGER.exception("No se pudo iniciar la vigilancia")
            self.watch_var.set(False)
            self.watch_status_var.set(f"Error de vigilancia: {exc}")
            messagebox.showerror("Vigilancia", f"No se pudo iniciar la vigilancia:\n{exc}", parent=self.root)
            return False
        self._watcher = watcher
        self.watch_var.set(True)
        self.config.setdefault("watcher", {})["enabled"] = True
        self.watch_status_var.set(watcher.status_text())
        self._update_watch_button()
        self._log("info", f"Vigilancia activada en {source}.")
        return True

    def _stop_watcher(self) -> None:
        watcher = self._watcher
        self._watcher = None
        if watcher is not None:
            try:
                watcher.stop(wait=True)
            except Exception:
                LOGGER.exception("No se pudo detener la vigilancia")
        self.watch_status_var.set("Vigilancia detenida")
        self._update_watch_button()

    def _toggle_watch(self) -> None:
        if self._busy:
            self.watch_var.set(not self.watch_var.get())
            return
        if not self.watch_var.get():
            self._stop_watcher()
            self.config.setdefault("watcher", {})["enabled"] = False
            self._save_config()
            self._log("info", "Vigilancia en tiempo real desactivada.")
            return
        if not WATCHDOG_AVAILABLE:
            self.watch_var.set(False)
            self._start_watcher()
            return
        if not messagebox.askyesno(
            "Activar vigilancia",
            "Los archivos nuevos que terminen de copiarse en la carpeta de origen "
            "se organizarán automáticamente tras un breve periodo de espera.\n\n"
            "¿Activar la vigilancia?",
            parent=self.root,
        ):
            self.watch_var.set(False)
            return
        if not self._start_watcher():
            return
        self._save_config()

    def _watcher_callback(self, path: Path, root: Path) -> None:
        """Callback del hilo de watchdog: solo encola y organiza fuera de Tk."""

        self._emit("log", ("info", f"Archivo nuevo detectado: {path.name}"))
        self._watcher_process(root)

    def _watcher_process(self, root: Path) -> None:
        watcher = self._watcher
        stop_event = watcher.stop_event if watcher else None
        config_copy = copy.deepcopy(self.config)
        if not config_copy.get("watcher", {}).get("enabled", False):
            return
        organizer = Organizer(
            config_copy,
            history=self.history_manager,
            lock_path=self.lock_path,
        )
        # Si hay una organización manual en curso, espera de forma cooperativa.
        for attempt in range(150):
            if stop_event is not None and stop_event.is_set():
                return
            try:
                result = organizer.organize_sources([root], stop_event=stop_event)
            except LockBusyError:
                if attempt == 0:
                    self._emit(
                        "log",
                        ("info", "Archivo nuevo detectado, pero hay otra organización en curso. Se reintentará."),
                    )
                if stop_event is not None:
                    if stop_event.wait(2.0):
                        return
                else:
                    time.sleep(2.0)
                continue
            except Exception as exc:
                LOGGER.exception("Error en la organización por vigilancia")
                self._emit("log", ("error", f"Error de vigilancia: {exc}"))
                return
            self._emit("watch_result", result.summary_text(result.dry_run))
            for error in result.all_errors:
                self._emit("log", ("error", error))
            return

    def _toggle_watch_pause(self) -> None:
        watcher = self._watcher
        if watcher is None:
            return
        try:
            if watcher.paused:
                watcher.resume()
            else:
                watcher.pause()
        except Exception as exc:
            messagebox.showerror("Vigilancia", str(exc), parent=self.root)
        self.watch_status_var.set(watcher.status_text())
        self._update_watch_button()

    def _update_watch_button(self) -> None:
        watcher = self._watcher
        if watcher is None or not watcher.running:
            self.watch_pause_button.configure(state="disabled", text="Pausar vigilancia")
        else:
            self.watch_pause_button.configure(
                state="normal",
                text="Reanudar vigilancia" if watcher.paused else "Pausar vigilancia",
            )

    def _apply_settle_seconds(self, _event: tk.Event | None = None) -> None:
        try:
            value = float(str(self.settle_var.get()).replace(",", "."))
        except ValueError:
            self.settle_var.set(str(self.config.get("watcher", {}).get("settle_seconds", 5)))
            return
        value = min(300.0, max(1.0, value))
        self.settle_var.set(f"{value:g}")
        self.config.setdefault("watcher", {})["settle_seconds"] = value
        if not self._save_config():
            return
        if self._watcher is not None and self.watch_var.get():
            self._start_watcher()
            self._save_config()
        self._log("info", f"Espera de estabilización: {value:g} s.")

    # ------------------------------------------------------------------ arranque
    def _toggle_startup(self) -> None:
        desired = bool(self.startup_var.get())
        if desired:
            if not self.startup_manager.available:
                self.startup_var.set(False)
                messagebox.showwarning(
                    "Inicio con Windows",
                    "Esta opción solo está disponible en Windows.",
                    parent=self.root,
                )
                return
            target = self.startup_manager.shortcut_path
            if not messagebox.askyesno(
                "Iniciar con Windows",
                "Se creará un acceso directo en tu carpeta Inicio:\n\n"
                f"{target}\n\n"
                "No se modifica el registro ni se usa el Programador de tareas, "
                "y no se solicitan permisos de administrador.\n\n"
                "¿Crear el inicio automático?",
                parent=self.root,
            ):
                self.startup_var.set(False)
                return
            result = self.startup_manager.install()
            if not result.installed:
                self.startup_var.set(False)
                messagebox.showerror("Inicio con Windows", result.message, parent=self.root)
                return
            self.startup_var.set(True)
            self.config.setdefault("automation", {})["start_with_windows"] = True
            self._save_config()
            self._log("info", f"{result.message} ({result.path})")
            if not self.auto_var.get():
                self._log(
                    "warning",
                    "El acceso directo abrirá la app al iniciar sesión, pero la "
                    "organización automática está desactivada.",
                )
        else:
            if not self.startup_manager.is_enabled():
                self.config.setdefault("automation", {})["start_with_windows"] = False
                self._save_config()
                return
            if not messagebox.askyesno(
                "Quitar inicio con Windows",
                "Se eliminará el acceso directo de la carpeta Inicio. "
                "¿Continuar?",
                parent=self.root,
            ):
                self.startup_var.set(True)
                return
            result = self.startup_manager.uninstall()
            if not result.installed:
                self.startup_var.set(True)
                messagebox.showerror("Inicio con Windows", result.message, parent=self.root)
                return
            self.startup_var.set(False)
            self.config.setdefault("automation", {})["start_with_windows"] = False
            self._save_config()
            self._log("info", "Inicio con Windows desactivado.")

    # --------------------------------------------------------------- estado/UI
    def _set_busy(self, busy: bool, *, stoppable: bool = False) -> None:
        self._busy = busy
        for widget in self._action_widgets:
            try:
                widget.configure(state="disabled" if busy else "normal")
            except tk.TclError:
                pass
        if not busy:
            self.source_combo.configure(state="normal")
            if not WATCHDOG_AVAILABLE:
                self.watch_check.configure(state="disabled")
            if not self.startup_manager.available:
                self.startup_check.configure(state="disabled")
            self._update_watch_button()
        self.stop_button.configure(state="normal" if busy and stoppable else "disabled")
        self.undo_button.configure(state="disabled" if busy else "normal")
        try:
            self.root.configure(cursor="watch" if busy else "")
        except tk.TclError:
            pass

    def _set_progress(self, done: int, total: int, name: str = "") -> None:
        if total <= 0:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            return
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=total, value=done)
        if name:
            self._status(f"Procesando {done}/{total}: {name}")

    def _set_summary(
        self,
        *,
        detected: int = 0,
        planned: int = 0,
        skipped: int = 0,
        errors: int = 0,
        moved: int = 0,
    ) -> None:
        values = {
            "detected": detected,
            "planned": planned,
            "skipped": skipped,
            "errors": errors,
            "moved": moved,
        }
        for key, value in values.items():
            self.summary_vars[key].set(str(value))

    def _summary_from_scan(self, report: ScanReport) -> None:
        self._set_summary(
            detected=report.total,
            planned=report.to_move,
            skipped=report.skipped,
            errors=report.error_count,
        )

    def _summary_from_result(self, result: OperationResult) -> None:
        self._set_summary(
            detected=result.total,
            planned=result.planned,
            skipped=result.skipped,
            errors=result.errors,
            moved=result.moved,
        )

    def _status(self, message: str) -> None:
        self.status_var.set(message)

    def _emit(self, kind: str, payload: Any = None) -> None:
        self.queue.put((kind, payload))

    def _drain_queue(self) -> None:
        if self._closing:
            return
        processed = 0
        try:
            while processed < 100:
                kind, payload = self.queue.get_nowait()
                processed += 1
                if kind == "log":
                    level, message = payload
                    self._append_log(level, message)
                elif kind == "progress":
                    done, total, name = payload
                    self._set_progress(done, total, name)
                elif kind == "status":
                    self._status(str(payload))
                elif kind == "task_success":
                    self._on_task_success(payload)
                elif kind == "task_failure":
                    label, message = payload
                    self._on_task_failure(label, message)
                elif kind == "task_lock":
                    self._finish_task()
                    self._status("Otra organización está en curso.")
                    messagebox.showwarning(
                        "Organización en curso",
                        str(payload),
                        parent=self.root,
                    )
                elif kind == "scheduled_result":
                    summary, errors = payload
                    self._log("info", f"Programador: {summary}")
                    for error in errors:
                        self._log("error", error)
                    self._status(summary)
                    self.auto_status_var.set("Automático: última ejecución completada")
                elif kind == "watch_result":
                    self._log("info", f"Vigilancia: {payload}")
                    self._status(str(payload))
        except queue.Empty:
            pass
        finally:
            if not self._closing:
                self.root.after(90, self._drain_queue)

    def _append_log(self, level: str, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        prefix = {"error": "ERROR", "warning": "AVISO", "info": "INFO"}.get(
            level, level.upper()
        )
        line = f"[{timestamp}] {prefix}: {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line, level)
        self.log_text.see("end")
        try:
            line_count = int(self.log_text.index("end-1c").split(".")[0])
            if line_count > 900:
                self.log_text.delete("1.0", "200.0")
        except (tk.TclError, ValueError):
            pass
        self.log_text.configure(state="disabled")
        getattr(LOGGER, level if level in {"error", "warning", "info"} else "info")(
            "%s", message
        )

    def _log(self, level: str, message: str) -> None:
        self._append_log(level, message)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _update_history_status(self) -> None:
        try:
            stats = self.history_manager.get_statistics()
        except Exception as exc:
            self._log("error", f"No se pudo leer el historial: {exc}")
            return
        self._log(
            "info",
            "Historial: "
            f"{stats['operations']} operaciones, {stats['files_moved']} archivos movidos, "
            f"{stats['restored']} restaurados, {stats['errors']} errores.",
        )

    # ------------------------------------------------------------------- tareas
    def _start_task(
        self,
        label: str,
        target: Callable[[], Any],
        on_success: Callable[[Any], None],
        *,
        stoppable: bool = True,
    ) -> None:
        if self._busy:
            messagebox.showinfo(
                "Proceso en curso",
                "Espera a que termine la operación actual.",
                parent=self.root,
            )
            return
        self._busy = True
        self._task_label = label
        self._task_callback = on_success
        self._stop_event = threading.Event() if stoppable else None
        self._set_busy(True, stoppable=stoppable)
        self.progress.stop()
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self._status(f"{label}…")

        def worker() -> None:
            try:
                result = target()
            except LockBusyError as exc:
                self._emit("task_lock", str(exc))
            except Exception as exc:  # noqa: BLE001 - se informa al hilo de Tk
                LOGGER.exception("Fallo en la tarea %s", label)
                self._emit("task_failure", (label, str(exc)))
            else:
                self._emit("task_success", result)

        threading.Thread(
            target=worker,
            name=f"smart-file-organizer-{label.lower().replace(' ', '-')}",
            daemon=True,
        ).start()

    def _finish_task(self) -> None:
        self._busy = False
        self._task_callback = None
        self._stop_event = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._set_busy(False)
        self._set_progress(0, 0)
        self._update_watch_button()

    def _on_task_success(self, payload: Any) -> None:
        callback = self._task_callback
        self._finish_task()
        if callback is not None:
            try:
                callback(payload)
            except Exception as exc:
                LOGGER.exception("Error al actualizar la interfaz con el resultado")
                self._log("error", f"Error al mostrar el resultado: {exc}")
                messagebox.showerror(
                    "Error de interfaz",
                    f"La operación terminó, pero no se pudo mostrar el resultado:\n{exc}",
                    parent=self.root,
                )

    def _on_task_failure(self, label: str, message: str) -> None:
        self._finish_task()
        self._log("error", f"{label}: {message}")
        self._status(f"{label}: error.")
        messagebox.showerror(
            label,
            f"La operación no pudo completarse:\n\n{message}",
            parent=self.root,
        )

    def _stop_process(self) -> None:
        if self._stop_event is None:
            return
        self._stop_event.set()
        self.stop_button.configure(state="disabled")
        self._status("Deteniendo de forma segura tras el archivo actual…")
        self._log("warning", "Se solicitó detener el proceso. No se interrumpirá un movimiento a medias.")

    def _build_organizer(self) -> Organizer:
        return Organizer(
            copy.deepcopy(self.config),
            history=self.history_manager,
            lock_path=self.lock_path,
        )

    # ------------------------------------------------------------------ análisis
    def _analyze(self) -> None:
        if self._busy:
            return
        self._start_scan(None)

    def _preview_changes(self) -> None:
        if self._busy:
            return
        self._start_scan("preview")

    def _organize_now(self) -> None:
        if self._busy:
            return
        self._start_scan("organize")

    def _start_scan(self, next_action: str | None) -> None:
        source = self._selected_source()
        if source is None:
            return
        self.config.setdefault("ui", {})["last_source"] = str(source)
        self._save_config()
        self._refresh_source_values()
        self._pending_after_scan = next_action
        self._last_scan = None
        self._last_scan_source = None
        self._clear_table()
        self._set_summary()
        self._log("info", f"Analizando {source} (sin modificar archivos)…")
        organizer = self._build_organizer()

        def work() -> ScanReport:
            return organizer.scan(source, stop_event=self._stop_event)

        self._start_task(
            "Analizando archivos",
            work,
            lambda report: self._on_scan_complete(report, source),
            stoppable=True,
        )

    def _on_scan_complete(self, report: ScanReport, source: Path) -> None:
        self._last_scan = report
        self._last_scan_source = str(source)
        self._populate_scan(report)
        self._summary_from_scan(report)
        action = self._pending_after_scan
        self._pending_after_scan = None

        if report.cancelled:
            self._status("Análisis detenido por el usuario.")
            self._log("warning", "El análisis se detuvo antes de terminar.")
        else:
            self._status(
                f"Análisis completado (sin modificar archivos): {report.to_move} por mover, "
                f"{report.skipped} omitidos, {report.error_count} errores."
            )
            self._log(
                "info",
                f"Análisis de {source} (solo lectura): {report.total} detectados, "
                f"{report.to_move} por mover, {report.skipped} omitidos, "
                f"{report.error_count} errores.",
            )
            for warning in report.all_errors():
                self._log("error", warning)
        for error in report.all_errors():
            self._log("error", error)

        if report.cancelled:
            return
        if action == "preview":
            self._show_preview(report)
        elif action == "organize":
            self._confirm_and_organize(report, source)

    def _show_preview(self, report: ScanReport) -> None:
        PreviewWindow(
            self.root,
            report,
            lambda current: self._confirm_and_organize(current, Path(current.source)),
        )

    def _confirm_and_organize(self, report: ScanReport, source: Path) -> None:
        if report.error_count and report.to_move == 0 and report.total == 0:
            messagebox.showerror(
                "No se puede organizar",
                "El análisis encontró errores y no hay archivos que se puedan mover.\n\n"
                + "\n".join(report.all_errors()[:8]),
                parent=self.root,
            )
            return
        if report.to_move == 0:
            messagebox.showinfo(
                "Sin cambios",
                "No hay archivos que mover con las reglas actuales.\n\n"
                f"Omitidos: {report.skipped}\nErrores: {report.error_count}",
                parent=self.root,
            )
            return

        categories: dict[str, int] = {}
        for plan in report.ready_plans:
            categories[plan.category] = categories.get(plan.category, 0) + 1
        destination_text = "\n".join(
            f"  · {category}: {count}" for category, count in sorted(categories.items())
        )
        first_time = not self.config.get("safety", {}).get(
            "first_organization_done", False
        )
        warning = (
            "\n\nEsta es la primera organización en esta instalación. Revisa la "
            "previsualización y el historial; la restauración no es una copia de seguridad."
            if first_time
            else ""
        )
        confirmed = messagebox.askyesno(
            "Confirmar organización",
            f"Se moverán {report.to_move} archivos desde:\n{source}\n\n"
            f"Destinos:\n{destination_text}\n\n"
            f"Omitidos: {report.skipped} · Errores: {report.error_count}\n\n"
            "No se sobrescribirá ningún archivo. Si el destino se modifica antes "
            f"de mover, se recalculará de forma segura.{warning}\n\n"
            "¿Organizar ahora?",
            parent=self.root,
        )
        if not confirmed:
            self._status("Organización cancelada.")
            self._log("info", "La persona usuaria canceló la organización.")
            return
        self._start_organization(source)

    def _start_organization(self, source: Path) -> None:
        organizer = self._build_organizer()
        self._log("info", f"Iniciando organización de {source}.")
        self._status("Organizando archivos…")

        def progress(done: int, total: int, name: str) -> None:
            self._emit("progress", (done, total, name))

        def work() -> OperationResult:
            return organizer.organize_sources(
                [source],
                stop_event=self._stop_event,
                progress=progress,
            )

        self._start_task(
            "Organizando archivos",
            work,
            self._on_organize_complete,
            stoppable=True,
        )

    def _on_organize_complete(self, result: OperationResult) -> None:
        self._populate_result(result)
        self._summary_from_result(result)
        self._last_scan = None
        self._last_scan_source = None
        summary = result.summary_text(result.dry_run)
        self._status(summary)
        self._log("info", summary)
        for error in result.all_errors:
            self._log("error", error)
        for warning in result.warnings:
            self._log("warning", warning)

        if not result.dry_run and not self.config.get("safety", {}).get(
            "first_organization_done", False
        ):
            self.config.setdefault("safety", {})["first_organization_done"] = True
            self._save_config()

        if result.cancelled:
            messagebox.showinfo(
                "Proceso detenido",
                "La organización se detuvo de forma segura.\n\n" + summary,
                parent=self.root,
            )
        elif result.errors:
            details = "\n".join(result.all_errors[:5])
            if result.errors > 5:
                details += f"\n… y {result.errors - 5} error(es) más."
            messagebox.showwarning(
                "Organización con incidencias",
                summary
                + "\n\nLos archivos con error se han dejado intactos. Motivos:\n\n"
                + details
                + "\n\nEl detalle completo está en el área de mensajes y en el registro.",
                parent=self.root,
            )
        elif result.moved:
            self._log(
                "info",
                "Los archivos se movieron a subcarpetas dentro del origen. "
                "Puedes usar el historial para restaurar el último movimiento.",
            )

    # --------------------------------------------------------------------- tabla
    def _clear_table(self) -> None:
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        self._rows.clear()

    def _insert_table_row(self, row: dict[str, Any]) -> str:
        status = str(row.get("status", ""))
        tag = ""
        if status in {"error"}:
            tag = "error"
        elif status in {"skipped", "ready"}:
            tag = "skipped" if status == "skipped" else ""
        elif status in {"moved"}:
            tag = "moved"
        elif status in {"dry_run"}:
            tag = "dry_run"
        destination = str(row.get("destination") or "—")
        iid = self.file_tree.insert(
            "",
            "end",
            values=(
                row.get("name", ""),
                row.get("extension") or "—",
                row.get("source", ""),
                row.get("category", "—"),
                destination,
                human_size(row.get("size")),
                row.get("status_label", STATUS_LABELS.get(status, status)),
            ),
            tags=(tag,) if tag else (),
        )
        self._rows[iid] = row
        return iid

    def _populate_scan(self, report: ScanReport) -> None:
        self._clear_table()
        rows = [
            {
                "name": plan.name,
                "extension": plan.extension,
                "source": str(plan.source),
                "category": plan.category,
                "destination": str(plan.destination) if plan.destination else "",
                "size": plan.size,
                "status": plan.status,
                "status_label": plan.status_label,
                "message": plan.message,
            }
            for plan in report.plans
        ]
        for row in rows:
            self._insert_table_row(row)
        children = self.file_tree.get_children()
        if children:
            self.file_tree.selection_set(children[0])

    def _populate_result(self, result: OperationResult) -> None:
        self._clear_table()
        for row in result.display_rows():
            self._insert_table_row(row)
        children = self.file_tree.get_children()
        if children:
            self.file_tree.selection_set(children[0])

    # ------------------------------------------------------------- abrir carpetas
    def _open_destination(self) -> None:
        destination: Path | None = None
        selection = self.file_tree.selection()
        if selection:
            row = self._rows.get(selection[0], {})
            raw = str(row.get("destination") or "")
            if raw:
                candidate = Path(raw)
                destination = candidate if candidate.is_dir() else candidate.parent
            elif row.get("category") and row.get("source"):
                destination = Path(str(row["source"])).parent / str(row["category"])
        if destination is None:
            selected = self.category_tree.selection()
            if selected:
                category = str(self.category_tree.item(selected[0], "values")[0])
                source = self._selected_source(show_message=False)
                if source is not None:
                    destination = source / category
        if destination is None:
            source = self._selected_source(show_message=False)
            if source is not None:
                destination = source
        if destination is None:
            messagebox.showinfo(
                "Carpeta de destino",
                "Selecciona un archivo o una categoría para abrir su carpeta de destino.",
                parent=self.root,
            )
            return
        try:
            _open_folder(destination)
        except (OSError, FileNotFoundError) as exc:
            messagebox.showinfo(
                "Carpeta no disponible",
                f"La carpeta de destino todavía no existe o no se puede abrir:\n"
                f"{destination}\n\n{exc}",
                parent=self.root,
            )

    def _open_selected_category(self) -> None:
        selection = self.category_tree.selection()
        if not selection:
            return
        category = str(self.category_tree.item(selection[0], "values")[0])
        source = self._selected_source()
        if source is None:
            return
        destination = source / category
        try:
            _open_folder(destination)
        except (OSError, FileNotFoundError) as exc:
            messagebox.showinfo(
                "Carpeta de categoría",
                f"La carpeta «{category}» todavía no existe:\n{destination}\n\n{exc}",
                parent=self.root,
            )

    # ------------------------------------------------------------------ historial
    def _show_history(self) -> None:
        try:
            operations = self.history_manager.get_operations(limit=200)
        except Exception as exc:
            messagebox.showerror("Historial", f"No se pudo leer el historial:\n{exc}", parent=self.root)
            return
        if self.history_manager.recovered:
            self._log(
                "warning",
                "El historial estaba dañado y se creó uno nuevo. "
                "Se conservó una copia de respaldo junto al archivo original.",
            )
        HistoryWindow(self.root, operations, self._restore_last)

    def _restore_last(self) -> None:
        if self._busy:
            return
        try:
            plan = self.history_manager.plan_undo()
        except Exception as exc:
            messagebox.showerror("Restauración", f"No se pudo analizar el historial:\n{exc}", parent=self.root)
            return
        if plan.operation_id is None:
            messagebox.showinfo(
                "Restauración",
                plan.message or "No hay ningún movimiento reciente que se pueda restaurar.",
                parent=self.root,
            )
            return
        if plan.ready == 0 and plan.conflicts == 0:
            messagebox.showinfo(
                "No se puede restaurar",
                "Los archivos de la última operación ya no están disponibles en su "
                "destino original o sus carpetas de origen ya no existen.\n\n"
                "El historial no es una copia de seguridad.",
                parent=self.root,
            )
            return

        summary = (
            f"Operación: {plan.operation_id}\n"
            f"Archivos listos para restaurar: {plan.ready}\n"
            f"Nombres ocupados: {plan.conflicts}\n"
            f"No disponibles: {plan.missing}\n\n"
            "Comprobaciones: el archivo debe existir en el destino, la carpeta "
            "original debe existir y no se sobrescribirá nada.\n\n"
            "Esta restauración no equivale a una copia de seguridad completa. "
            "¿Continuar?"
        )
        if not messagebox.askyesno("Restaurar último movimiento", summary, parent=self.root):
            return

        policy = "skip"
        if plan.conflicts:
            decision = messagebox.askyesnocancel(
                "Nombres originales ocupados",
                f"{plan.conflicts} archivo(s) ya existen en su ubicación original.\n\n"
                "Sí: restaurar los demás y usar nombres alternativos (_1, _2…) "
                "para los conflictos.\n"
                "No: omitir los archivos en conflicto y restaurar el resto.\n"
                "Cancelar: no restaurar nada.",
                parent=self.root,
            )
            if decision is None:
                return
            policy = "rename" if decision else "skip"

        def progress(done: int, total: int) -> None:
            self._emit("progress", (done, total, "Restaurando archivos"))

        def work() -> Any:
            return self.history_manager.undo_last_operation(
                policy,
                progress=progress,
            )

        self._start_task(
            "Restaurando archivos",
            work,
            self._on_restore_complete,
            stoppable=False,
        )

    def _on_restore_complete(self, report: Any) -> None:
        summary = report.summary()
        self._log("info", f"Restauración: {summary}")
        self._status(f"Restauración: {summary}")
        for result in report.errors:
            self._log("error", f"{result.source}: {result.message}")
        for result in report.skipped:
            self._log("warning", f"Omitido: {result.source}: {result.message}")
        self._last_scan = None
        self._last_scan_source = None
        self._clear_table()
        self._set_summary()
        if report.errors:
            messagebox.showwarning(
                "Restauración con incidencias",
                summary
                + "\n\nLos archivos con error se han dejado en su ubicación actual. "
                "Revisa el área de mensajes.",
                parent=self.root,
            )
        elif report.restored_count:
            messagebox.showinfo(
                "Restauración completada",
                summary
                + "\n\nLa restauración no sustituye una copia de seguridad: no puede "
                "recuperar archivos que se hayan modificado o eliminado.",
                parent=self.root,
            )
        elif report.skipped:
            messagebox.showinfo(
                "Restauración omitida",
                summary
                + "\n\nNo se sobrescribió ningún archivo. Puedes reorganizar la "
                "carpeta para actualizar el historial.",
                parent=self.root,
            )

    # ------------------------------------------------------------------- cierre
    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askyesno(
                "Proceso en curso",
                "Hay una operación en curso. Se solicitará una detención segura "
                "y se cerrará la aplicación. ¿Continuar?",
                parent=self.root,
            ):
                return
            if self._stop_event is not None:
                self._stop_event.set()
        try:
            self._stop_scheduler()
        except Exception:
            LOGGER.exception("Error al detener el programador")
        try:
            self._stop_watcher()
        except Exception:
            LOGGER.exception("Error al detener la vigilancia")
        self.config.setdefault("ui", {})["last_source"] = self.source_var.get().strip()
        try:
            width = max(980, self.root.winfo_width())
            height = max(680, self.root.winfo_height())
            self.config.setdefault("ui", {})["window_width"] = width
            self.config["ui"]["window_height"] = height
        except tk.TclError:
            pass
        self._save_config()
        self._closing = True
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def run_gui(
    config_manager: ConfigManager | None = None,
    *,
    autostart_invocation: bool = False,
) -> None:
    app = SmartFileOrganizerApp(
        config_manager,
        autostart_invocation=autostart_invocation,
    )
    app.run()


