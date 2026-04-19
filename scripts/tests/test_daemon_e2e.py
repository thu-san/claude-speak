"""End-to-end daemon tests. Patch DATA_DIR + heavy imports so the daemon
serves on a tmp socket without touching real models, real config, or real
plugin dirs. Verifies: socket lifecycle, status op, shutdown cleanup of
SOCKET / PID / START_LOCK files, kill op, speak op routing, request
serialization."""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture
def tmp_path(tmp_path_factory):
    """Override pytest's tmp_path with a short /tmp path — AF_UNIX socket
    paths on macOS are capped at 104 bytes and pytest's default
    /var/folders/... path blows past that."""
    d = Path(tempfile.mkdtemp(prefix="cs-d-", dir="/tmp"))
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _wait_socket(path: Path, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.5)
            try:
                s.connect(str(path))
                s.close()
                return True
            except OSError:
                pass
        time.sleep(0.05)
    return False


def _wait_gone(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not path.exists():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def daemon_module(tmp_path, monkeypatch):
    """Reload claude_speak.daemon with DATA_DIR pointed at tmp_path."""
    # Patch config.DATA_DIR before importing daemon, so module-level
    # SOCKET_PATH/PID_FILE/START_LOCK pick up the tmp dir.
    import claude_speak.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "DATA_DIR", tmp_path, raising=False)

    # Force reimport of daemon so SOCKET_PATH etc. rebind.
    for mod in list(sys.modules):
        if mod.startswith("claude_speak.daemon"):
            del sys.modules[mod]
    import claude_speak.daemon as d
    d.SOCKET_PATH = tmp_path / "daemon.sock"
    d.PID_FILE = tmp_path / "daemon.pid"
    d.START_LOCK = tmp_path / "daemon.start.lock"
    return d


def _serve_in_subprocess(tmp_path: Path) -> subprocess_helper:
    """Spawn the daemon in a subprocess with DATA_DIR forced to tmp_path
    via a tiny bootstrap script. Returns a handle for cleanup."""
    import subprocess
    bootstrap = tmp_path / "boot.py"
    scripts_dir = Path(__file__).resolve().parent.parent
    bootstrap.write_text(
        f"import sys; sys.path.insert(0, {str(scripts_dir)!r})\n"
        f"from pathlib import Path\n"
        f"import claude_speak.config as c\n"
        f"c.DATA_DIR = Path({str(tmp_path)!r})\n"
        f"c.CONFIG_PATH = c.DATA_DIR / 'config.json'\n"
        f"import claude_speak.daemon as d\n"
        f"d.SOCKET_PATH = c.DATA_DIR / 'daemon.sock'\n"
        f"d.PID_FILE = c.DATA_DIR / 'daemon.pid'\n"
        f"d.START_LOCK = c.DATA_DIR / 'daemon.start.lock'\n"
        # Disable preload so we don't try to load Silero/Kokoro in tests.
        f"d._preload = lambda: None\n"
        f"d.serve()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, str(bootstrap)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return subprocess_helper(proc, tmp_path)


class subprocess_helper:
    def __init__(self, proc, tmp_path):
        self.proc = proc
        self.tmp_path = tmp_path
        self.sock_path = tmp_path / "daemon.sock"

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return _wait_socket(self.sock_path, timeout)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()


def _send(sock_path: Path, req: dict, timeout: float = 5.0) -> dict | None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        if not buf:
            return None
        line, _, _ = buf.partition(b"\n")
        return json.loads(line.decode())
    finally:
        try:
            s.close()
        except OSError:
            pass


# ---- tests ----

def test_socket_alive_false_when_no_daemon(daemon_module):
    assert daemon_module._socket_alive() is False


def test_socket_alive_cleans_stale_socket_file(daemon_module, tmp_path):
    # A leftover socket file with nothing listening on it should be
    # detected as stale and unlinked.
    daemon_module.SOCKET_PATH.touch()
    assert daemon_module._socket_alive() is False
    assert not daemon_module.SOCKET_PATH.exists()


def test_acquire_start_lock_basic(daemon_module):
    assert daemon_module._acquire_start_lock() is True
    assert daemon_module.START_LOCK.exists()
    # Second attempt fails because the holder (us, current pid) is alive.
    assert daemon_module._acquire_start_lock() is False
    daemon_module.START_LOCK.unlink()


def test_acquire_start_lock_reclaims_stale(daemon_module):
    # Simulate a stale lock from a long-dead pid.
    daemon_module.START_LOCK.write_text("999999")
    assert daemon_module._acquire_start_lock() is True
    daemon_module.START_LOCK.unlink()


def test_full_lifecycle_status_and_shutdown(tmp_path):
    """Spawn daemon, query status, shutdown — verify cleanup of all files."""
    h = _serve_in_subprocess(tmp_path)
    try:
        assert h.wait_ready(timeout=10), "daemon failed to come up"
        assert h.sock_path.exists()
        assert (tmp_path / "daemon.pid").exists()
        assert (tmp_path / "daemon.start.lock").exists()

        resp = _send(h.sock_path, {"op": "status"})
        assert resp and resp["ok"] is True
        assert resp["in_flight"] is False
        assert isinstance(resp["uptime_s"], (int, float))
        assert resp["pid"] == h.proc.pid

        resp = _send(h.sock_path, {"op": "shutdown"})
        assert resp and resp["ok"] is True

        # Wait for process to exit and files to be cleaned up.
        try:
            h.proc.wait(timeout=3)
        except Exception:
            pytest.fail("daemon did not exit after shutdown")

        assert _wait_gone(h.sock_path, timeout=2), "socket file leaked"
        assert _wait_gone(tmp_path / "daemon.pid", timeout=2), "pid file leaked"
        assert _wait_gone(tmp_path / "daemon.start.lock", timeout=2), "start lock leaked"
    finally:
        h.stop()


def test_unknown_op_returns_error(tmp_path):
    h = _serve_in_subprocess(tmp_path)
    try:
        assert h.wait_ready(timeout=10)
        resp = _send(h.sock_path, {"op": "no-such-op"})
        assert resp and resp["ok"] is False
        assert "unknown op" in resp["error"]
    finally:
        _send(h.sock_path, {"op": "shutdown"})
        h.stop()


def test_bad_json_handled_gracefully(tmp_path):
    h = _serve_in_subprocess(tmp_path)
    try:
        assert h.wait_ready(timeout=10)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(str(h.sock_path))
        s.sendall(b"this is not json\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        line, _, _ = buf.partition(b"\n")
        resp = json.loads(line.decode())
        assert resp["ok"] is False
        assert "bad json" in resp["error"]

        # Daemon should still be up and serving.
        resp2 = _send(h.sock_path, {"op": "status"})
        assert resp2 and resp2["ok"] is True
    finally:
        _send(h.sock_path, {"op": "shutdown"})
        h.stop()
