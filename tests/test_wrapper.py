import importlib.machinery
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from multiprocessing import Event, get_context
from pathlib import Path
from typing import Optional
from unittest import mock

WRAPPER_PATH = Path(__file__).resolve().parents[1] / "python3"


def _load_wrapper_module():
    loader = importlib.machinery.SourceFileLoader("pywrap", str(WRAPPER_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


WRAPPER_MODULE = _load_wrapper_module()


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
    module = _load_wrapper_module()
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

    def _run_wrapper(self, cwd: str, env: dict, args: Optional[list[str]] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(WRAPPER_PATH), *(args or ["-c", "print('ok')"])],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
        )

    def _venv_dir(self, project_root: Path) -> Path:
        return project_root / ".venv"

    def _runtime_dir(self, project_root: Path) -> Path:
        return self._venv_dir(project_root) / ".pywrap" / "running"

    def _wait_for_runtime_leases(
        self,
        runtime_dir: Path,
        *,
        min_count: int = 1,
        timeout: float = 10.0,
        require_initialized: bool = False,
        require_locked: bool = False,
    ) -> list[Path]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            leases = list(runtime_dir.glob("*.lock"))
            if len(leases) >= min_count:
                if require_initialized:
                    ready = [lease for lease in leases if WRAPPER_MODULE._load_json(lease) is not None]
                    if len(ready) >= min_count:
                        leases = ready
                    else:
                        time.sleep(0.05)
                        continue
                if require_locked:
                    locked = []
                    for lease in leases:
                        probe = WRAPPER_MODULE._try_lock_once(lease, write_metadata=False)
                        if probe is None:
                            locked.append(lease)
                        else:
                            probe.release()
                    if len(locked) >= min_count:
                        return locked
                    time.sleep(0.05)
                    continue
                else:
                    return leases
            time.sleep(0.05)
        self.fail(f"Timed out waiting for runtime leases in {runtime_dir}")

    def _reap_process(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                proc.kill()
        proc.communicate(timeout=10)

    def _cache_venv_dir(self, project_root: Path, env: dict) -> Path:
        dep_mode_env = env.get("PYWRAP_DEP_MODE", "").strip().lower()
        req_file = Path(env.get("PYWRAP_REQUIREMENTS", str(project_root / "requirements.txt")))
        has_req = req_file.is_file()
        has_pyproject = (project_root / "pyproject.toml").is_file()

        dep_mode = dep_mode_env
        if dep_mode not in ("requirements", "pyproject"):
            dep_mode = "requirements" if has_req else "pyproject"
        if dep_mode == "pyproject" and not has_pyproject and not has_req:
            dep_mode = "none"

        pip_args = shlex.split(env.get("PYWRAP_PIP_ARGS", "").strip())
        local_first = env.get("PYWRAP_LOCAL_FIRST", "").strip().lower() in ("1", "true", "yes", "y", "on")
        dep_hash = WRAPPER_MODULE._dep_fingerprint(
            Path(env["PYWRAP_BASE_PYTHON"]).resolve(),
            dep_mode,
            project_root,
            req_file,
            pip_args,
            local_first,
        )
        cache_base = Path(env["PYWRAP_CACHE_DIR"])
        py_tag = f"py{sys.version_info.major}.{sys.version_info.minor}"
        return cache_base / "venvs" / py_tag / dep_hash[:16]

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

    @unittest.skipUnless(os.name == "nt", "Windows-only test for python3.cmd shim")
    def test_windows_cmd_shim_invokes_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._base_env()
            env["PYWRAP_VENV_MODE"] = "project"
            result = subprocess.run(
                ["cmd", "/c", "python3.cmd", "-c", "print('ok')"],
                cwd=str(WRAPPER_PATH.parent),
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "ok")

    def test_local_first_records_marker_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "requirements.txt").write_text("", encoding="utf-8")
            env = self._base_env()
            env["PYWRAP_INSTALL_DEPS"] = "1"
            env["PYWRAP_UPGRADE_PIP"] = "0"
            env["PYWRAP_LOCAL_FIRST"] = "1"

            result = self._run_wrapper(tmpdir, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            marker = self._venv_dir(Path(tmpdir)) / ".pywrap" / "ok.json"
            data = json.loads(marker.read_text("utf-8"))
            self.assertTrue(data["local_first"])

    def test_force_recreate_waits_for_active_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._base_env()
            env["PYWRAP_LOCK_TIMEOUT_SEC"] = "10"
            env["PYWRAP_LOCK_POLL_SEC"] = "0.05"

            result = self._run_wrapper(tmpdir, env)
            self.assertEqual(result.returncode, 0, result.stderr)

            venv_dir = self._venv_dir(Path(tmpdir))
            sentinel = venv_dir / "sentinel.txt"
            sentinel.write_text("old", encoding="utf-8")

            runner = subprocess.Popen(
                [sys.executable, str(WRAPPER_PATH), "-c", "import time; time.sleep(2)"],
                cwd=tmpdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self._wait_for_runtime_leases(
                    self._runtime_dir(Path(tmpdir)),
                    require_locked=True,
                )

                recreate_env = env.copy()
                recreate_env["PYWRAP_FORCE_RECREATE"] = "1"
                recreator = subprocess.Popen(
                    [sys.executable, str(WRAPPER_PATH), "-c", "print('recreated')"],
                    cwd=tmpdir,
                    env=recreate_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    time.sleep(0.5)
                    self.assertIsNone(recreator.poll(), "force recreate should wait for the active runtime lease")

                    runner_stdout, runner_stderr = runner.communicate(timeout=10)
                    self.assertEqual(runner.returncode, 0, runner_stderr or runner_stdout)

                    recreate_stdout, recreate_stderr = recreator.communicate(timeout=10)
                    self.assertEqual(recreator.returncode, 0, recreate_stderr)
                    self.assertEqual(recreate_stdout.strip(), "recreated")
                    self.assertFalse(sentinel.exists(), "force recreate should replace the old venv after the lease ends")
                finally:
                    self._reap_process(recreator)
            finally:
                if runner.poll() is None:
                    self._reap_process(runner)

    def test_force_recreate_times_out_while_runtime_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._base_env()

            result = self._run_wrapper(tmpdir, env)
            self.assertEqual(result.returncode, 0, result.stderr)

            runner = subprocess.Popen(
                [sys.executable, str(WRAPPER_PATH), "-c", "import time; time.sleep(10)"],
                cwd=tmpdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self._wait_for_runtime_leases(
                    self._runtime_dir(Path(tmpdir)),
                    require_locked=True,
                )
                if runner.poll() is not None:
                    runner_stdout, runner_stderr = runner.communicate(timeout=10)
                    self.fail(
                        "runtime exited before timeout assertion: "
                        f"rc={runner.returncode} stdout={runner_stdout!r} stderr={runner_stderr!r}"
                    )

                recreate_env = env.copy()
                recreate_env["PYWRAP_FORCE_RECREATE"] = "1"
                recreate_env["PYWRAP_LOCK_TIMEOUT_SEC"] = "1"
                recreate_env["PYWRAP_LOCK_POLL_SEC"] = "0.05"
                result = self._run_wrapper(tmpdir, recreate_env, ["-c", "print('recreated')"])

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Timeout waiting for active venv users to exit", result.stderr)
            finally:
                self._reap_process(runner)

    def test_pending_marker_blocks_reuse_without_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "requirements.txt").write_text("", encoding="utf-8")
            env = self._base_env()

            result = self._run_wrapper(tmpdir, env)
            self.assertEqual(result.returncode, 0, result.stderr)

            pending = self._venv_dir(Path(tmpdir)) / ".pywrap" / "installing.json"
            pending.parent.mkdir(parents=True, exist_ok=True)
            pending.write_text(json.dumps({"dep_hash": "incomplete"}) + "\n", encoding="utf-8")

            result = self._run_wrapper(tmpdir, env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("dependency installation was interrupted", result.stderr)

    def test_pending_marker_recovers_with_install_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "requirements.txt").write_text("", encoding="utf-8")
            env = self._base_env()
            env["PYWRAP_INSTALL_DEPS"] = "1"
            env["PYWRAP_UPGRADE_PIP"] = "0"

            result = self._run_wrapper(tmpdir, env)
            self.assertEqual(result.returncode, 0, result.stderr)

            marker = self._venv_dir(Path(tmpdir)) / ".pywrap" / "ok.json"
            before = json.loads(marker.read_text("utf-8"))
            time.sleep(1.1)

            pending = self._venv_dir(Path(tmpdir)) / ".pywrap" / "installing.json"
            pending.write_text(json.dumps({"dep_hash": "incomplete"}) + "\n", encoding="utf-8")

            result = self._run_wrapper(tmpdir, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(pending.exists())

            after = json.loads(marker.read_text("utf-8"))
            self.assertGreaterEqual(after["created_at"], before["created_at"] + 1)
            self.assertEqual(Path(after["requirements"]).resolve(), (Path(tmpdir) / "requirements.txt").resolve())

    def test_cache_mode_keys_include_pip_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as cache_dir:
            Path(tmpdir, "requirements.txt").write_text("", encoding="utf-8")

            env_a = self._base_env()
            env_a["PYWRAP_VENV_MODE"] = "cache"
            env_a["PYWRAP_CACHE_DIR"] = cache_dir
            env_a["PYWRAP_PIP_ARGS"] = "--index-url https://example.com/simple"

            env_b = self._base_env()
            env_b["PYWRAP_VENV_MODE"] = "cache"
            env_b["PYWRAP_CACHE_DIR"] = cache_dir
            env_b["PYWRAP_PIP_ARGS"] = "--extra-index-url https://example.com/extra"

            result_a = self._run_wrapper(tmpdir, env_a)
            result_b = self._run_wrapper(tmpdir, env_b)

            self.assertEqual(result_a.returncode, 0, result_a.stderr)
            self.assertEqual(result_b.returncode, 0, result_b.stderr)
            self.assertNotEqual(
                self._cache_venv_dir(Path(tmpdir), env_a),
                self._cache_venv_dir(Path(tmpdir), env_b),
            )

    def test_tmp_cleanup_only_removes_matching_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir)
            venv_a = parent / "venv-a"
            venv_b = parent / "venv-b"
            tmp_a = parent / f"{WRAPPER_MODULE._tmp_venv_prefix(venv_a)}leftover"
            tmp_b = parent / f"{WRAPPER_MODULE._tmp_venv_prefix(venv_b)}leftover"
            tmp_a.mkdir()
            tmp_b.mkdir()

            WRAPPER_MODULE._cleanup_tmp(parent, prefix=WRAPPER_MODULE._tmp_venv_prefix(venv_a))

            self.assertFalse(tmp_a.exists())
            self.assertTrue(tmp_b.exists())

    def test_stale_runtime_lease_is_pruned_after_process_kill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._base_env()

            result = self._run_wrapper(tmpdir, env)
            self.assertEqual(result.returncode, 0, result.stderr)

            runner = subprocess.Popen(
                [sys.executable, str(WRAPPER_PATH), "-c", "import time; time.sleep(10)"],
                cwd=tmpdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                leases = self._wait_for_runtime_leases(self._runtime_dir(Path(tmpdir)))
                stale_lease = leases[0]
                self.assertTrue(stale_lease.exists())
            finally:
                self._reap_process(runner)

            self.assertTrue(stale_lease.exists(), "Killed runtime should leave a stale lease file behind")

            recreate_env = env.copy()
            recreate_env["PYWRAP_FORCE_RECREATE"] = "1"
            recreate_result = self._run_wrapper(tmpdir, recreate_env, ["-c", "print('recreated')"])

            self.assertEqual(recreate_result.returncode, 0, recreate_result.stderr)
            self.assertFalse(stale_lease.exists(), "Next recreate should prune stale runtime lease files")

    def test_prune_runtime_leases_releases_before_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            lease_path = self._runtime_dir(project_root) / "stale.lock"
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            lease_path.write_text(json.dumps({"pid": 123}) + "\n", encoding="utf-8")

            calls: list[str] = []

            class Probe:
                def release(self_inner) -> None:
                    calls.append("release")

            def unlink_side_effect(path_self: Path, *, missing_ok: bool = False) -> None:
                self.assertTrue(missing_ok)
                self.assertEqual(path_self, lease_path)
                self.assertEqual(calls, ["release"])
                calls.append("unlink")

            with mock.patch.object(WRAPPER_MODULE, "_lease_ready_for_prune", return_value=True):
                with mock.patch.object(WRAPPER_MODULE, "_try_lock_once", return_value=Probe()):
                    with mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink_side_effect):
                        active = WRAPPER_MODULE._prune_runtime_leases(self._venv_dir(project_root))

            self.assertEqual(active, [])
            self.assertEqual(calls, ["release", "unlink"])

    def test_prune_runtime_leases_keeps_recent_uninitialized_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            lease_path = self._runtime_dir(project_root) / "pending.lock"
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            lease_path.write_text("", encoding="utf-8")

            calls: list[str] = []

            class Probe:
                def release(self_inner) -> None:
                    calls.append("release")

            with mock.patch.object(WRAPPER_MODULE, "_try_lock_once", return_value=Probe()):
                active = WRAPPER_MODULE._prune_runtime_leases(self._venv_dir(project_root))

            self.assertEqual(active, [lease_path])
            self.assertEqual(calls, [])
            self.assertTrue(lease_path.exists())


if __name__ == "__main__":
    unittest.main()
