from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.history_manager import HistoryManager
from src.locking import FileLock, LockBusyError
from src.safe_mover import STATUS_MOVED, move_file_safely


class HistoryManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sfo-history-")
        self.base = Path(self.temp.name)
        self.root = self.base / "origen"
        self.root.mkdir()
        self.history_path = self.base / "history.json"
        self.lock_path = self.base / "organizer.lock"
        self.history = HistoryManager(
            self.history_path,
            max_operations=50,
            lock_path=self.lock_path,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_file(self, name: str, content: bytes = b"datos") -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def move_and_record(
        self,
        name: str = "informe.pdf",
        category: str = "Documentos",
        content: bytes = b"datos",
    ) -> tuple[Path, Path, str]:
        source = self.make_file(name, content)
        destination_dir = self.root / category
        result = move_file_safely(
            source,
            destination_dir,
            allowed_roots=[self.root],
        )
        self.assertEqual(result.status, STATUS_MOVED)
        assert result.destination is not None
        operation_id = self.history.record_operation(
            kind="organize",
            roots=[self.root],
            entries=[
                {
                    "source": str(source),
                    "destination": str(result.destination),
                    "size": len(content),
                    "status": result.status,
                    "category": category,
                }
            ],
        )
        return source, result.destination, operation_id

    # ------------------------------------------------------------------ registro
    def test_record_and_list_operations(self) -> None:
        source = self.make_file("a.pdf")
        operation_id = self.history.record_operation(
            roots=[self.root],
            entries=[{"source": str(source), "destination": "", "size": 4, "status": "error"}],
        )
        self.assertTrue(operation_id)

        operations = self.history.get_operations()
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["id"], operation_id)
        self.assertEqual(operations[0]["kind"], "organize")
        self.assertEqual(operations[0]["roots"], [str(self.root)])

        operation = self.history.get_operation(operation_id)
        self.assertIsNotNone(operation)
        self.assertEqual(len(operation["entries"]), 1)
        self.assertEqual(operation["entries"][0]["status"], "error")

    def test_record_without_entries_is_not_persisted(self) -> None:
        operation_id = self.history.record_operation(entries=[])
        self.assertEqual(operation_id, "")
        self.assertFalse(self.history_path.exists())

    def test_operations_are_listed_newest_first(self) -> None:
        first = self.history.record_operation(entries=[{"source": "a.pdf"}])
        second = self.history.record_operation(entries=[{"source": "b.pdf"}])
        operations = self.history.get_operations()
        self.assertEqual([operation["id"] for operation in operations], [second, first])
        self.assertEqual(len(self.history.get_operations(limit=1)), 1)

    def test_max_operations_is_enforced(self) -> None:
        manager = HistoryManager(
            self.history_path,
            max_operations=2,
            lock_path=self.lock_path,
        )
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            manager.record_operation(entries=[{"source": name}])
        operations = manager.get_operations()
        self.assertEqual(len(operations), 2)
        self.assertEqual([operation["entries"][0]["source"] for operation in operations], ["c.pdf", "b.pdf"])

    def test_statistics(self) -> None:
        self.move_and_record("a.pdf")
        self.move_and_record("b.png", category="Imagenes")
        stats = self.history.get_statistics()
        self.assertEqual(stats["operations"], 2)
        self.assertEqual(stats["files_moved"], 2)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["restored"], 0)
        self.assertEqual(stats["restore_operations"], 0)
        self.assertEqual(stats["history_path"], str(self.history_path))

    def test_corrupt_history_is_backed_up(self) -> None:
        self.history_path.write_text("{history roto", encoding="utf-8")
        operations = self.history.get_operations()
        self.assertEqual(operations, [])
        self.assertTrue(self.history.recovered)
        self.assertIsNotNone(self.history.last_error)
        backups = list(self.base.glob("history.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("history roto", backups[0].read_text(encoding="utf-8"))

    def test_invalid_history_shape_is_recovered(self) -> None:
        self.history_path.write_text(
            json.dumps({"operations": "no es una lista"}), encoding="utf-8"
        )
        self.assertEqual(self.history.load()["operations"], [])
        self.assertTrue(self.history.recovered)
        self.assertTrue(list(self.base.glob("history.json.corrupt-*")))

    # ----------------------------------------------------------------- restauración
    def test_plan_and_undo_restores_file(self) -> None:
        source, destination, operation_id = self.move_and_record()
        plan = self.history.plan_undo()
        self.assertEqual(plan.operation_id, operation_id)
        self.assertEqual(plan.total, 1)
        self.assertEqual(plan.ready, 1)
        self.assertEqual(plan.conflicts, 0)
        self.assertEqual(plan.actions[0].original, source)
        self.assertEqual(plan.actions[0].destination, destination)

        report = self.history.undo_last_operation("skip")
        self.assertEqual(report.operation_id, operation_id)
        self.assertEqual(report.restored_count, 1)
        self.assertEqual(report.skipped, [])
        self.assertEqual(report.errors, [])
        self.assertTrue(source.exists())
        self.assertFalse(destination.exists())
        self.assertEqual(source.read_bytes(), b"datos")

        operation = self.history.get_operation(operation_id)
        self.assertIsNotNone(operation)
        self.assertTrue(operation["undone_at"])
        self.assertTrue(operation["entries"][0]["restored_at"])
        self.assertIsNone(self.history.get_last_undoable_operation())

    def test_undo_records_a_restore_operation(self) -> None:
        self.move_and_record()
        self.history.undo_last_operation("skip")
        operations = self.history.get_operations()
        self.assertEqual(operations[0]["kind"], "restore")
        self.assertEqual(operations[0]["entries"][0]["status"], "moved")
        self.assertEqual(operations[0]["entries"][0]["category"], "Restauración")
        stats = self.history.get_statistics()
        self.assertEqual(stats["restore_operations"], 1)
        self.assertEqual(stats["restored"], 1)

    def test_collision_skip_does_not_overwrite_or_mark_undone(self) -> None:
        source, destination, operation_id = self.move_and_record()
        source.write_bytes(b"archivo-ocupado")

        plan = self.history.plan_undo()
        self.assertEqual(plan.conflicts, 1)
        report = self.history.undo_last_operation("skip")

        self.assertEqual(report.restored_count, 0)
        self.assertEqual(len(report.skipped), 1)
        self.assertEqual(source.read_bytes(), b"archivo-ocupado")
        self.assertTrue(destination.exists())
        operation = self.history.get_operation(operation_id)
        self.assertIsNotNone(operation)
        self.assertFalse(operation["undone_at"])
        self.assertEqual(self.history.get_last_undoable_operation()["id"], operation_id)

    def test_collision_rename_restores_with_alternative_name(self) -> None:
        source, destination, operation_id = self.move_and_record()
        source.write_bytes(b"archivo-ocupado")

        report = self.history.undo_last_operation("rename")
        self.assertEqual(report.restored_count, 1)
        self.assertEqual(source.read_bytes(), b"archivo-ocupado")
        restored = self.root / "informe_1.pdf"
        self.assertTrue(restored.exists())
        self.assertEqual(restored.read_bytes(), b"datos")
        self.assertFalse(destination.exists())

        operation = self.history.get_operation(operation_id)
        self.assertIsNotNone(operation)
        self.assertTrue(operation["undone_at"])
        self.assertEqual(operation["entries"][0]["restore_destination"], str(restored))

    def test_missing_destination_is_skipped_without_error(self) -> None:
        _, destination, operation_id = self.move_and_record()
        destination.unlink()

        plan = self.history.plan_undo()
        self.assertEqual(plan.missing, 1)
        report = self.history.undo_last_operation("skip")
        self.assertEqual(report.restored_count, 0)
        self.assertEqual(len(report.skipped), 1)
        self.assertEqual(report.errors, [])
        operation = self.history.get_operation(operation_id)
        self.assertIsNotNone(operation)
        self.assertFalse(operation["undone_at"])

    def test_missing_original_parent_is_not_restored(self) -> None:
        _, destination, operation_id = self.move_and_record()
        destination.unlink()
        (self.root / "Documentos").rmdir()
        self.root.rmdir()
        plan = self.history.plan_undo(operation_id)
        self.assertEqual(plan.missing, 1)
        report = self.history.undo_last_operation(operation_id=operation_id)
        self.assertEqual(report.restored_count, 0)

    def test_entries_outside_registered_roots_are_invalid(self) -> None:
        outside = self.base / "fuera"
        outside.mkdir()
        source = outside / "ajeno.pdf"
        source.write_text("x")
        operation_id = self.history.record_operation(
            roots=[self.root],
            entries=[
                {
                    "source": str(source),
                    "destination": str(self.root / "Documentos" / "ajeno.pdf"),
                    "status": "moved",
                }
            ],
        )
        plan = self.history.plan_undo(operation_id)
        self.assertEqual(plan.actions[0].status, "invalid")
        report = self.history.undo_last_operation("skip", operation_id=operation_id)
        self.assertEqual(report.restored_count, 0)
        self.assertTrue(source.exists())

    def test_no_undoable_operation(self) -> None:
        plan = self.history.plan_undo()
        self.assertIsNone(plan.operation_id)
        self.assertTrue(plan.message)
        report = self.history.undo_last_operation("skip")
        self.assertIsNone(report.operation_id)
        self.assertEqual(report.restored_count, 0)

    def test_invalid_collision_policy_raises(self) -> None:
        self.move_and_record()
        with self.assertRaises(ValueError):
            self.history.undo_last_operation("sobrescribir")

    def test_ask_policy_can_cancel(self) -> None:
        source, _, _ = self.move_and_record()
        source.write_bytes(b"ocupado")
        asked = []

        def on_collision(action: object) -> str:
            asked.append(action)
            return "cancel"

        report = self.history.undo_last_operation("ask", on_collision=on_collision)
        self.assertTrue(report.cancelled)
        self.assertEqual(report.restored_count, 0)
        self.assertEqual(len(asked), 1)
        self.assertEqual(source.read_bytes(), b"ocupado")

    def test_ask_policy_without_callback_skips(self) -> None:
        source, destination, _ = self.move_and_record()
        source.write_bytes(b"ocupado")
        report = self.history.undo_last_operation("ask")
        self.assertEqual(report.restored_count, 0)
        self.assertEqual(len(report.skipped), 1)
        self.assertTrue(destination.exists())

    def test_lock_prevents_simultaneous_restore(self) -> None:
        self.move_and_record()
        lock = FileLock(self.lock_path)
        lock.acquire()
        try:
            with self.assertRaises(LockBusyError):
                self.history.undo_last_operation("skip")
        finally:
            lock.release()

    def test_history_file_is_written_atomically_as_json(self) -> None:
        self.move_and_record()
        data = json.loads(self.history_path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["operations"]), 1)
        self.assertFalse(list(self.base.glob(".history-*.tmp")))


if __name__ == "__main__":
    unittest.main()
