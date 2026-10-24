from __future__ import annotations

import threading
import time
import unittest

from src.scheduler import OrganizationScheduler


class SchedulerTests(unittest.TestCase):
    def make_scheduler(self, **kwargs: object) -> tuple[OrganizationScheduler, threading.Event, list[str]]:
        finished = threading.Event()
        calls: list[str] = []

        def job(stop_event: threading.Event) -> None:
            calls.append("run")
            finished.set()

        scheduler = OrganizationScheduler(1, job, **kwargs)
        return scheduler, finished, calls

    def test_run_immediately_executes_the_job(self) -> None:
        scheduler, finished, calls = self.make_scheduler(run_immediately=True)
        scheduler.start()
        try:
            self.assertTrue(finished.wait(5.0), "El programador no ejecutó el trabajo")
            self.assertEqual(calls, ["run"])
            self.assertTrue(scheduler.running)
            self.assertIn("Automático", scheduler.status_text())
        finally:
            scheduler.stop(wait=True)
        self.assertFalse(scheduler.running)
        self.assertEqual(scheduler.status_text(), "Programador detenido")

    def test_disabled_scheduler_does_not_run(self) -> None:
        scheduler, finished, calls = self.make_scheduler(
            run_immediately=True, enabled=False
        )
        scheduler.start()
        try:
            self.assertFalse(finished.wait(1.5))
            self.assertEqual(calls, [])
            self.assertEqual(scheduler.status_text(), "Organización automática pausada")
        finally:
            scheduler.stop(wait=True)

    def test_trigger_now_runs_immediately(self) -> None:
        scheduler, finished, calls = self.make_scheduler()
        scheduler.start()
        try:
            scheduler.trigger_now()
            self.assertTrue(finished.wait(5.0))
            self.assertEqual(calls, ["run"])
        finally:
            scheduler.stop(wait=True)

    def test_pause_prevents_trigger(self) -> None:
        scheduler, finished, calls = self.make_scheduler()
        scheduler.start()
        try:
            scheduler.pause()
            scheduler.trigger_now()
            self.assertFalse(finished.wait(1.5))
            self.assertEqual(calls, [])
            self.assertTrue(scheduler.enabled is False)
            scheduler.resume()
            self.assertTrue(scheduler.enabled)
        finally:
            scheduler.stop(wait=True)

    def test_interval_is_clamped(self) -> None:
        scheduler, _, _ = self.make_scheduler()
        self.assertEqual(scheduler.interval_minutes, 1)
        scheduler.set_interval(0)
        self.assertEqual(scheduler.interval_minutes, 1)
        scheduler.set_interval(60)
        self.assertEqual(scheduler.interval_minutes, 60)

    def test_job_errors_are_reported_without_stopping(self) -> None:
        finished = threading.Event()
        events: list[tuple[str, str]] = []

        def job(stop_event: threading.Event) -> None:
            finished.set()
            raise RuntimeError("fallo simulado")

        scheduler = OrganizationScheduler(
            1,
            job,
            run_immediately=True,
            on_event=lambda level, message: events.append((level, message)),
        )
        scheduler.start()
        try:
            self.assertTrue(finished.wait(5.0))
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not any(
                level == "error" for level, _ in events
            ):
                time.sleep(0.05)
            self.assertTrue(any(level == "error" for level, _ in events))
            self.assertEqual(scheduler.last_error, "fallo simulado")
            self.assertTrue(scheduler.running)
        finally:
            scheduler.stop(wait=True)


if __name__ == "__main__":
    unittest.main()
