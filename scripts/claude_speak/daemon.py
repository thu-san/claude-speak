"""Long-running daemon that keeps Python + Silero VAD + Kokoro models warm.

Without the daemon, every Stop hook spawns a fresh Python process and reloads
torch/silero/kokoro from scratch — ~2-3s of pure startup before the mic even
opens. The daemon owns those once and serves requests over a Unix socket, so
the hook is reduced to "send JSON, print response."

Protocol (newline-delimited JSON over a SOCK_STREAM Unix socket):

  client → daemon:  {"op": "speak", "transcript_path": "..."}
  daemon → client:  {"ok": true, "stdout": "...", "stderr": ""}

Operations:
  - speak:    run the full Stop-hook flow (rewrite + TTS + maybe dictate)
  - kill:     SIGTERM in-flight playback / dictation
  - status:   {"ok": true, "uptime_s": N, "in_flight": bool}
  - shutdown: clean exit

Design notes:
  - One request at a time. A new "speak" while one is running first calls
    `_kill_previous()` (the same path the existing fork-mode hook uses).
  - The daemon writes to speak.log via the same log()/section() functions, so
    the hook's view of what happened is unchanged.
  - On any unhandled exception, the daemon logs and keeps serving — never
    dies on a single bad request.
  - Stale socket files from a killed prior daemon are detected and reclaimed.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from .config import DATA_DIR
from .logging import log, log_v, section

SOCKET_PATH = DATA_DIR / "daemon.sock"
PID_FILE = DATA_DIR / "daemon.pid"
START_LOCK = DATA_DIR / "daemon.start.lock"


_MAX_MSG_BYTES = 1 * 1024 * 1024  # 1 MiB — one speak/dictate payload is < 10 KiB;
                                   # cap is only here to stop pathological
                                   # clients from OOM'ing the daemon.


# ---- client side ----

def _socket_alive() -> bool:
    """True if a daemon is listening on SOCKET_PATH."""
    if not SOCKET_PATH.exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(str(SOCKET_PATH))
        s.close()
        return True
    except OSError:
        # Stale socket file — clean up so the next bind succeeds.
        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass
        return False


def send_request(req: dict, timeout: float = 600.0) -> dict | None:
    """Send a JSON request; return the JSON response. None on connection error."""
    if not SOCKET_PATH.exists():
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(SOCKET_PATH))
    except OSError:
        return None
    try:
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        # Read until newline, capped so a buggy server can't OOM us.
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > _MAX_MSG_BYTES:
                log(f"daemon reply exceeded {_MAX_MSG_BYTES} bytes; aborting")
                return None
        if not buf:
            return None
        line, _, _ = buf.partition(b"\n")
        return json.loads(line.decode("utf-8"))
    except Exception as e:
        log(f"daemon client error: {type(e).__name__}: {e}")
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


def _spawn_daemon() -> None:
    """Fork + detach + exec the daemon. Parent returns immediately."""
    from .venv import VENV_PYTHON, venv_ready
    py = str(VENV_PYTHON) if venv_ready() else sys.executable
    pid = os.fork()
    if pid == 0:
        os.setsid()
        if os.fork() > 0:
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull, fd)
            except OSError:
                pass
        os.execv(py, [py, "-m", "claude_speak.daemon", "serve"])


def _wait_for_socket(timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _socket_alive():
            return True
        time.sleep(0.1)
    return False


def ensure_daemon() -> bool:
    """If the daemon isn't running, fork a detached child to start it.
    Returns True if the daemon is alive AND responsive after this call.

    Two recovery paths for wedged state:
      * Socket alive but unresponsive → status RPC times out → kill holder,
        respawn.
      * No socket AND spawn timed out → a prior crashed daemon left a stale
        start.lock (with a still-live pid that's stuck in preload / import /
        etc.). The fresh spawn's _acquire_start_lock sees 'another daemon
        running' and silently exits without binding. Detect this by timeout,
        wipe the holder, retry ONCE. Without this, every subsequent hook
        call times out the same way until the user manually intervenes.
    """
    if _socket_alive():
        if _daemon_responsive():
            return True
        log("daemon socket alive but unresponsive — killing and respawning")
        _kill_held_daemon()
    log("starting daemon (background)…")
    # Cold-start preload of Silero + Kokoro can take 20-35s on Intel CPU;
    # 60s gives enough headroom without forcing a fallback on every spawn.
    wait_s = 60
    _spawn_daemon()
    if _wait_for_socket(wait_s):
        return True
    # Spawn timed out. Usually means a stale/wedged predecessor is holding
    # the start.lock — our child saw the held lock, bailed silently. Wipe
    # and retry once.
    log(f"daemon failed to start within {wait_s}s — attempting recovery")
    _kill_held_daemon()
    _spawn_daemon()
    if _wait_for_socket(wait_s):
        log("daemon recovered after second spawn")
        return True
    log(f"daemon still down after recovery attempt; falling back to in-process")
    return False


def _daemon_responsive() -> bool:
    """Send a status RPC with a tight timeout. Returns True iff the daemon
    answered. Used to distinguish 'socket alive' from 'daemon actually
    serving requests' (a hung daemon thread holds the socket but never
    accepts/replies)."""
    resp = send_request({"op": "status"}, timeout=2)
    return bool(resp and resp.get("ok"))


def _pid_is_ours(pid: int) -> bool:
    """True if `pid` is alive AND owned by the current user.

    os.kill(pid, 0) raises PermissionError when the PID exists but belongs
    to someone else (common after a reboot + PID reuse), ProcessLookupError
    when the PID isn't live. We signal only when neither error fires."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _kill_held_daemon() -> None:
    """Best-effort: terminate the daemon process named in PID_FILE, then
    clean up its socket / pid / start lock so a fresh spawn can bind.

    Only signals the PID if ownership looks right — stale PID files are
    routine here (crashes, reboots), and on macOS PIDs recycle fast, so
    naive `os.kill(pid_from_file)` could SIGKILL a stranger."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if not _pid_is_ours(pid):
                log(f"stale/foreign pid {pid} in {PID_FILE.name}; skipping kill")
            else:
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    if not _pid_is_ours(pid):
                        break
                    time.sleep(0.1)
                else:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    for p in (SOCKET_PATH, PID_FILE, START_LOCK):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


# ---- server side ----

_T_START = time.monotonic()
_request_lock = threading.Lock()
_in_flight = [False]


def _handle(req: dict) -> dict:
    op = req.get("op", "")
    if op == "status":
        return {
            "ok": True,
            "uptime_s": time.monotonic() - _T_START,
            "in_flight": _in_flight[0],
            "pid": os.getpid(),
        }
    if op == "shutdown":
        # Reply first, then exit after a moment via a side thread. Clean up
        # the socket / pid / start-lock so the next daemon can spawn cleanly.
        def _exit() -> None:
            for p in (SOCKET_PATH, PID_FILE, START_LOCK):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            os._exit(0)
        threading.Timer(0.1, _exit).start()
        return {"ok": True, "shutdown": True}
    if op == "kill":
        from .main import _kill_previous
        from .recording import RECORD_CANCEL
        _kill_previous()
        RECORD_CANCEL.set()  # break any in-flight record loop ASAP
        return {"ok": True}
    if op == "dictate":
        # CLI-style: record + transcribe via the warm models, return the
        # transcript as a string. Honors `overrides` (model/lang/threads/silence).
        # Uses _dictate_in_process directly to avoid recursing through the
        # daemon-routing wrapper.
        from .stt import _dictate_in_process
        from .config import load_config
        from .defaults import DEFAULTS
        if _in_flight[0]:
            # A prior request is in flight. If it's a recording, abort it via
            # RECORD_CANCEL so it releases the lock; if it's playback, kill
            # ffplay. Together this makes "newest dictate wins" — the prior
            # recording stops, the new mic opens immediately.
            from .recording import RECORD_CANCEL
            from .main import _kill_previous
            RECORD_CANCEL.set()
            _kill_previous()
        with _request_lock:
            _in_flight[0] = True
            try:
                cfg = load_config(DEFAULTS)
                cfg.update(req.get("overrides") or {})
                text = _dictate_in_process(cfg)
                return {"ok": True, "text": text}
            except Exception as e:
                import traceback
                log(f"daemon dictate error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                return {"ok": False, "error": str(e)}
            finally:
                _in_flight[0] = False
    # "turn" is the full conversational turn (rewrite → speak → listen →
    # feedback). "speak" is kept as an alias for one release so pre-rename
    # shims don't immediately break after a user pulls.
    if op in ("turn", "speak"):
        from .main import _kill_previous, run_turn
        if _in_flight[0]:
            _kill_previous()
        with _request_lock:
            _in_flight[0] = True
            try:
                stdout = run_turn(req)
            except Exception as e:
                import traceback
                log(f"daemon turn error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                return {"ok": False, "error": str(e)}
            finally:
                _in_flight[0] = False
            return {"ok": True, "stdout": stdout}
    return {"ok": False, "error": f"unknown op: {op!r}"}


def _client_thread(conn: socket.socket) -> None:
    try:
        conn.settimeout(600)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
            if len(buf) > _MAX_MSG_BYTES:
                try:
                    conn.sendall(
                        (json.dumps({"ok": False, "error": "request too large"})
                         + "\n").encode("utf-8"))
                except OSError:
                    pass
                return
        line, _, _ = buf.partition(b"\n")
        try:
            req = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as e:
            conn.sendall((json.dumps({"ok": False, "error": f"bad json: {e}"}) + "\n").encode())
            return
        resp = _handle(req)
        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
    except Exception as e:
        log(f"daemon client thread error: {type(e).__name__}: {e}")
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _preload() -> None:
    """Eagerly load Silero + Kokoro so the first speak request doesn't pay the
    cold-start cost. Best-effort — failures here are logged and don't abort
    daemon startup."""
    try:
        from .recording import _ensure_silero
        if _ensure_silero():
            import silero_vad  # type: ignore
            silero_vad.load_silero_vad(onnx=True)
            log_v("preloaded silero VAD")
    except Exception as e:
        log(f"silero preload failed: {type(e).__name__}: {e}")
    try:
        from .tts.kokoro import _load
        from .config import load_config
        from .defaults import DEFAULTS
        cfg = load_config(DEFAULTS)
        if _load(cfg) is not None:
            log_v("preloaded kokoro")
    except Exception as e:
        log(f"kokoro preload failed: {type(e).__name__}: {e}")


def _acquire_start_lock() -> bool:
    """Atomic O_EXCL lock so two simultaneously-spawned daemons can't both bind.
    Returns True if we got the lock; the lock holder exits on shutdown."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(START_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        # Is the holder alive?
        try:
            other = int(START_LOCK.read_text().strip())
            os.kill(other, 0)
            return False  # another daemon is starting / running
        except (ValueError, ProcessLookupError, PermissionError):
            # Stale lock — reclaim.
            try:
                START_LOCK.unlink()
            except FileNotFoundError:
                pass
            return _acquire_start_lock()


