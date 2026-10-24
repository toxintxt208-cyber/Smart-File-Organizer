from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.config_manager import (
    DEFAULT_CATEGORIES,
    ConfigManager,
    default_config,
    is_valid_category_name,
    normalize_config,
    normalize_extension,
)


class ExtensionTests(unittest.TestCase):
    def test_normalizes_case_and_leading_dot(self) -> None:
        self.assertEqual(normalize_extension("PDF"), ".pdf")
        self.assertEqual(normalize_extension("  .DoCx  "), ".docx")
        self.assertEqual(normalize_extension(".tar.gz"), ".tar.gz")

    def test_rejects_empty_and_unsafe_values(self) -> None:
        for value in ("", "   ", ".", "..", "a/b", "a\\b", "a b", None, 3):
            with self.subTest(value=value):
                self.assertIsNone(normalize_extension(value))

    def test_strips_surrounding_whitespace(self) -> None:
        self.assertEqual(normalize_extension("  .pdf  "), ".pdf")

    def test_rejects_too_long_extensions(self) -> None:
        self.assertIsNone(normalize_extension("." + "a" * 40))


class CategoryNameTests(unittest.TestCase):
    def test_accepts_safe_names(self) -> None:
        for name in ("Documentos", "Hojas_de_calculo", "Mis archivos", "Año 2026"):
            with self.subTest(name=name):
                self.assertTrue(is_valid_category_name(name))

    def test_rejects_unsafe_names(self) -> None:
        for name in ("", "   ", ".", "..", "../fuera", "a/b", "a\\b", "CON", "nul.txt", "malo:", "x" * 81):
            with self.subTest(name=name):
                self.assertFalse(is_valid_category_name(name))


class NormalizationTests(unittest.TestCase):
    def test_default_config_is_safe(self) -> None:
        config = default_config()
        self.assertIn("Otros", config["categories"])
        self.assertEqual(config["default_category"], "Otros")
        self.assertFalse(config["automation"]["enabled"])
        self.assertFalse(config["automation"]["start_with_windows"])
        self.assertFalse(config["watcher"]["enabled"])
        self.assertTrue(config["safety"]["confirm_before_organize"])
        self.assertTrue(config["history"]["enabled"])

    def test_automation_values_are_clamped(self) -> None:
        config, warnings = normalize_config(
            {
                "automation": {
                    "enabled": "yes",
                    "interval_minutes": 0,
                    "start_with_windows": "no",
                },
                "watcher": {"enabled": "true", "settle_seconds": 0.01},
                "history": {"max_operations": 999999},
                "ui": {"window_width": 10, "window_height": 99999},
            }
        )
        self.assertTrue(config["automation"]["enabled"])
        self.assertEqual(config["automation"]["interval_minutes"], 1)
        self.assertFalse(config["automation"]["start_with_windows"])
        self.assertEqual(config["watcher"]["settle_seconds"], 0.5)
        self.assertEqual(config["history"]["max_operations"], 10000)
        self.assertEqual(config["ui"]["window_width"], 900)
        self.assertEqual(config["ui"]["window_height"], 2160)
        self.assertIsInstance(warnings, list)

    def test_invalid_category_names_are_dropped(self) -> None:
        config, warnings = normalize_config(
            {
                "categories": {
                    "../fuera": [".exe"],
                    "CON": [".bat"],
                    "Bien": [".abc"],
                    "Otros": [],
                }
            }
        )
        self.assertIn("Bien", config["categories"])
        self.assertIn("Otros", config["categories"])
        self.assertNotIn("../fuera", config["categories"])
        self.assertNotIn("CON", config["categories"])
        self.assertTrue(any("ignorada" in warning for warning in warnings))

    def test_duplicate_extensions_keep_first_category(self) -> None:
        config, warnings = normalize_config(
            {
                "categories": {
                    "Primero": [".xyz"],
                    "Segundo": [".XYZ"],
                    "Otros": [],
                }
            }
        )
        self.assertEqual(config["categories"]["Primero"], [".xyz"])
        self.assertEqual(config["categories"]["Segundo"], [])
        self.assertTrue(any("duplicada" in warning for warning in warnings))

    def test_default_category_is_created_when_missing(self) -> None:
        config, _ = normalize_config(
            {"categories": {"Documentos": [".pdf"]}, "default_category": "Varios"}
        )
        self.assertEqual(config["default_category"], "Varios")
        self.assertIn("Varios", config["categories"])

    def test_relative_sources_are_ignored(self) -> None:
        config, warnings = normalize_config(
            {"sources": ["carpeta-relativa", {"path": "", "name": "Vacía"}]}
        )
        for entry in config["sources"]:
            self.assertTrue(Path(entry["path"]).is_absolute())
        self.assertTrue(any("absoluta" in warning for warning in warnings))

    def test_duplicate_sources_are_removed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sfo-config-") as temp:
            root = Path(temp) / "origen"
            root.mkdir()
            config, warnings = normalize_config(
                {
                    "sources": [
                        {"name": "A", "path": str(root)},
                        {"name": "B", "path": str(root)},
                    ]
                }
            )
        self.assertEqual(len(config["sources"]), 1)
        self.assertTrue(any("duplicado" in warning.lower() for warning in warnings))


class ConfigManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sfo-config-")
        self.home = Path(self.temp.name)
        self.path = self.home / "config.json"
        self.manager = ConfigManager(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_first_load_creates_default_file(self) -> None:
        config = self.manager.load()
        self.assertTrue(self.path.exists())
        self.assertIn("categories", config)
        self.assertIn("Otros", config["categories"])
        self.assertTrue(any("No existía" in warning for warning in self.manager.last_warnings))

    def test_save_and_load_round_trip(self) -> None:
        source = self.home / "Descargas de prueba"
        source.mkdir()
        config = {
            "sources": [{"name": "Pruebas", "path": str(source), "enabled": True}],
            "categories": {"Documentos": [".pdf"], "Otros": []},
            "default_category": "Otros",
            "automation": {"enabled": False, "interval_minutes": 15},
            "ui": {"last_source": str(source)},
        }
        saved = self.manager.save(config)
        self.assertEqual(saved["automation"]["interval_minutes"], 15)
        self.assertEqual(Path(saved["sources"][0]["path"]), source.resolve())

        loaded = self.manager.load()
        self.assertEqual(loaded["sources"][0]["path"], str(source.resolve()))
        self.assertEqual(loaded["categories"]["Documentos"], [".pdf"])
        self.assertFalse(loaded["automation"]["enabled"])
        self.assertEqual(Path(loaded["ui"]["last_source"]), source.resolve())
        self.assertFalse(self.manager.recovered)

    def test_saved_file_is_valid_utf8_json(self) -> None:
        self.manager.save({"categories": {"Año": [".á"], "Otros": []}})
        with self.path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertIn("Año", raw["categories"])
        self.assertIn(".á", raw["categories"]["Año"])

    def test_corrupt_json_is_backed_up_and_recovered(self) -> None:
        self.path.write_text("{esto no es json", encoding="utf-8")
        config = self.manager.load()

        self.assertTrue(self.manager.recovered)
        self.assertIsNotNone(self.manager.last_error)
        self.assertIn("JSON", self.manager.last_error or "")
        backups = list(self.home.glob("config.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("esto no es json", backups[0].read_text(encoding="utf-8"))
        self.assertIn("Otros", config["categories"])
        with self.path.open("r", encoding="utf-8") as handle:
            self.assertIn("categories", json.load(handle))

    def test_non_object_json_is_normalized(self) -> None:
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        config = self.manager.load()
        self.assertIsInstance(config, dict)
        self.assertIn("categories", config)
        self.assertTrue(any("objeto JSON" in warning for warning in self.manager.last_warnings))

    def test_validate_missing_file_does_not_create_it(self) -> None:
        result = self.manager.validate()
        self.assertTrue(result.valid)
        self.assertFalse(self.path.exists())
        self.assertTrue(any("No existe" in warning for warning in result.warnings))

    def test_validate_reports_corrupt_file(self) -> None:
        self.path.write_text("{roto", encoding="utf-8")
        result = self.manager.validate()
        self.assertFalse(result.valid)
        self.assertTrue(result.errors)
        self.assertFalse(self.manager.recovered)

    def test_validate_normalizes_without_writing(self) -> None:
        self.path.write_text(
            json.dumps({"automation": {"interval_minutes": 0}}), encoding="utf-8"
        )
        before = self.path.read_text(encoding="utf-8")
        result = self.manager.validate()
        self.assertTrue(result.valid)
        self.assertEqual(result.config["automation"]["interval_minutes"], 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_saving_does_not_mutate_the_input_mapping(self) -> None:
        original = {"categories": {"Documentos": ["PDF"], "Otros": []}}
        snapshot = json.loads(json.dumps(original))
        self.manager.save(original)
        self.assertEqual(original, snapshot)

    def test_unknown_category_is_reported_in_warnings(self) -> None:
        result = self.manager.save(
            {
                "categories": {"Documentos": [".pdf"], "Otros": []},
                "default_category": "Inexistente",
            }
        )
        self.assertIn("Inexistente", result["categories"])
        self.assertTrue(any("no existía" in warning for warning in self.manager.last_warnings))


class DefaultCategoryCoverageTests(unittest.TestCase):
    def test_all_required_categories_exist(self) -> None:
        required = {
            "Documentos",
            "Hojas_de_calculo",
            "Presentaciones",
            "Imagenes",
            "Videos",
            "Audio",
            "Comprimidos",
            "Instaladores",
            "Codigo",
            "Otros",
        }
        self.assertEqual(set(DEFAULT_CATEGORIES), required)


if __name__ == "__main__":
    unittest.main()
