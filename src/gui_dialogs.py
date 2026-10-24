"""Diálogos de la interfaz gráfica: categorías, vista previa e historial."""

from __future__ import annotations

import copy
import re
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Callable, Mapping

from .config_manager import is_valid_category_name, normalize_extension
from .organizer import STATUS_LABELS, ScanReport
from .paths import human_size


def _format_timestamp(value: Any) -> str:
    if not value:
        return "-"
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return text


def center_window(
    window: tk.Toplevel | tk.Tk,
    parent: tk.Misc | None,
    width: int,
    height: int,
) -> None:
    window.update_idletasks()
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    if parent is not None:
        try:
            x = max(0, parent.winfo_rootx() + (parent.winfo_width() - width) // 2)
            y = max(0, parent.winfo_rooty() + (parent.winfo_height() - height) // 2)
        except tk.TclError:
            pass
    window.geometry(f"{width}x{height}+{x}+{y}")


class CategoryDialog(tk.Toplevel):
    """Editor de categorías y extensiones con validación antes de guardar."""

    def __init__(
        self,
        parent: tk.Misc,
        categories: Mapping[str, list[str]],
        default_category: str,
        on_save: Callable[[dict[str, list[str]], str], bool],
    ) -> None:
        super().__init__(parent)
        self.parent = parent
        self.categories: dict[str, list[str]] = {
            str(name): list(extensions) for name, extensions in categories.items()
        }
        self.default_category = default_category
        self.on_save = on_save

        self.title("Gestionar categorías y reglas")
        self.transient(parent)
        self.resizable(True, True)
        self.minsize(680, 440)
        center_window(self, parent, 760, 540)
        self.grab_set()

        self._build()
        self._refresh_categories()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=2)
        outer.rowconfigure(1, weight=1)

        ttk.Label(
            outer,
            text="Categorías y extensiones",
            style="DialogTitle.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        left = ttk.LabelFrame(outer, text="Categorías", padding=8)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        self.category_list = tk.Listbox(
            left,
            exportselection=False,
            activestyle="none",
            height=16,
        )
        self.category_list.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.category_list.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.category_list.configure(yscrollcommand=scroll.set)
        self.category_list.bind("<<ListboxSelect>>", self._on_category_select)

        cat_buttons = ttk.Frame(left)
        cat_buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(cat_buttons, text="Añadir", command=self._add_category).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(cat_buttons, text="Renombrar", command=self._rename_category).pack(
            side="left", padx=4
        )
        ttk.Button(cat_buttons, text="Eliminar", command=self._delete_category).pack(
            side="left", padx=4
        )

        right = ttk.LabelFrame(outer, text="Extensiones de la categoría", padding=8)
        right.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.extension_list = tk.Listbox(
            right,
            exportselection=False,
            activestyle="none",
            height=16,
        )
        self.extension_list.grid(row=0, column=0, sticky="nsew")
        ext_scroll = ttk.Scrollbar(
            right, orient="vertical", command=self.extension_list.yview
        )
        ext_scroll.grid(row=0, column=1, sticky="ns")
        self.extension_list.configure(yscrollcommand=ext_scroll.set)

        ext_buttons = ttk.Frame(right)
        ext_buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(ext_buttons, text="Añadir extensión", command=self._add_extension).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(
            ext_buttons, text="Quitar extensión", command=self._remove_extension
        ).pack(side="left", padx=4)

        note = (
            "Las extensiones se comparan sin distinguir mayúsculas. Los archivos sin "
            f"extensión se envían a «{self.default_category}». Una extensión no puede "
            "pertenecer a dos categorías."
        )
        ttk.Label(outer, text=note, style="Muted.TLabel", wraplength=720).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(12, 8)
        )

        bottom = ttk.Frame(outer)
        bottom.grid(row=3, column=0, columnspan=2, sticky="e")
        ttk.Button(bottom, text="Cancelar", command=self.destroy).pack(
            side="right", padx=(8, 0)
        )
        ttk.Button(
            bottom,
            text="Guardar cambios",
            style="Accent.TButton",
            command=self._save,
        ).pack(side="right")

        self.category_list.focus_set()

    def _selected_category(self, *, require: bool = True) -> str | None:
        selection = self.category_list.curselection()
        if not selection:
            if require:
                messagebox.showinfo(
                    "Categorías", "Selecciona una categoría primero.", parent=self
                )
            return None
        return self.category_list.get(selection[0])

    def _selected_extension(self, *, require: bool = True) -> str | None:
        selection = self.extension_list.curselection()
        if not selection:
            if require:
                messagebox.showinfo(
                    "Extensiones", "Selecciona una extensión primero.", parent=self
                )
            return None
        return self.extension_list.get(selection[0])

    def _refresh_categories(self, select: str | None = None) -> None:
        self.category_list.delete(0, "end")
        names = list(self.categories)
        for name in names:
            self.category_list.insert("end", name)
        if names:
            index = names.index(select) if select in names else 0
            self.category_list.selection_set(index)
            self.category_list.see(index)
        self._refresh_extensions()

    def _refresh_extensions(self) -> None:
        self.extension_list.delete(0, "end")
        category = self._selected_category(require=False)
        if not category:
            return
        for extension in self.categories.get(category, []):
            self.extension_list.insert("end", extension)

    def _on_category_select(self, _event: tk.Event | None = None) -> None:
        self._refresh_extensions()

    def _add_category(self) -> None:
        name = simpledialog.askstring(
            "Nueva categoría", "Nombre de la carpeta de destino:", parent=self
        )
        if name is None:
            return
        name = name.strip()
        if not is_valid_category_name(name):
            messagebox.showerror(
                "Nombre no válido",
                "Usa un nombre de carpeta válido, sin barras ni caracteres especiales.",
                parent=self,
            )
            return
        if name in self.categories:
            messagebox.showerror(
                "Categoría duplicada", f"«{name}» ya existe.", parent=self
            )
            return
        self.categories[name] = []
        self._refresh_categories(select=name)

    def _rename_category(self) -> None:
        old_name = self._selected_category()
        if not old_name:
            return
        if old_name == self.default_category:
            messagebox.showinfo(
                "Categoría predeterminada",
                "La categoría de archivos sin extensión no se puede renombrar aquí.",
                parent=self,
            )
            return
        new_name = simpledialog.askstring(
            "Renombrar categoría",
            "Nuevo nombre de la carpeta:",
            initialvalue=old_name,
            parent=self,
        )
        if new_name is None:
            return
        new_name = new_name.strip()
        if not is_valid_category_name(new_name):
            messagebox.showerror(
                "Nombre no válido", "El nombre no es válido como carpeta.", parent=self
            )
            return
        if new_name in self.categories and new_name != old_name:
            messagebox.showerror(
                "Categoría duplicada", f"«{new_name}» ya existe.", parent=self
            )
            return

        updated: dict[str, list[str]] = {}
        for name, extensions in self.categories.items():
            updated[new_name if name == old_name else name] = extensions
        self.categories = updated
        self._refresh_categories(select=new_name)

    def _delete_category(self) -> None:
        category = self._selected_category()
        if not category:
            return
        if category == self.default_category:
            messagebox.showinfo(
                "Categoría predeterminada",
                "No se puede eliminar la categoría que recibe los archivos sin extensión.",
                parent=self,
            )
            return
        if len(self.categories) <= 1:
            messagebox.showinfo(
                "Categorías", "Debe quedar al menos una categoría.", parent=self
            )
            return
        if not messagebox.askyesno(
            "Eliminar categoría",
            f"¿Eliminar «{category}» y todas sus reglas?\n"
            "Los archivos existentes no se moverán ni se eliminarán.",
            parent=self,
        ):
            return
        del self.categories[category]
        self._refresh_categories()

    def _add_extension(self) -> None:
        category = self._selected_category()
        if not category:
            return
        value = simpledialog.askstring(
            "Añadir extensión",
            "Extensión o extensiones separadas por comas (ejemplo: .pdf, .docx):",
            parent=self,
        )
        if value is None:
            return
        pieces = [part for part in re.split(r"[,;\s]+", value.strip()) if part]
        if not pieces:
            return
        added: list[str] = []
        invalid: list[str] = []
        conflicts: list[str] = []
        current = self.categories.setdefault(category, [])
        for piece in pieces:
            extension = normalize_extension(piece)
            if extension is None:
                invalid.append(piece)
                continue
            owner = next(
                (
                    name
                    for name, extensions in self.categories.items()
                    if name != category and extension in extensions
                ),
                None,
            )
            if owner:
                conflicts.append(f"{extension} (en {owner})")
                continue
            if extension not in current:
                current.append(extension)
                added.append(extension)

        self._refresh_extensions()
        messages: list[str] = []
        if added:
            messages.append("Añadidas: " + ", ".join(added))
        if invalid:
            messages.append("No válidas: " + ", ".join(invalid))
        if conflicts:
            messages.append("Ya pertenecen a otra categoría: " + ", ".join(conflicts))
        if messages:
            messagebox.showinfo("Extensiones", "\n".join(messages), parent=self)

    def _remove_extension(self) -> None:
        category = self._selected_category()
        extension = self._selected_extension()
        if not category or not extension:
            return
        if extension in self.categories.get(category, []):
            self.categories[category].remove(extension)
        self._refresh_extensions()

    def _save(self) -> None:
        if not self.categories:
            messagebox.showerror(
                "Categorías", "Debe existir al menos una categoría.", parent=self
            )
            return
        if self.default_category not in self.categories:
            messagebox.showerror(
                "Categoría predeterminada",
                "La categoría predeterminada debe existir.",
                parent=self,
            )
            return
        if self.on_save(copy.deepcopy(self.categories), self.default_category):
            self.destroy()


