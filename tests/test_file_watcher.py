from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.file_watcher import WATCHDOG_AVAILABLE, FileWatcher


@unittest.skipUnless(WATCHDOG_AVAILABLE, "watchdog no está instalado")
class FileWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sfo-watcher-")
        self.root = Path(self.temp.name)
        self.detected: list[Path] = []
        self.event = threading.Event()
        self.errors: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def callback(self, path: Path) -> None:
        self.detected.append(path)
        self.event.set()

    def test_detects_a_new_stable_file(self) -> None:
        watcher = FileWatcher(
            self.root,
            self.callback,
            settle_seconds=1.0,
            poll_interval=0.2,
            on_event=lambda level, message: self.errors.append((level, message)),
        )
        watcher.start()
        try:
            self.assertTrue(watcher.running)
            self.assertIn("Vigilando", watcher.status_text())
            time.sleep(0.4)
            new_file = self.root / "nuevo archivo.txt"
            new_file.write_text("contenido", encoding="utf-8")
            self.assertTrue(self.event.wait(10), "No se detectó el archivo nuevo")
            self.assertEqual(self.detected[0].name, "nuevo archivo.txt")
        finally:
            watcher.stop(wait=True)
        self.assertFalse(watcher.running)
        self.assertFalse(any(level == "error" for level, _ in self.errors))

    def test_directory_events_are_ignored(self) -> None:
        watcher = FileWatcher(
            self.root,
            self.callback,
            settle_seconds=1.0,
            poll_interval=0.2,
        )
        watcher.start()
        try:
            time.sleep(0.4)
            (self.root / "subcarpeta").mkdir()
            self.assertFalse(self.event.wait(2.0))
            self.assertEqual(self.detected, [])
        finally:
            watcher.stop(wait=True)

    def test_pause_and_resume_state(self) -> None:
        watcher = FileWatcher(
            self.root,
            self.callback,
            settle_seconds=1.0,
            poll_interval=0.2,
        )
        watcher.start()
        try:
            watcher.pause()
            self.assertTrue(watcher.paused)
            self.assertIn("pausa", watcher.status_text())
            watcher.resume()
            self.assertFalse(watcher.paused)
            self.assertIn("Vigilando", watcher.status_text())
        finally:
            watcher.stop(wait=True)

    def test_invalid_directory_raises(self) -> None:
        watcher = FileWatcher(self.root / "no-existe", self.callback)
        with self.assertRaises(ValueError):
            watcher.start()

    def test_requires_watchdog_when_unavailable(self) -> None:
        watcher = FileWatcher(self.root, self.callback)
        if watcher.available:
            self.skipTest("watchdog está instalado")
        with self.assertRaises(RuntimeError):
            watcher.start()


if __name__ == "__main__":
    unittest.main()
