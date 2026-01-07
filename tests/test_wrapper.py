import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from multiprocessing import Event, get_context
from pathlib import Path

WRAPPER_PATH = Path(__file__).resolve().parents[1] / "python3"


def _run_wrapper(wrapper_path: str, cwd: str, env: dict, args: list[str], queue) -> None:
    result = subprocess.run(
        [sys.executable, wrapper_path, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    queue.put((result.returncode, result.stdout, result.stderr))


def _hold_lock(wrapper_path: str, lock_path: str, ready: Event, hold_sec: float) -> None:
    loader = importlib.machinery.SourceFileLoader("pywrap", wrapper_path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)

    lock = module.FileLock(Path(lock_path), timeout_sec=30, poll_sec=0.05)
    lock.acquire()
    try:
        ready.set()
        time.sleep(hold_sec)
    finally:
        lock.release()


class WrapperTests(unittest.TestCase):
    def _base_env(self) -> dict:
        env = os.environ.copy()
        env.update(
            {
                "PYWRAP_BASE_PYTHON": sys.executable,
                "PYWRAP_DEP_MODE": "none",
                "PYWRAP_VENV_MODE": "project",
                "PYWRAP_UPGRADE_PIP": "0",
            }
        )
        return env

    def _run_wrapper(self, cwd: str, env: dict, args: list[str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(WRAPPER_PATH), *(args or ["-c", "print('ok')"])],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
        )

    def _venv_dir(self, project_root: Path) -> Path:
        return project_root / ".venv"

    def test_concurrent_invocations_share_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._base_env()
            ctx = get_context("spawn")
            queue = ctx.Queue()
            processes = []

            for _ in range(5):
                proc = ctx.Process(
                    target=_run_wrapper,
                    args=(str(WRAPPER_PATH), tmpdir, env, ["-c", "print('ok')"], queue),
                )
                proc.start()
                processes.append(proc)

            results = []
            for _ in processes:
                results.append(queue.get(timeout=120))

            for proc in processes:
                proc.join(timeout=120)

            failures = [result for result in results if result[0] != 0]
            self.assertFalse(failures, f"Expected all processes to succeed, got: {failures}")

    def test_lock_timeout_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._base_env()
            env["PYWRAP_LOCK_TIMEOUT_SEC"] = "1"
            env["PYWRAP_LOCK_POLL_SEC"] = "0.01"
            lock_path = str(Path(tmpdir) / ".venv.lock")
            ctx = get_context("spawn")
            ready = ctx.Event()
            holder = ctx.Process(
                target=_hold_lock,
                args=(str(WRAPPER_PATH), lock_path, ready, 3.0),
            )
            holder.start()
            self.assertTrue(ready.wait(timeout=10), "Lock holder did not signal readiness")

            result = subprocess.run(
                [sys.executable, str(WRAPPER_PATH), "-c", "print('wait')"],
                cwd=tmpdir,
                env=env,
                capture_output=True,
                text=True,
            )

            holder.join(timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Timeout waiting for lock", result.stderr)

    def test_invalid_base_python_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._base_env()
            env["PYWRAP_BASE_PYTHON"] = str(Path(tmpdir) / "missing-python")

            result = subprocess.run(
                [sys.executable, str(WRAPPER_PATH), "-c", "print('nope')"],
                cwd=tmpdir,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("[pywrap]", result.stderr)

    def test_verbose_logs_dep_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "requirements.txt").write_text("", encoding="utf-8")
            env = self._base_env()
            env["PYWRAP_VERBOSE"] = "1"
            env["PYWRAP_DEP_MODE"] = "requirements"

            result = self._run_wrapper(tmpdir, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[pywrap] project_root=", result.stderr)
            self.assertIn("dep_mode=requirements", result.stderr)

    def test_requirements_env_override_selects_requirements_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_req = Path(tmpdir, "custom-req.txt")
            custom_req.write_text("", encoding="utf-8")
            env = self._base_env()
            env["PYWRAP_VERBOSE"] = "1"
            env["PYWRAP_REQUIREMENTS"] = str(custom_req)

            result = self._run_wrapper(tmpdir, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("dep_mode=requirements", result.stderr)

    def test_cache_mode_uses_cache_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as cache_dir:
            Path(tmpdir, "requirements.txt").write_text("", encoding="utf-8")
            env = self._base_env()
            env["PYWRAP_VERBOSE"] = "1"
            env["PYWRAP_VENV_MODE"] = "cache"
            env["PYWRAP_CACHE_DIR"] = cache_dir

            result = self._run_wrapper(tmpdir, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            venv_dir_line = next(
                (line for line in result.stderr.splitlines() if "venv_dir=" in line),
                "",
            )
            self.assertIn(str(Path(cache_dir)), venv_dir_line)
            self.assertFalse(self._venv_dir(Path(tmpdir)).exists())

    def test_force_recreate_removes_existing_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "requirements.txt").write_text("", encoding="utf-8")
            env = self._base_env()

            result = self._run_wrapper(tmpdir, env)
            self.assertEqual(result.returncode, 0, result.stderr)

            venv_dir = self._venv_dir(Path(tmpdir))
            sentinel = venv_dir / "sentinel.txt"
            sentinel.write_text("old", encoding="utf-8")

            env["PYWRAP_FORCE_RECREATE"] = "1"
            result = self._run_wrapper(tmpdir, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(venv_dir.exists())
            self.assertFalse(sentinel.exists())

    def test_install_deps_writes_marker_with_pip_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "requirements.txt").write_text("", encoding="utf-8")
            env = self._base_env()
            env["PYWRAP_INSTALL_DEPS"] = "1"
            env["PYWRAP_UPGRADE_PIP"] = "0"
            env["PYWRAP_PIP_ARGS"] = "--no-index"

            result = self._run_wrapper(tmpdir, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            marker = self._venv_dir(Path(tmpdir)) / ".pywrap" / "ok.json"
            data = json.loads(marker.read_text("utf-8"))
            self.assertEqual(data["pip_args"], ["--no-index"])


if __name__ == "__main__":
    unittest.main()