class PreviewWindow(tk.Toplevel):
    """Vista previa de una simulación, sin realizar cambios."""

    def __init__(
        self,
        parent: tk.Misc,
        report: ScanReport,
        on_organize: Callable[[ScanReport], None] | None,
    ) -> None:
        super().__init__(parent)
        self.parent = parent
        self.report = report
        self.on_organize = on_organize
        self.title("Previsualización de cambios")
        self.transient(parent)
        self.minsize(850, 480)
        center_window(self, parent, 1080, 650)
        self.grab_set()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        ttk.Label(
            outer, text="Previsualización de cambios", style="DialogTitle.TLabel"
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            outer,
            text=str(self.report.source),
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 10))

        summary = ttk.Frame(outer)
        summary.grid(row=2, column=0, sticky="ew")
        ttk.Label(
            summary,
            text=(
                f"Detectados: {self.report.total}   ·   A mover: {self.report.to_move}   ·   "
                f"Omitidos: {self.report.skipped}   ·   Errores: {self.report.error_count}"
            ),
            style="Summary.TLabel",
        ).pack(anchor="w")

        table_frame = ttk.Frame(outer)
        table_frame.grid(row=3, column=0, sticky="nsew", pady=(8, 8))
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        columns = ("name", "extension", "origin", "destination", "status")
        self.tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="browse"
        )
        headings = {
            "name": "Archivo",
            "extension": "Ext.",
            "origin": "Ubicación original",
            "destination": "Carpeta / destino",
            "status": "Estado",
        }
        widths = {"name": 220, "extension": 70, "origin": 320, "destination": 320, "status": 100}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=60, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(
            table_frame, orient="horizontal", command=self.tree.xview
        )
        xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.tag_configure("error", foreground="#a61b1b")
        self.tree.tag_configure("skipped", foreground="#6b7280")

        for plan in self.report.plans:
            self.tree.insert(
                "",
                "end",
                values=(
                    plan.name,
                    plan.extension or "—",
                    str(plan.source),
                    str(plan.destination) if plan.destination else "—",
                    plan.status_label,
                ),
                tags=(plan.status,),
            )

        errors = self.report.all_errors()
        if errors:
            ttk.Label(outer, text="Errores y advertencias", style="Summary.TLabel").grid(
                row=4, column=0, sticky="w"
            )
            error_text = tk.Text(outer, height=4, wrap="word")
            error_text.grid(row=5, column=0, sticky="ew", pady=(4, 8))
            error_text.insert("1.0", "\n".join(errors))
            error_text.configure(state="disabled")

        bottom = ttk.Frame(outer)
        bottom.grid(row=6, column=0, sticky="e", pady=(8, 0))
        ttk.Button(bottom, text="Cerrar", command=self.destroy).pack(
            side="right", padx=(8, 0)
        )
        if self.on_organize and self.report.to_move > 0:
            ttk.Button(
                bottom,
                text="Organizar ahora",
                style="Accent.TButton",
                command=self._organize,
            ).pack(side="right")

    def _organize(self) -> None:
        callback = self.on_organize
        report = self.report
        self.destroy()
        if callback:
            callback(report)


