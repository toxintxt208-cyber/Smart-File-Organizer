from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.safe_mover import (
    STATUS_DRY_RUN,
    STATUS_ERROR,
    STATUS_MOVED,
    STATUS_SKIPPED,
    find_available_destination,
    move_file_safely,
)


class SafeMoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sfo-mover-")
        self.base = Path(self.temp.name)
        self.source_dir = self.base / "origen"
        self.destination_dir = self.base / "origen" / "Documentos"
        self.source_dir.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_file(self, relative: str, content: bytes = b"contenido") -> Path:
        path = self.source_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_move_file_successfully_and_create_destination(self) -> None:
        source = self.make_file("informe.pdf", b"pdf-data")
        result = move_file_safely(
            source, self.destination_dir, allowed_roots=[self.source_dir]
        )
        self.assertEqual(result.status, STATUS_MOVED)
        self.assertFalse(source.exists())
        self.assertTrue((self.destination_dir / "informe.pdf").exists())
        self.assertEqual((self.destination_dir / "informe.pdf").read_bytes(), b"pdf-data")
        self.assertEqual(result.size, len(b"pdf-data"))

    def test_dry_run_does_not_create_or_move(self) -> None:
        source = self.make_file("informe.pdf", b"pdf-data")
        result = move_file_safely(
            source,
            self.destination_dir,
            dry_run=True,
            allowed_roots=[self.source_dir],
        )
        self.assertEqual(result.status, STATUS_DRY_RUN)
        self.assertTrue(source.exists())
        self.assertFalse(self.destination_dir.exists())
        self.assertEqual(result.destination, self.destination_dir / "informe.pdf")

    def test_existing_destination_is_never_overwritten(self) -> None:
        source = self.make_file("informe.pdf", b"nuevo")
        self.destination_dir.mkdir()
        existing = self.destination_dir / "informe.pdf"
        existing.write_bytes(b"antiguo")

        result = move_file_safely(
            source, self.destination_dir, allowed_roots=[self.source_dir]
        )
        self.assertEqual(result.status, STATUS_MOVED)
        self.assertTrue(result.renamed)
        self.assertEqual(result.destination, self.destination_dir / "informe_1.pdf")
        self.assertEqual(existing.read_bytes(), b"antiguo")
        self.assertEqual((self.destination_dir / "informe_1.pdf").read_bytes(), b"nuevo")

    def test_multiple_collisions_increment_suffix(self) -> None:
        self.destination_dir.mkdir()
        (self.destination_dir / "dato.txt").write_text("0")
        (self.destination_dir / "dato_1.txt").write_text("1")
        source = self.make_file("dato.txt", b"2")
        result = move_file_safely(source, self.destination_dir, allowed_roots=[self.source_dir])
        self.assertEqual(result.destination, self.destination_dir / "dato_2.txt")

    def test_compound_extension_is_preserved_on_rename(self) -> None:
        self.destination_dir.mkdir()
        (self.destination_dir / "respaldo.tar.gz").write_text("viejo")
        source = self.make_file("respaldo.tar.gz", b"nuevo")
        result = move_file_safely(source, self.destination_dir, allowed_roots=[self.source_dir])
        self.assertEqual(result.destination, self.destination_dir / "respaldo_1.tar.gz")

    def test_collision_policy_skip(self) -> None:
        source = self.make_file("informe.pdf", b"nuevo")
        self.destination_dir.mkdir()
        (self.destination_dir / "informe.pdf").write_bytes(b"antiguo")
        result = move_file_safely(
            source,
            self.destination_dir,
            collision_policy="skip",
            allowed_roots=[self.source_dir],
        )
        self.assertEqual(result.status, STATUS_SKIPPED)
        self.assertTrue(source.exists())
        self.assertEqual((self.destination_dir / "informe.pdf").read_bytes(), b"antiguo")

    def test_missing_source_returns_specific_error(self) -> None:
        result = move_file_safely(
            self.source_dir / "no-existe.pdf",
            self.destination_dir,
            allowed_roots=[self.source_dir],
        )
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertIn("origen", result.message.lower())
        self.assertIn("no existe", result.message.lower())
        self.assertFalse(self.destination_dir.exists())

    def test_destination_mkdir_failure_does_not_claim_source_disappeared(self) -> None:
        source = self.make_file("informe.pdf")
        with mock.patch(
            "pathlib.Path.mkdir",
            side_effect=FileNotFoundError("ruta de destino no disponible"),
        ):
            result = move_file_safely(
                source, self.destination_dir, allowed_roots=[self.source_dir]
            )
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertIn("carpeta de destino", result.message.lower())
        self.assertNotIn("archivo de origen ya no existe", result.message.lower())
        self.assertTrue(source.exists())

    def test_destination_mkdir_permission_error_is_specific(self) -> None:
        source = self.make_file("informe.pdf")
        with mock.patch(
            "pathlib.Path.mkdir",
            side_effect=PermissionError("acceso denegado"),
        ):
            result = move_file_safely(
                source, self.destination_dir, allowed_roots=[self.source_dir]
            )
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertIn("permiso denegado", result.message.lower())
        self.assertIn("carpeta de destino", result.message.lower())
        self.assertTrue(source.exists())

    def test_file_changed_during_operation_is_not_moved(self) -> None:
        source = self.make_file("informe.pdf", b"datos")
        real_stat = os.lstat(source)
        changed = mock.Mock(
            st_mode=real_stat.st_mode,
            st_size=real_stat.st_size + 10,
            st_mtime_ns=getattr(real_stat, "st_mtime_ns", 0) + 1,
        )
        with mock.patch("src.safe_mover.os.lstat", side_effect=[real_stat, changed]):
            result = move_file_safely(
                source, self.destination_dir, allowed_roots=[self.source_dir]
            )
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertIn("cambió", result.message.lower())
        self.assertTrue(source.exists())
        self.assertFalse((self.destination_dir / "informe.pdf").exists())

    def test_replace_file_not_found_reports_paths_and_cleans_placeholder(self) -> None:
        source = self.make_file("informe.pdf")
        with mock.patch(
            "src.safe_mover.os.replace",
            side_effect=FileNotFoundError("no encontrado"),
        ):
            result = move_file_safely(
                source, self.destination_dir, allowed_roots=[self.source_dir]
            )
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertIn(str(source), result.message)
        self.assertTrue(source.exists())
        self.assertFalse((self.destination_dir / "informe.pdf").exists())

    def test_directory_is_not_moved(self) -> None:
        directory = self.source_dir / "carpeta"
        directory.mkdir()
        result = move_file_safely(directory, self.destination_dir, allowed_roots=[self.source_dir])
        self.assertEqual(result.status, STATUS_SKIPPED)
        self.assertTrue(directory.is_dir())

    def test_same_directory_is_skipped(self) -> None:
        source = self.make_file("mismo.txt")
        result = move_file_safely(source, self.source_dir, allowed_roots=[self.source_dir])
        self.assertEqual(result.status, STATUS_SKIPPED)
        self.assertTrue(source.exists())

    def test_symlink_file_is_skipped(self) -> None:
        target = self.make_file("real.txt")
        link = self.source_dir / "enlace.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"No se pueden crear enlaces simbólicos: {exc}")
        result = move_file_safely(link, self.destination_dir, allowed_roots=[self.source_dir])
        self.assertEqual(result.status, STATUS_SKIPPED)
        self.assertTrue(link.exists())
        self.assertFalse((self.destination_dir / "enlace.txt").exists())

    def test_source_outside_allowed_roots_is_rejected(self) -> None:
        outside = self.base / "fuera"
        outside.mkdir()
        source = outside / "archivo.txt"
        source.write_text("x")
        result = move_file_safely(source, self.destination_dir, allowed_roots=[self.source_dir])
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertTrue(source.exists())

    def test_destination_outside_allowed_roots_is_rejected(self) -> None:
        source = self.make_file("archivo.txt")
        outside = self.base / "destino-fuera"
        result = move_file_safely(source, outside, allowed_roots=[self.source_dir])
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertTrue(source.exists())
        self.assertFalse(outside.exists())

    def test_destination_symlink_is_rejected(self) -> None:
        source = self.make_file("archivo.txt")
        outside = self.base / "fuera-destino"
        outside.mkdir()
        try:
            self.destination_dir.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"No se pueden crear enlaces simbólicos: {exc}")
        result = move_file_safely(source, self.destination_dir, allowed_roots=[self.source_dir])
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertTrue(source.exists())
        self.assertFalse((outside / "archivo.txt").exists())

    def test_permission_error_leaves_source_and_no_partial_file(self) -> None:
        source = self.make_file("bloqueado.pdf")
        with mock.patch(
            "src.safe_mover.os.replace",
            side_effect=PermissionError("acceso denegado"),
        ):
            result = move_file_safely(
                source, self.destination_dir, allowed_roots=[self.source_dir]
            )
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertTrue(source.exists())
        self.assertFalse((self.destination_dir / "bloqueado.pdf").exists())
        self.assertIn("Permiso denegado", result.message)

    def test_cross_device_fallback_uses_copy_and_delete(self) -> None:
        source = self.make_file("cruzado.pdf", b"datos")
        with mock.patch(
            "src.safe_mover.os.replace",
            side_effect=OSError(errno.EXDEV, "cross-device"),
        ):
            result = move_file_safely(
                source, self.destination_dir, allowed_roots=[self.source_dir]
            )
        self.assertEqual(result.status, STATUS_MOVED)
        self.assertFalse(source.exists())
        self.assertEqual((self.destination_dir / "cruzado.pdf").read_bytes(), b"datos")

    def test_unicode_and_spaces(self) -> None:
        source = self.make_file("Informe año final (v2).PDF", b"x")
        result = move_file_safely(source, self.destination_dir, allowed_roots=[self.source_dir])
        self.assertEqual(result.status, STATUS_MOVED)
        self.assertTrue((self.destination_dir / "Informe año final (v2).PDF").exists())

    def test_target_name_cannot_escape_destination(self) -> None:
        source = self.make_file("archivo.txt")
        result = move_file_safely(
            source,
            self.destination_dir,
            target_name="../fuera.txt",
            allowed_roots=[self.source_dir],
        )
        self.assertEqual(result.status, STATUS_ERROR)
        self.assertTrue(source.exists())

    def test_find_available_destination_with_reserved_preview(self) -> None:
        reserved: set[str] = set()
        first = find_available_destination(self.destination_dir, "archivo.pdf", reserved=reserved)
        reserved.add(os.path.normcase(str(first)))
        second = find_available_destination(self.destination_dir, "archivo.pdf", reserved=reserved)
        self.assertEqual(first.name, "archivo.pdf")
        self.assertEqual(second.name, "archivo_1.pdf")

    def test_rename_inside_same_directory_is_skipped(self) -> None:
        source = self.make_file("original.txt")
        result = move_file_safely(
            source,
            self.source_dir,
            target_name="nuevo.txt",
            allowed_roots=[self.source_dir],
        )
        self.assertEqual(result.status, STATUS_SKIPPED)
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
