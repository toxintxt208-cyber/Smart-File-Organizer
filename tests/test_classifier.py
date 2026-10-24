from __future__ import annotations

import unittest
from pathlib import Path

from src.config_manager import DEFAULT_CATEGORIES
from src.file_classifier import FileClassifier


class FileClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = FileClassifier(DEFAULT_CATEGORIES, "Otros")

    def test_default_rules_for_every_extension(self) -> None:
        for category, extensions in DEFAULT_CATEGORIES.items():
            for extension in extensions:
                with self.subTest(category=category, extension=extension):
                    self.assertEqual(
                        self.classifier.classify(Path(f"archivo{extension}")),
                        category,
                    )

    def test_uppercase_and_mixed_case_extensions(self) -> None:
        self.assertEqual(self.classifier.classify("REPORTE.PDF"), "Documentos")
        self.assertEqual(self.classifier.classify("foto.JpG"), "Imagenes")
        self.assertEqual(self.classifier.classify("video.MKV"), "Videos")
        self.assertEqual(self.classifier.classify("datos.XLSX"), "Hojas_de_calculo")

    def test_file_without_extension_goes_to_default(self) -> None:
        self.assertEqual(self.classifier.classify("README"), "Otros")
        self.assertEqual(self.classifier.classify(Path("archivo.")), "Otros")

    def test_unknown_extension_goes_to_default(self) -> None:
        self.assertEqual(self.classifier.classify("archivo.zzz"), "Otros")
        self.assertEqual(self.classifier.get_extension("archivo.zzz"), ".zzz")

    def test_unicode_and_spaces(self) -> None:
        self.assertEqual(self.classifier.classify("Informe año 2026 (final).pdf"), "Documentos")
        self.assertEqual(self.classifier.classify("Ñandú y niños.PNG"), "Imagenes")

    def test_custom_rules_and_compound_extensions(self) -> None:
        classifier = FileClassifier(
            {
                "Paquetes": [".tar.gz"],
                "Comprimidos": [".gz", ".zip"],
                "Otros": [],
            },
            "Otros",
        )
        self.assertEqual(classifier.classify("respaldo.tar.gz"), "Paquetes")
        self.assertEqual(classifier.classify("respaldo.ZIP"), "Comprimidos")
        self.assertEqual(classifier.get_extension("respaldo.tar.gz"), ".tar.gz")

    def test_custom_extension_without_leading_dot(self) -> None:
        classifier = FileClassifier({"Libros": ["EPUB", ".PDF"], "Otros": []}, "Otros")
        self.assertEqual(classifier.classify("libro.epub"), "Libros")
        self.assertEqual(classifier.classify("libro.pdf"), "Libros")

    def test_default_category_is_created_if_missing(self) -> None:
        classifier = FileClassifier({"Documentos": [".pdf"]}, "Otros")
        self.assertIn("Otros", classifier.categories)
        self.assertEqual(classifier.classify("sin_extension"), "Otros")

    def test_duplicate_extension_keeps_first_category(self) -> None:
        classifier = FileClassifier(
            {"Primero": [".abc"], "Segundo": [".ABC"], "Otros": []}, "Otros"
        )
        self.assertEqual(classifier.classify("archivo.abc"), "Primero")

    def test_empty_categories_raise_value_error(self) -> None:
        with self.assertRaises(ValueError):
            FileClassifier({})

    def test_rules_copy(self) -> None:
        rules = self.classifier.rules()
        rules["Documentos"].append(".mutado")
        self.assertNotIn(".mutado", self.classifier.rules()["Documentos"])


if __name__ == "__main__":
    unittest.main()
