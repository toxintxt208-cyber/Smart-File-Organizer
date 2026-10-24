"""Clasificación de archivos por extensión."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from .config_manager import normalize_extension


class FileClassifier:
    """Clasificador determinista y configurable.

    Las extensiones se comparan sin distinguir mayúsculas de minúsculas. Se
    admiten extensiones compuestas como ``.tar.gz`` y se priorizan las más
    largas para que una regla específica gane sobre una genérica.
    """

    def __init__(
        self,
        categories: Mapping[str, Iterable[str]],
        default_category: str = "Otros",
    ) -> None:
        if not isinstance(categories, Mapping) or not categories:
            raise ValueError("La configuración debe incluir al menos una categoría")

        self.categories: dict[str, tuple[str, ...]] = {}
        extension_map: dict[str, str] = {}
        for raw_category, raw_extensions in categories.items():
            category = str(raw_category).strip()
            if not category:
                continue
            extensions: list[str] = []
            for raw_extension in raw_extensions:
                extension = normalize_extension(raw_extension)
                if extension is None:
                    continue
                if extension in extension_map and extension_map[extension] != category:
                    continue
                extension_map[extension] = category
                extensions.append(extension)
            self.categories[category] = tuple(extensions)

        if not self.categories:
            raise ValueError("No hay categorías válidas")

        self.default_category = str(default_category).strip() or "Otros"
        if self.default_category not in self.categories:
            self.categories[self.default_category] = ()

        self._extension_map = extension_map
        self._ordered_extensions = sorted(
            extension_map, key=lambda item: (-len(item), item)
        )

    def get_extension(self, path: str | Path) -> str:
        """Devuelve la extensión reconocida o la extensión real en minúsculas."""

        file_path = Path(path)
        name = file_path.name.lower()
        for extension in self._ordered_extensions:
            if name.endswith(extension):
                return extension
        suffix = file_path.suffix.lower()
        return suffix if suffix and suffix != "." else ""

    def classify(self, path: str | Path) -> str:
        """Devuelve la categoría de destino para un archivo."""

        file_path = Path(path)
        name = file_path.name.lower()
        if not name or name in {".", ".."}:
            return self.default_category

        for extension in self._ordered_extensions:
            if name.endswith(extension):
                return self._extension_map[extension]
        return self.default_category

    def category_for_extension(self, extension: str) -> str:
        normalized = normalize_extension(extension)
        if normalized is None:
            return self.default_category
        return self._extension_map.get(normalized, self.default_category)

    def rules(self) -> dict[str, list[str]]:
        """Copia serializable de las reglas efectivas."""

        return {category: list(extensions) for category, extensions in self.categories.items()}

    def summary_lines(self) -> list[str]:
        lines = []
        for category, extensions in self.categories.items():
            extension_text = ", ".join(extensions) if extensions else "(extensión libre)"
            lines.append(f"{category}: {extension_text}")
        return lines