class HistoryWindow(tk.Toplevel):
    """Historial de operaciones y detalle de cada archivo."""

    def __init__(
        self,
        parent: tk.Misc,
        operations: list[dict[str, Any]],
        on_restore: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.parent = parent
        self.operations = operations
        self.on_restore = on_restore
        self.by_id: dict[str, dict[str, Any]] = {}
        self.title("Historial de movimientos")
        self.transient(parent)
        self.minsize(850, 500)
        center_window(self, parent, 1080, 680)
        self.grab_set()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        if operations:
            first = self.tree.get_children()
            if first:
                self.tree.selection_set(first[0])
                self.tree.focus(first[0])
                self._show_details(first[0])

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(
            outer, text="Historial de movimientos", style="DialogTitle.TLabel"
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            outer,
            text=(
                "Se muestran las operaciones más recientes. La restauración no es una "
                "copia de seguridad: si modificaste o eliminaste archivos, puede no ser posible."
            ),
            style="Muted.TLabel",
            wraplength=1020,
        ).grid(row=1, column=0, sticky="w", pady=(2, 10))

        upper = ttk.Frame(outer)
        upper.grid(row=2, column=0, sticky="nsew")
        upper.rowconfigure(0, weight=1)
        upper.columnconfigure(0, weight=1)

        columns = ("date", "type", "files", "status", "id")
        self.tree = ttk.Treeview(
            upper, columns=columns, show="headings", selectmode="browse", height=10
        )
        headings = {
            "date": "Fecha",
            "type": "Tipo",
            "files": "Archivos",
            "status": "Estado",
            "id": "Identificador",
        }
        widths = {"date": 145, "type": 120, "files": 70, "status": 130, "id": 270}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=70, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(upper, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        for index, operation in enumerate(self.operations):
            iid = str(index)
            self.by_id[iid] = operation
            entries = operation.get("entries", [])
            moved = sum(1 for entry in entries if entry.get("status") == "moved")
            if operation.get("undone_at"):
                status = "Deshecha"
            elif operation.get("cancelled"):
                status = "Detenida"
            elif operation.get("kind") == "restore":
                status = "Restauración"
            elif any(entry.get("status") == "error" for entry in entries):
                status = "Con errores"
            else:
                status = "Completada"
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    _format_timestamp(operation.get("timestamp")),
                    "Restauración" if operation.get("kind") == "restore" else "Organización",
                    moved,
                    status,
                    operation.get("id", ""),
                ),
            )

        detail_frame = ttk.LabelFrame(outer, text="Detalle", padding=8)
        detail_frame.grid(row=3, column=0, sticky="nsew", pady=(8, 8))
        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(0, weight=1)
        self.details = tk.Text(detail_frame, height=9, wrap="word")
        self.details.grid(row=0, column=0, sticky="nsew")
        detail_scroll = ttk.Scrollbar(
            detail_frame, orient="vertical", command=self.details.yview
        )
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.details.configure(yscrollcommand=detail_scroll.set, state="disabled")

        bottom = ttk.Frame(outer)
        bottom.grid(row=4, column=0, sticky="e")
        ttk.Button(bottom, text="Cerrar", command=self.destroy).pack(
            side="right", padx=(8, 0)
        )
        ttk.Button(
            bottom,
            text="Restaurar último movimiento",
            style="Accent.TButton",
            command=self._restore,
        ).pack(side="right")
        if not self.operations:
            self._set_details("Todavía no hay operaciones registradas.")

    def _on_select(self, _event: tk.Event | None = None) -> None:
        selection = self.tree.selection()
        if selection:
            self._show_details(selection[0])

    def _show_details(self, iid: str) -> None:
        operation = self.by_id.get(iid)
        if not operation:
            return
        lines = [
            f"Operación: {operation.get('id', '')}",
            f"Fecha: {_format_timestamp(operation.get('timestamp'))}",
            f"Tipo: {operation.get('kind', 'organize')}",
            f"Raíces: {', '.join(operation.get('roots', [])) or '—'}",
            "",
        ]
        for entry in operation.get("entries", []):
            lines.append(f"Archivo: {entry.get('source', '')}")
            lines.append(f"  Tamaño: {human_size(entry.get('size'))}")
            lines.append(f"  Destino: {entry.get('destination') or '—'}")
            lines.append(f"  Estado: {STATUS_LABELS.get(entry.get('status'), entry.get('status'))}")
            if entry.get("error"):
                lines.append(f"  Error: {entry['error']}")
            if entry.get("restored_at"):
                lines.append(
                    f"  Restaurado: {_format_timestamp(entry.get('restored_at'))} "
                    f"→ {entry.get('restore_destination') or '—'}"
                )
            lines.append("")
        if not operation.get("entries"):
            lines.append("Sin archivos registrados.")
        self._set_details("\n".join(lines))

    def _set_details(self, text: str) -> None:
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def _restore(self) -> None:
        callback = self.on_restore
        self.destroy()
        if callback:
            callback()