def _start_code_watchdog() -> None:
    """Background thread: every 30s, snapshot mtimes of the daemon-relevant
    Python files. If any moved forward since startup, shut down so the next
    request respawns a fresh daemon running the new code. Skipped while a
    request is in flight."""
    targets = _watched_files()
    snapshot = {p: _safe_mtime(p) for p in targets}
    log(f"code watchdog: tracking {len(snapshot)} files")

    def _watch() -> None:
        while True:
            time.sleep(30)
            # Check _in_flight first as a cheap bailout, then acquire the
            # request lock non-blocking — if we can't grab it, a request is
            # either in flight or about to start and we must NOT exit this
            # tick. Without this serialization, os._exit(0) can race an
            # arriving request and leave the client hung until its 60/600s
            # socket timeout.
            if _in_flight[0]:
                continue
            if not _request_lock.acquire(blocking=False):
                continue
            try:
                # Recheck under lock — state could have changed between the
                # unlocked check and here.
                if _in_flight[0]:
                    continue
                for p, t0 in snapshot.items():
                    t = _safe_mtime(p)
                    if t is not None and t0 is not None and t > t0:
                        log(f"code change detected in {p.name} → shutting down (will respawn)")
                        for q in (SOCKET_PATH, PID_FILE, START_LOCK):
                            try:
                                q.unlink()
                            except FileNotFoundError:
                                pass
                        os._exit(0)
            finally:
                _request_lock.release()
    threading.Thread(target=_watch, daemon=True).start()


