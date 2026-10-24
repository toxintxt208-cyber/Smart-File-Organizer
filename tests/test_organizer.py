from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from src.config_manager import ConfigManager
from src.history_manager import HistoryManager
from src.locking import FileLock, LockBusyError
from src.organizer import PLAN_ERROR, PLAN_READY, PLAN_SKIPPED, Organizer
from src.safe_mover import STATUS_DRY_RUN, STATUS_MOVED


class OrganizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sfo-organizer-")
        self.base = Path(self.temp.name)
        self.root = self.base / "origen"
        self.root.mkdir()
        self.history_path = self.base / "history.json"
        self.lock_path = self.base / "organizer.lock"
        self.categories = {
            "Documentos": [".pdf", ".docx"],
            "Imagenes": [".png", ".jpg"],
            "Comprimidos": [".zip"],
            "Otros": [],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_organizer(
        self,
        *,
        categories: dict[str, list[str]] | None = None,
        sources: list[Path] | None = None,
        safety: dict[str, bool] | None = None,
    ) -> Organizer:
        config = {
            "categories": categories or self.categories,
            "default_category": "Otros",
            "sources": [
                {"name": f"Origen {index}", "path": str(path), "enabled": True}
                for index, path in enumerate(sources or [self.root])
            ],
            "safety": {
                "skip_hidden": True,
                "skip_system": True,
                **(safety or {}),
            },
            "history": {"enabled": True, "max_operations": 100},
        }
        history = HistoryManager(
            self.history_path,
            max_operations=100,
            lock_path=self.lock_path,
        )
        return Organizer(config, history=history, lock_path=self.lock_path)

    def make_file(self, name: str, content: bytes = b"datos", root: Path | None = None) -> Path:
        path = (root or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    # ------------------------------------------------------------------ análisis
    def test_scan_classifies_top_level_files(self) -> None:
        pdf = self.make_file("informe.pdf")
        png = self.make_file("foto.PNG")
        without_extension = self.make_file("LEEME")
        organizer = self.make_organizer()

        report = organizer.scan(self.root)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.total, 3)
        self.assertEqual(report.to_move, 3)
        self.assertEqual(report.skipped, 0)
        self.assertFalse(report.cancelled)
        self.assertEqual(report.root, self.root.resolve())

        by_name = {plan.name: plan for plan in report.plans}
        self.assertEqual(by_name["informe.pdf"].category, "Documentos")
        self.assertEqual(by_name["informe.pdf"].destination, self.root.resolve() / "Documentos" / "informe.pdf")
        self.assertEqual(by_name["foto.PNG"].category, "Imagenes")
        self.assertEqual(by_name["LEEME"].category, "Otros")
        self.assertEqual(by_name["informe.pdf"].status, PLAN_READY)
        self.assertEqual(pdf.read_bytes(), b"datos")
        self.assertFalse((self.root / "Documentos").exists())
        self.assertTrue(png.exists())
        self.assertTrue(without_extension.exists())

    def test_scan_does_not_recurse(self) -> None:
        self.make_file("superior.pdf")
        nested_dir = self.root / "subcarpeta"
        nested_dir.mkdir()
        nested = nested_dir / "interno.pdf"
        nested.write_text("no debe moverse")

        report = self.make_organizer().scan(self.root)
        self.assertEqual(report.total, 1)
        self.assertEqual(report.ignored_directories, 1)
        self.assertEqual(report.plans[0].name, "superior.pdf")
        self.assertFalse(any(plan.name == "interno.pdf" for plan in report.plans))

    def test_scan_missing_source_reports_error(self) -> None:
        report = self.make_organizer().scan(self.base / "no-existe")
        self.assertTrue(report.errors)
        self.assertEqual(report.total, 0)
        self.assertIn("no existe", report.errors[0])

    def test_scan_file_instead_of_directory_reports_error(self) -> None:
        file_path = self.make_file("archivo.pdf")
        report = self.make_organizer().scan(file_path)
        self.assertTrue(report.errors)
        self.assertIn("no es una carpeta", report.errors[0])

    def test_hidden_dot_file_is_skipped(self) -> None:
        hidden = self.make_file(".oculto.pdf")
        visible = self.make_file("visible.pdf")
        report = self.make_organizer().scan(self.root)
        by_name = {plan.name: plan for plan in report.plans}
        self.assertEqual(by_name[".oculto.pdf"].status, PLAN_SKIPPED)
        self.assertIn("oculto", by_name[".oculto.pdf"].message)
        self.assertEqual(by_name["visible.pdf"].status, PLAN_READY)
        result = self.make_organizer().organize_sources([self.root])
        self.assertEqual(result.moved, 1)
        self.assertTrue(hidden.exists())
        self.assertFalse(visible.exists())

    def test_hidden_files_can_be_included_by_configuration(self) -> None:
        hidden = self.make_file(".oculto.pdf")
        organizer = self.make_organizer(safety={"skip_hidden": False})
        report = organizer.scan(self.root)
        self.assertEqual(report.plans[0].status, PLAN_READY)
        result = organizer.organize_sources([self.root])
        self.assertEqual(result.moved, 1)
        self.assertTrue((self.root / "Documentos" / ".oculto.pdf").exists())
        self.assertFalse(hidden.exists())

    def test_symlink_is_skipped_and_not_followed(self) -> None:
        target = self.make_file("real.pdf")
        link = self.root / "enlace.pdf"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"No se pueden crear enlaces simbólicos: {exc}")
        report = self.make_organizer().scan(self.root)
        by_name = {plan.name: plan for plan in report.plans}
        self.assertEqual(by_name["enlace.pdf"].status, PLAN_SKIPPED)
        self.assertIn("regular", by_name["enlace.pdf"].message)
        result = self.make_organizer().organize_sources([self.root])
        self.assertEqual(result.moved, 1)
        self.assertTrue(link.is_symlink())
        self.assertTrue(target.exists())

    def test_destination_path_that_is_a_file_is_an_error(self) -> None:
        pdf = self.make_file("informe.pdf")
        blocker = self.root / "Documentos"
        blocker.write_text("bloqueo")
        report = self.make_organizer().scan(self.root)
        plan = next(plan for plan in report.plans if plan.name == "informe.pdf")
        self.assertEqual(plan.status, PLAN_ERROR)
        self.assertIn("no es una carpeta", plan.message)

        result = self.make_organizer().organize_sources([self.root])
        # El archivo que ocupa el nombre de la carpeta es un archivo normal del
        # primer nivel, por lo que se clasifica como «Otros»; el PDF no se mueve.
        self.assertTrue(result.errors)
        self.assertTrue(pdf.exists())
        self.assertFalse((self.root / "Documentos").is_file())
        self.assertTrue((self.root / "Otros" / "Documentos").exists())

    # ------------------------------------------------------------------ dry run
    def test_dry_run_changes_nothing(self) -> None:
        self.make_file("informe.pdf")
        self.make_file("foto.png")
        self.make_file("sin_extension")
        organizer = self.make_organizer()

        result = organizer.organize_sources([self.root], dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.planned, 3)
        self.assertEqual(result.moved, 0)
        self.assertEqual(result.errors, 0)
        self.assertEqual(len(result.results), 3)
        self.assertTrue(all(item.status == STATUS_DRY_RUN for item in result.results))
        self.assertFalse(result.history_recorded)
        self.assertFalse((self.root / "Documentos").exists())
        self.assertFalse((self.root / "Imagenes").exists())
        self.assertFalse((self.root / "Otros").exists())
        self.assertTrue((self.root / "informe.pdf").exists())
        self.assertFalse(self.history_path.exists())

    # ---------------------------------------------------------------- ejecución
    def test_organize_moves_files_into_categories(self) -> None:
        pdf = self.make_file("informe.pdf", b"pdf")
        png = self.make_file("foto.png", b"png")
        other = self.make_file("notas.zzz", b"zzz")

        result = self.make_organizer().organize_sources([self.root])
        self.assertFalse(result.dry_run)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.moved, 3)
        self.assertEqual(result.errors, 0)
        self.assertEqual(result.skipped, 0)
        self.assertTrue(result.history_recorded)
        self.assertTrue(result.operation_id)

        self.assertFalse(pdf.exists())
        self.assertFalse(png.exists())
        self.assertFalse(other.exists())
        self.assertEqual((self.root / "Documentos" / "informe.pdf").read_bytes(), b"pdf")
        self.assertEqual((self.root / "Imagenes" / "foto.png").read_bytes(), b"png")
        self.assertEqual((self.root / "Otros" / "notas.zzz").read_bytes(), b"zzz")
        self.assertFalse(self.lock_path.exists())

    def test_history_records_operation(self) -> None:
        self.make_file("informe.pdf")
        organizer = self.make_organizer()
        result = organizer.organize_sources([self.root])

        operations = organizer.history.get_operations()
        self.assertEqual(len(operations), 1)
        operation = operations[0]
        self.assertEqual(operation["id"], result.operation_id)
        self.assertEqual(operation["kind"], "organize")
        self.assertEqual(operation["roots"], [str(self.root.resolve())])
        self.assertEqual(len(operation["entries"]), 1)
        self.assertEqual(operation["entries"][0]["status"], STATUS_MOVED)
        self.assertEqual(operation["entries"][0]["category"], "Documentos")
        self.assertFalse(operation["cancelled"])

    def test_existing_destination_is_not_overwritten(self) -> None:
        existing_dir = self.root / "Documentos"
        existing_dir.mkdir()
        existing = existing_dir / "informe.pdf"
        existing.write_bytes(b"antiguo")
        source = self.make_file("informe.pdf", b"nuevo")

        result = self.make_organizer().organize_sources([self.root])
        self.assertEqual(result.moved, 1)
        self.assertEqual(existing.read_bytes(), b"antiguo")
        self.assertEqual((existing_dir / "informe_1.pdf").read_bytes(), b"nuevo")
        self.assertFalse(source.exists())

    def test_multiple_sources_are_organized_independently(self) -> None:
        second_root = self.base / "segundo origen"
        second_root.mkdir()
        self.make_file("a.pdf", root=self.root)
        self.make_file("b.png", root=second_root)

        organizer = self.make_organizer(sources=[self.root, second_root])
        result = organizer.organize_sources()
        self.assertEqual(result.moved, 2)
        self.assertEqual(len(result.scans), 2)
        self.assertTrue((self.root / "Documentos" / "a.pdf").exists())
        self.assertTrue((second_root / "Imagenes" / "b.png").exists())

    def test_missing_source_is_reported_without_crashing(self) -> None:
        result = self.make_organizer().organize_sources([self.base / "no-existe"])
        self.assertEqual(result.moved, 0)
        self.assertTrue(result.errors)
        self.assertTrue(result.scans[0].errors)
        self.assertFalse(result.history_recorded)

    def test_empty_source_does_nothing(self) -> None:
        result = self.make_organizer().organize_sources([self.root])
        self.assertEqual(result.moved, 0)
        self.assertEqual(result.errors, 0)
        self.assertFalse(result.history_recorded)

    def test_pre_set_stop_event_cancels_without_moving(self) -> None:
        source = self.make_file("informe.pdf")
        stop_event = threading.Event()
        stop_event.set()

        result = self.make_organizer().organize_sources([self.root], stop_event=stop_event)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.moved, 0)
        self.assertTrue(source.exists())
        self.assertFalse((self.root / "Documentos").exists())

    def test_stop_event_during_operation_is_cooperative(self) -> None:
        first = self.make_file("a.pdf")
        second = self.make_file("b.pdf")
        stop_event = threading.Event()
        organizer = self.make_organizer()

        def progress(done: int, total: int, name: str) -> None:
            if done == 1:
                stop_event.set()

        result = organizer.organize_sources(
            [self.root], stop_event=stop_event, progress=progress
        )
        self.assertTrue(result.cancelled)
        self.assertEqual(result.moved, 1)
        moved_names = {item.source.name for item in result.results}
        self.assertEqual(len(moved_names), 1)
        remaining = [path for path in (first, second) if path.exists()]
        self.assertEqual(len(remaining), 1)
        operations = organizer.history.get_operations()
        self.assertEqual(len(operations), 1)
        self.assertTrue(operations[0]["cancelled"])

    def test_lock_prevents_simultaneous_organization(self) -> None:
        self.make_file("informe.pdf")
        lock = FileLock(self.lock_path)
        lock.acquire()
        try:
            with self.assertRaises(LockBusyError):
                self.make_organizer().organize_sources([self.root])
        finally:
            lock.release()
        self.assertTrue((self.root / "informe.pdf").exists())

    def test_dry_run_does_not_acquire_lock(self) -> None:
        self.make_file("informe.pdf")
        lock = FileLock(self.lock_path)
        lock.acquire()
        try:
            result = self.make_organizer().organize_sources([self.root], dry_run=True)
            self.assertEqual(result.planned, 1)
            self.assertEqual(result.moved, 0)
        finally:
            lock.release()

    def test_custom_categories_and_extensionless_files(self) -> None:
        categories = {
            "Proyectos": [".foo"],
            "Documentos": [".pdf"],
            "Otros": [],
        }
        foo = self.make_file("modelo.foo")
        plain = self.make_file("LEEME")
        result = self.make_organizer(categories=categories).organize_sources([self.root])
        self.assertEqual(result.moved, 2)
        self.assertTrue((self.root / "Proyectos" / "modelo.foo").exists())
        self.assertTrue((self.root / "Otros" / "LEEME").exists())
        self.assertFalse(foo.exists())
        self.assertFalse(plain.exists())

    def test_unicode_and_spaces_in_names(self) -> None:
        name = "Ñandú año (final) v2.PDF"
        source = self.make_file(name, b"contenido")
        result = self.make_organizer().organize_sources([self.root])
        self.assertEqual(result.moved, 1)
        moved = self.root / "Documentos" / name
        self.assertTrue(moved.exists())
        self.assertEqual(moved.read_bytes(), b"contenido")
        self.assertFalse(source.exists())

    def test_organize_source_alias(self) -> None:
        self.make_file("informe.pdf")
        result = self.make_organizer().organize_source(self.root)
        self.assertEqual(result.moved, 1)
        self.assertTrue((self.root / "Documentos" / "informe.pdf").exists())

    def test_history_disabled_still_moves_files(self) -> None:
        config = {
            "categories": self.categories,
            "default_category": "Otros",
            "sources": [],
            "safety": {},
            "history": {"enabled": False, "max_operations": 10},
        }
        organizer = Organizer(config, lock_path=self.lock_path)
        self.make_file("informe.pdf")
        result = organizer.organize_sources([self.root])
        self.assertEqual(result.moved, 1)
        self.assertFalse(result.history_recorded)
        self.assertFalse(self.history_path.exists())

    def test_config_manager_integration(self) -> None:
        config_path = self.base / "config.json"
        manager = ConfigManager(config_path)
        config = manager.save(
            {
                "sources": [{"name": "Origen", "path": str(self.root)}],
                "categories": self.categories,
                "default_category": "Otros",
            }
        )
        history = HistoryManager(
            self.history_path,
            max_operations=10,
            lock_path=self.lock_path,
        )
        organizer = Organizer(config, history=history, lock_path=self.lock_path)
        self.make_file("informe.pdf")
        result = organizer.organize_sources()
        self.assertEqual(result.moved, 1)
        self.assertTrue((self.root / "Documentos" / "informe.pdf").exists())

    # ------------------------------------------ regresiones del error reportado
    def test_scan_classifies_pdf_jpg_zip(self) -> None:
        pdf = self.make_file("informe.pdf", b"pdf")
        jpg = self.make_file("foto.JPG", b"jpg")
        zip_file = self.make_file("paquete.zip", b"zip")

        report = self.make_organizer().scan(self.root)

        self.assertEqual(report.errors, [])
        self.assertEqual(report.total, 3)
        self.assertEqual(report.to_move, 3)
        by_name = {plan.name: plan for plan in report.plans}
        self.assertEqual(by_name["informe.pdf"].category, "Documentos")
        self.assertEqual(by_name["foto.JPG"].category, "Imagenes")
        self.assertEqual(by_name["paquete.zip"].category, "Comprimidos")
        for path in (pdf, jpg, zip_file):
            self.assertTrue(path.exists())
        for category in ("Documentos", "Imagenes", "Comprimidos"):
            self.assertFalse((self.root / category).exists())

    def test_scan_empty_folder_reports_no_errors_and_changes_nothing(self) -> None:
        organizer = self.make_organizer()
        before = sorted(self.root.iterdir())

        report = organizer.scan(self.root)
        result = organizer.organize_sources([self.root])

        self.assertEqual(report.total, 0)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.to_move, 0)
        self.assertEqual(result.moved, 0)
        self.assertEqual(result.errors, 0)
        self.assertFalse(result.history_recorded)
        self.assertEqual(sorted(self.root.iterdir()), before)
        self.assertFalse((self.root / "Documentos").exists())

    def test_analysis_never_creates_destination_directories(self) -> None:
        pdf = self.make_file("informe.pdf")
        organizer = self.make_organizer()
        report = organizer.scan(self.root)
        self.assertEqual(report.to_move, 1)
        self.assertFalse((self.root / "Documentos").exists())
        self.assertTrue(pdf.exists())
        # El análisis puede repetirse sin efectos acumulativos.
        report2 = organizer.scan(self.root)
        self.assertEqual(report2.to_move, 1)
        self.assertFalse((self.root / "Documentos").exists())

    def test_organize_creates_missing_destination_directories(self) -> None:
        self.make_file("informe.pdf")
        self.assertFalse((self.root / "Documentos").exists())
        result = self.make_organizer().organize_sources([self.root])
        self.assertEqual(result.errors, 0)
        self.assertEqual(result.moved, 1)
        self.assertTrue((self.root / "Documentos").is_dir())
        self.assertTrue((self.root / "Documentos" / "informe.pdf").is_file())

    def test_windows_path_with_spaces_and_special_characters(self) -> None:
        special_root = self.base / "carpeta con espacios & símbolos [x] #1"
        special_root.mkdir()
        names = {
            "informe final (v2) #1.pdf": "Documentos",
            "foto ñandú & copia.JPG": "Imagenes",
            "paquete [copia] & final.ZIP": "Comprimidos",
        }
        for name in names:
            self.make_file(name, b"contenido", root=special_root)

        organizer = self.make_organizer(sources=[special_root])
        report = organizer.scan(special_root)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.to_move, 3)
        self.assertEqual(
            {plan.name: plan.category for plan in report.plans}, names
        )
        self.assertFalse((special_root / "Documentos").exists())

        result = organizer.organize_sources([special_root])
        self.assertEqual(result.errors, 0)
        self.assertEqual(result.moved, 3)
        for name, category in names.items():
            self.assertFalse((special_root / name).exists())
            self.assertTrue((special_root / category / name).is_file())

    def test_file_disappearing_during_processing_does_not_abort_batch(self) -> None:
        first = self.make_file("a.pdf", b"a")
        disappearing = self.make_file("b.pdf", b"b")
        last = self.make_file("c.png", b"c")
        organizer = self.make_organizer()

        def progress(done: int, total: int, name: str) -> None:
            if done == 1:
                disappearing.unlink()

        result = organizer.organize_sources([self.root], progress=progress)

        self.assertEqual(result.moved, 2)
        self.assertEqual(result.errors, 1)
        self.assertFalse(first.exists())
        self.assertFalse(disappearing.exists())
        self.assertFalse(last.exists())
        self.assertTrue((self.root / "Documentos" / "a.pdf").exists())
        self.assertTrue((self.root / "Imagenes" / "c.png").exists())
        messages = " ".join(result.all_errors).lower()
        self.assertTrue("desapareció" in messages or "no existe" in messages)

    def test_multiple_collisions_never_overwrite_any_version(self) -> None:
        destination = self.root / "Documentos"
        destination.mkdir()
        (destination / "informe.pdf").write_bytes(b"version-0")
        (destination / "informe_1.pdf").write_bytes(b"version-1")
        source = self.make_file("informe.pdf", b"version-nueva")

        result = self.make_organizer().organize_sources([self.root])

        self.assertEqual(result.errors, 0)
        self.assertEqual(result.moved, 1)
        self.assertTrue((destination / "informe.pdf").read_bytes() == b"version-0")
        self.assertTrue((destination / "informe_1.pdf").read_bytes() == b"version-1")
        self.assertTrue((destination / "informe_2.pdf").read_bytes() == b"version-nueva")
        self.assertFalse(source.exists())

    def test_organize_multiple_files_and_categories_in_one_run(self) -> None:
        self.make_file("documento.pdf", b"pdf")
        self.make_file("imagen.jpg", b"jpg")
        self.make_file("comprimido.zip", b"zip")
        self.make_file("sin_extension", b"otros")

        result = self.make_organizer().organize_sources([self.root])

        self.assertEqual(result.errors, 0)
        self.assertEqual(result.moved, 4)
        self.assertEqual(result.scans[0].total, 4)
        self.assertTrue((self.root / "Documentos" / "documento.pdf").exists())
        self.assertTrue((self.root / "Imagenes" / "imagen.jpg").exists())
        self.assertTrue((self.root / "Comprimidos" / "comprimido.zip").exists())
        self.assertTrue((self.root / "Otros" / "sin_extension").exists())

    def test_dry_run_never_touches_files_or_directories(self) -> None:
        pdf = self.make_file("informe.pdf", b"pdf")
        jpg = self.make_file("foto.jpg", b"jpg")
        organizer = self.make_organizer()
        before = {path.name for path in self.root.iterdir()}

        result = organizer.organize_sources([self.root], dry_run=True)

        self.assertTrue(result.dry_run)
        self.assertEqual(result.moved, 0)
        self.assertEqual(result.planned, 2)
        self.assertEqual(result.errors, 0)
        self.assertEqual({path.name for path in self.root.iterdir()}, before)
        self.assertTrue(pdf.exists())
        self.assertTrue(jpg.exists())
        self.assertFalse((self.root / "Documentos").exists())
        self.assertFalse((self.root / "Imagenes").exists())
        self.assertFalse(self.history_path.exists())


if __name__ == "__main__":
    unittest.main()