def _watched_files() -> list[Path]:
    """The set of source files whose mtime change should trigger a respawn.
    Anything imported at request-handle time qualifies."""
    pkg = Path(__file__).resolve().parent
    return [
        pkg / "daemon.py",
        pkg / "main.py",
        pkg / "pipeline.py",
        pkg / "rewrite.py",
        pkg / "recording.py",
        pkg / "transcript.py",
        pkg / "audio.py",
        pkg / "logging.py",
        pkg / "config.py",
        pkg / "defaults.py",
        pkg / "stt" / "__init__.py",
        pkg / "stt" / "whisper_cpp.py",
        pkg / "stt" / "cancel.py",
        pkg / "tts" / "__init__.py",
        pkg / "tts" / "kokoro.py",
    ]


def _safe_mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return None


def serve() -> None:
    """Bind the socket and accept connections forever."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Owner-only on the data dir — transcripts, recordings, voice logs all
    # live here; no reason another local user should read them.
    try:
        DATA_DIR.chmod(0o700)
    except OSError:
        pass

    if not _acquire_start_lock():
        log("daemon already running (start lock held); exiting")
        return

    # Only unlink the stale socket if it's a real socket we own (not a
    # symlink planted by another local user between our unlink and bind).
    if SOCKET_PATH.exists() or SOCKET_PATH.is_symlink():
        try:
            st = SOCKET_PATH.lstat()
            import stat as _stat
            if _stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
                log(f"refusing to unlink suspicious {SOCKET_PATH} "
                    f"(symlink={_stat.S_ISLNK(st.st_mode)} uid={st.st_uid})")
                return
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass

    # SIGTERM / SIGINT: clean shutdown.
    def _term(*_args) -> None:
        log("daemon shutting down (signal)")
        for p in (SOCKET_PATH, PID_FILE, START_LOCK):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    PID_FILE.write_text(str(os.getpid()))
    section("🛰️  DAEMON")
    log(f"listening on {SOCKET_PATH} (pid {os.getpid()})")
    _preload()
    log("daemon ready")
    _start_code_watchdog()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Bind under a restrictive umask so the socket inode lands with
    # owner-only permissions. Any other local user connecting to our
    # socket could make us speak arbitrary text or kill our processes.
    prev_umask = os.umask(0o077)
    try:
        sock.bind(str(SOCKET_PATH))
    finally:
        os.umask(prev_umask)
    # Belt-and-suspenders in case the umask dance didn't take (e.g. NFS).
    try:
        os.chmod(str(SOCKET_PATH), 0o600)
    except OSError:
        pass
    sock.listen(8)
    try:
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                continue
            t = threading.Thread(target=_client_thread, args=(conn,), daemon=True)
            t.start()
    finally:
        try:
            sock.close()
        except OSError:
            pass
        _term()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m claude_speak.daemon {serve | status | stop | restart}",
              file=sys.stderr)
        return 1
    cmd = argv[0]
    if cmd == "serve":
        serve()
        return 0
    if cmd == "status":
        if not _socket_alive():
            print("daemon: not running")
            return 1
        resp = send_request({"op": "status"}, timeout=2)
        if not resp:
            print("daemon: socket exists but unresponsive")
            return 2
        print(json.dumps(resp, indent=2))
        return 0
    if cmd == "stop":
        if not _socket_alive():
            print("daemon: not running")
            return 0
        send_request({"op": "shutdown"}, timeout=2)
        # Give it a moment to close.
        for _ in range(20):
            if not SOCKET_PATH.exists():
                print("daemon: stopped")
                return 0
            time.sleep(0.1)
        print("daemon: didn't exit cleanly within 2s")
        return 1
    if cmd == "restart":
        if _socket_alive():
            send_request({"op": "shutdown"}, timeout=2)
            for _ in range(20):
                if not SOCKET_PATH.exists():
                    break
                time.sleep(0.1)
        if ensure_daemon():
            print("daemon: restarted")
            return 0
        print("daemon: failed to start")
        return 1
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    from .venv import ensure_venv_python
    ensure_venv_python()
    sys.exit(main(sys.argv[1:]))
