"""Pre-download every model and CLI claude-speak needs.

Called once by the SessionStart hook on first session (guarded by a marker
file) and manually via `/speak install` for explicit refreshes. Without this,
downloads happen lazily on the first Stop hook, which makes that first turn
confusingly slow.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .config import DATA_DIR
from .defaults import DEFAULTS
from .logging import log, notify
from .venv import VENV_DIR, VENV_PIP, VENV_PYTHON, running_in_venv, venv_ready

MARKER = DATA_DIR / ".installed_v1"
LOCK = DATA_DIR / ".install.lock"

# Root of the plugin's shipped scripts — where requirements.txt lives.
_SCRIPTS_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = _SCRIPTS_ROOT / "requirements.txt"
LOCKFILE = DATA_DIR / "requirements.lock"


def _acquire_lock() -> bool:
    """Prevent concurrent install runs. Returns True if we got the lock.

    Plugin reinstall + reload can fire SessionStart multiple times in quick
    succession. Without this, N daemons race on download + rename and all but
    one fail. We use an atomic O_CREAT|O_EXCL file and store our pid; stale
    locks (pid no longer running) are reclaimed.
    """
    import os
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        # Is the holder still alive?
        try:
            other = int(LOCK.read_text().strip())
            os.kill(other, 0)
            log(f"install: lock held by pid {other}; skipping")
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            log("install: stale lock, reclaiming")
            try:
                LOCK.unlink()
            except FileNotFoundError:
                pass
            return _acquire_lock()


def _release_lock() -> None:
    try:
        LOCK.unlink()
    except FileNotFoundError:
        pass


def _say(msg: str, quiet: bool, notify_too: bool = True) -> None:
    if not quiet:
        print(msg, flush=True)
    log(f"install: {msg}")
    if notify_too:
        notify("claude-speak install", msg)


def _progress_hook(label: str, quiet: bool):
    last = [0]

    def hook(block_num, block_size, total_size):
        if total_size <= 0:
            return
        downloaded = block_num * block_size
        pct = min(100, int(downloaded * 100 / total_size))
        if pct >= last[0] + 10 or pct == 100:
            last[0] = pct
            mb = total_size // (1024 * 1024)
            # Log every 10% so `tail -f speak.log` works during background installs.
            log(f"install: {label}: {pct}% of {mb}MB")
            if not quiet:
                print(f"  {label}: {pct}% of {mb}MB", flush=True)
    return hook


def _verify_onnx(path: Path) -> bool:
    """Parse the ONNX proto header to catch truncated / interleaved downloads.

    Full load through onnxruntime would be authoritative but costs ~1-2s. The
    proto magic at offset 0 plus the last bytes give a cheap sanity check that
    catches the realistic failure mode: concurrent writers producing a file of
    the right length but with garbled contents.
    """
    if not path.exists():
        return False
    if path.stat().st_size < 1_000_000:
        return False
    try:
        import onnx  # type: ignore
        onnx.checker.check_model(str(path))
        return True
    except ImportError:
        # onnx package not installed — fall back to a structural check.
        pass
    except Exception:
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(16)
        # Protobuf-encoded ONNX models begin with the `ir_version` field tag
        # 0x08 (varint field 1). Worth ~nothing on its own but catches the
        # obviously-junk case.
        return len(head) == 16 and head[0] == 0x08
    except OSError:
        return False


def _download(url: str, dest: Path, label: str, quiet: bool,
              verify=None) -> bool:
    """Download `url` to `dest`. If `verify(dest)` is given and a pre-existing
    file fails it, re-download; if a fresh download fails it, delete + fail."""
    if dest.exists() and dest.stat().st_size > 1_000_000:
        if verify is None or verify(dest):
            _say(f"{label}: already present ({dest.stat().st_size // (1024 * 1024)}MB)",
                 quiet, notify_too=False)
            return True
        _say(f"{label}: existing file failed integrity check — re-downloading",
             quiet)
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    _say(f"{label}: downloading from {url}", quiet)
    try:
        urllib.request.urlretrieve(url, tmp, reporthook=_progress_hook(label, quiet))
        tmp.rename(dest)
        if verify is not None and not verify(dest):
            _say(f"{label}: downloaded file failed integrity check — deleting",
                 quiet)
            try:
                dest.unlink()
            except FileNotFoundError:
                pass
            return False
        _say(f"{label}: done ({dest.stat().st_size // (1024 * 1024)}MB)", quiet)
        return True
    except Exception as e:
        _say(f"{label}: failed ({type(e).__name__}: {e})", quiet)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        return False


def _ensure_venv(quiet: bool) -> bool:
    """Create $CLAUDE_PLUGIN_DATA/.venv if missing. Idempotent."""
    if venv_ready():
        return True
    _say(f"creating plugin venv at {VENV_DIR}", quiet)
    import venv as _pyvenv
    try:
        _pyvenv.EnvBuilder(with_pip=True, upgrade_deps=False,
                           clear=False, symlinks=True).create(str(VENV_DIR))
    except Exception as e:
        _say(f"venv create failed: {type(e).__name__}: {e}", quiet)
        return False
    if not venv_ready():
        _say("venv create finished but python3 not found — aborting", quiet)
        return False
    _say(f"venv ready ({VENV_PYTHON})", quiet)
    return True


def _probe_in_venv(package: str) -> bool:
    """True if `package` imports cleanly inside the venv python."""
    probe = package.split("[")[0].replace("-", "_")
    result = subprocess.run(
        [str(VENV_PYTHON), "-c", f"import {probe}"],
        check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _pip_install(packages: list[str], label: str, quiet: bool, no_deps: bool = False) -> bool:
    if not venv_ready():
        _say(f"{label}: venv not ready — cannot pip install", quiet)
        return False
    missing = [pkg for pkg in packages if not _probe_in_venv(pkg)]
    if not missing:
        _say(f"{label}: already installed", quiet, notify_too=False)
        return True
    _say(f"{label}: pip installing {', '.join(missing)}{' (--no-deps)' if no_deps else ''}",
         quiet)
    cmd = [str(VENV_PIP), "install"]
    if quiet:
        cmd.append("--quiet")
    if no_deps:
        cmd.append("--no-deps")
    cmd.extend(missing)
    result = subprocess.run(cmd, check=False,
                            stdout=sys.stdout if not quiet else subprocess.DEVNULL,
                            stderr=subprocess.STDOUT if not quiet else subprocess.DEVNULL)
    ok = result.returncode == 0
    _say(f"{label}: {'done' if ok else 'failed (exit ' + str(result.returncode) + ')'}",
         quiet)
    return ok


def _pip_install_requirements(quiet: bool) -> bool:
    """Install everything from requirements.txt in one pip call so the resolver
    can pick a consistent set. We still invoke _pip_install per-group earlier
    for progress messaging, but this catches any package-level probe we missed."""
    if not REQUIREMENTS.exists():
        _say(f"requirements.txt missing at {REQUIREMENTS}", quiet)
        return False
    if not venv_ready():
        return False
    _say(f"pip installing from {REQUIREMENTS.name}", quiet)
    cmd = [str(VENV_PIP), "install"]
    if quiet:
        cmd.append("--quiet")
    cmd.extend(["-r", str(REQUIREMENTS)])
    result = subprocess.run(cmd, check=False,
                            stdout=sys.stdout if not quiet else subprocess.DEVNULL,
                            stderr=subprocess.STDOUT if not quiet else subprocess.DEVNULL)
    ok = result.returncode == 0
    _say(f"requirements: {'done' if ok else 'failed (exit ' + str(result.returncode) + ')'}",
         quiet)
    return ok


def _freeze_lockfile(quiet: bool) -> None:
    """After a successful install, snapshot the resolved versions to
    $CLAUDE_PLUGIN_DATA/requirements.lock — useful for bug reports."""
    if not venv_ready():
        return
    try:
        result = subprocess.run(
            [str(VENV_PIP), "freeze"],
            check=False, capture_output=True, text=True,
        )
        if result.returncode == 0:
            LOCKFILE.write_text(result.stdout)
            _say(f"lockfile written: {LOCKFILE}", quiet, notify_too=False)
    except Exception as e:
        _say(f"pip freeze failed: {type(e).__name__}: {e}", quiet, notify_too=False)


def _brew_install(pkg: str, label: str, quiet: bool) -> bool:
    if shutil.which(pkg.replace("-cpp", "-cli")) or shutil.which(pkg):
        _say(f"{label}: already installed", quiet, notify_too=False)
        return True
    if not shutil.which("brew"):
        _say(f"{label}: brew not found — install manually: brew install {pkg}", quiet)
        return False
    _say(f"{label}: brew installing {pkg}", quiet)
    result = subprocess.run(
        ["brew", "install", pkg], check=False,
        stdout=sys.stdout if not quiet else subprocess.DEVNULL,
        stderr=subprocess.STDOUT if not quiet else subprocess.DEVNULL,
    )
    ok = result.returncode == 0
    _say(f"{label}: {'done' if ok else 'failed'}", quiet)
    return ok


def install_all(cfg: dict | None = None, quiet: bool = False) -> bool:
    cfg = {**DEFAULTS, **(cfg or {})}
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not _acquire_lock():
        _say("another install is already running — skipping", quiet, notify_too=False)
        return True

    t0 = time.monotonic()
    _say("starting full dependency install — this is a one-time ~1GB download", quiet)

    all_ok = True

    # 0. Plugin-private venv (keeps torch/onnx/numpy off the user's system Python)
    if not _ensure_venv(quiet):
        _release_lock()
        return False

    # 1. One-shot pip install from requirements.txt — gives pip's resolver a
    # complete view so it picks a consistent numpy/torch combo. The per-group
    # _pip_install calls below then all report "already installed".
    all_ok &= _pip_install_requirements(quiet)

    # 2. Mic capture (probe for per-step progress report; already in reqs.txt)
    all_ok &= _pip_install(["sounddevice", "numpy"], "mic capture (sounddevice+numpy)", quiet)

    # 3. Silero VAD (also in reqs.txt — this call is usually a no-op)
    all_ok &= _pip_install(["silero-vad"], "Silero VAD", quiet)

    # 4. Kokoro TTS (pip + model + voices). kokoro-onnx's metadata says
    # numpy>=2 but its runtime works fine on 1.26 (verified). Install with
    # --no-deps so the resolver doesn't fight our numpy<2 pin; its real
    # runtime deps (onnxruntime, espeakng-loader, phonemizer-fork, colorlog)
    # are listed in requirements.txt.
    all_ok &= _pip_install(["kokoro-onnx>=0.5,<1"], "Kokoro TTS (pip)", quiet, no_deps=True)
    kokoro_dir = DATA_DIR / "kokoro"
    all_ok &= _download(cfg["kokoro_model_url"],
                        kokoro_dir / "kokoro-v1.0.onnx",
                        "Kokoro model", quiet,
                        verify=_verify_onnx)
    all_ok &= _download(cfg["kokoro_voices_url"],
                        kokoro_dir / "voices-v1.0.bin",
                        "Kokoro voices", quiet)

    # 4. whisper.cpp (brew) + chosen model
    all_ok &= _brew_install("whisper-cpp", "whisper.cpp", quiet)
    model_name = cfg["whisper_cpp_model"]
    model_url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{model_name}"
    all_ok &= _download(model_url,
                        DATA_DIR / "models" / model_name,
                        f"whisper model ({model_name})", quiet)

    dt = time.monotonic() - t0
    if all_ok:
        MARKER.write_text(str(int(time.time())))
        _freeze_lockfile(quiet)
        _say(f"all components ready ({dt:.1f}s)", quiet)
    else:
        _say(f"some components failed (see log); will retry on next run", quiet)
    _release_lock()
    return all_ok


def _find_install_dirs() -> list[Path]:
    """All ~/.claude/plugins/data/claude-speak-* directories currently on disk.
    Sorted alphabetically for stable output."""
    base = Path.home() / ".claude" / "plugins" / "data"
    if not base.is_dir():
        return []
    return sorted(
        e for e in base.iterdir()
        if e.is_dir() and e.name.startswith("claude-speak-")
    )


def _plugin_bindings() -> tuple[Path | None, list[str]]:
    """Inspect Claude Code's settings files for claude-speak@<marketplace>
    bindings.

    Returns (active_dir, disabled_marketplaces):
      - active_dir: the ~/.claude/plugins/data/claude-speak-<marketplace>/
        path for the FIRST enabled (True) claude-speak@X we find walking
        project scope → user scope. None if nothing enabled.
      - disabled_marketplaces: list of marketplace names bound but with
        value False — useful for a helpful 'it's disabled' hint.

    Search order: cwd + parents' .claude/settings{.local,}.json, then
    ~/.claude/settings{.local,}.json. First enabled wins."""
    import json
    base = Path.home() / ".claude" / "plugins" / "data"
    prefix = "claude-speak@"
    candidates = []
    cwd = Path.cwd().resolve()
    for d in [cwd] + list(cwd.parents):
        for name in ("settings.local.json", "settings.json"):
            candidates.append(d / ".claude" / name)
    home = Path.home() / ".claude"
    candidates.extend([home / "settings.local.json", home / "settings.json"])
    active: Path | None = None
    disabled: list[str] = []
    for p in candidates:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        enabled = data.get("enabledPlugins", {})
        for key, on in enabled.items():
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            marketplace = key[len(prefix):]
            if on and active is None:
                active = base / f"claude-speak-{marketplace}"
            elif not on and marketplace not in disabled:
                disabled.append(marketplace)
    return active, disabled


def uninstall_all(keep_log: bool = True, quiet: bool = False,
                  data_dir: Path | None = None) -> bool:
    """Wipe one install's venv, downloaded models, markers, config.

    `data_dir` defaults to the module's DATA_DIR (the currently-active
    install) for back-compat. Pass explicitly to target a specific sibling
    install (e.g. an orphaned @thu-san dir next to your active @local one).

    Does NOT remove the plugin from Claude Code — run `/plugin uninstall
    claude-speak` for that. This is the "reset my data" button.
    """
    import os
    dd = data_dir or DATA_DIR
    lock = dd / ".install.lock"
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)
            _say(f"an install is running (pid {pid}) in {dd}; refusing to uninstall", quiet)
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            try:
                lock.unlink()
            except FileNotFoundError:
                pass

    # Stop the daemon BEFORE wiping files — otherwise it keeps running with
    # stale references to the venv we're about to delete. Only targets the
    # daemon that belongs to THIS data_dir (matches its socket path).
    sock = dd / "daemon.sock"
    try:
        if sock.exists():
            import socket as _socket, json as _json
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.settimeout(2)
            try:
                s.connect(str(sock))
                s.sendall((_json.dumps({"op": "shutdown"}) + "\n").encode())
                _say(f"stopping daemon at {sock}", quiet, notify_too=False)
            except OSError:
                pass
            finally:
                try:
                    s.close()
                except OSError:
                    pass
            for _ in range(20):
                if not sock.exists():
                    break
                import time as _t
                _t.sleep(0.1)
    except Exception:
        pass

    _say(f"uninstalling plugin data at {dd}", quiet)
    targets = [
        (dd / ".venv", True),
        (dd / "kokoro", True),
        (dd / "models", True),
        (dd / "rewrite_sandbox", True),
        (dd / "last.mp3", False),
        (dd / "last.wav", False),
        (dd / "input.wav", False),
        (dd / ".installed_v1", False),
        (dd / "requirements.lock", False),
        (dd / ".install.lock", False),
        (dd / "config.json", False),
        (dd / "daemon.pid", False),
        (dd / "daemon.sock", False),
        (dd / "daemon.start.lock", False),
        (dd / "player.pid", False),
        (dd / "pipeline.pid", False),
        (dd / ".skip_next_turn", False),
    ]
    if not keep_log:
        targets.append((dd / "speak.log", False))

    removed = 0
    total_bytes = 0
    for path, is_dir in targets:
        if is_dir and path.is_dir():
            size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
            shutil.rmtree(path, ignore_errors=True)
            _say(f"  removed dir {path.name}/ ({size // (1024 * 1024)}MB)", quiet,
                 notify_too=False)
            removed += 1
            total_bytes += size
        elif not is_dir and path.exists():
            size = path.stat().st_size
            try:
                path.unlink()
            except OSError:
                continue
            _say(f"  removed {path.name} ({size} bytes)", quiet, notify_too=False)
            removed += 1
            total_bytes += size

    # If the dir is now empty (or only contains .DS_Store), remove it too —
    # otherwise you'd see stale empty dirs under ~/.claude/plugins/data/.
    try:
        leftovers = [p for p in dd.iterdir() if p.name != ".DS_Store"]
        if not leftovers:
            for p in dd.iterdir():
                try:
                    p.unlink()
                except OSError:
                    pass
            try:
                dd.rmdir()
                _say(f"  removed empty dir {dd.name}/", quiet, notify_too=False)
            except OSError:
                pass
    except FileNotFoundError:
        pass

    _say(f"uninstall complete ({removed} items, {total_bytes // (1024 * 1024)}MB freed)",
         quiet)
    return True


def _daemonize() -> None:
    """Detach fully from the parent shell so Claude Code's SessionStart hook
    returns in milliseconds while this process keeps working in the background."""
    import os
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Pre-install all claude-speak dependencies and models.",
    )
    ap.add_argument("--quiet", action="store_true",
                    help="suppress stdout; log + notifications only")
    ap.add_argument("--force", action="store_true",
                    help="rerun even if the marker file exists")
    ap.add_argument("--background", action="store_true",
                    help="double-fork and detach immediately, then run the install "
                    "in the background. Used by the SessionStart hook.")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove the venv, downloaded models, and plugin data. "
                    "Defaults to the single install if there's exactly one; "
                    "requires --target or --all when multiple side-by-side "
                    "installs exist, to avoid accidentally wiping the wrong one.")
    ap.add_argument("--wipe-logs", action="store_true",
                    help="with --uninstall, also delete speak.log")
    ap.add_argument("--target", metavar="MARKETPLACE",
                    help="with --uninstall, wipe only the install for this "
                    "marketplace (e.g. 'thu-san' or 'claude-speak-local'). "
                    "Leaves other side-by-side installs intact.")
    ap.add_argument("--all", action="store_true",
                    help="with --uninstall, wipe EVERY claude-speak install "
                    "found under ~/.claude/plugins/data/. Required when "
                    "multiple installs exist and no --target is given.")
    args = ap.parse_args()

    if args.uninstall:
        all_installs = _find_install_dirs()
        if not all_installs:
            print("no claude-speak installs found under ~/.claude/plugins/data/")
            return 0

        # Choose which install(s) to wipe, in order of preference:
        #   --target X    → just that one (explicit override)
        #   --all         → every install on disk
        #   (neither, currently-enabled claude-speak@X found via settings)
        #                 → use that one (the install Claude Code is using)
        #   (neither, exactly one dir on disk) → the single one
        #   (neither, 2+ dirs, no active binding) → refuse; require explicit choice
        if args.target:
            target_dir = (Path.home() / ".claude" / "plugins" / "data"
                          / f"claude-speak-{args.target}")
            if not target_dir.is_dir():
                print(f"no install at {target_dir}")
                print(f"found installs: {[p.name for p in all_installs]}")
                return 1
            installs = [target_dir]
        elif args.all:
            installs = all_installs
        else:
            active, disabled = _plugin_bindings()
            if active is not None and active.is_dir():
                installs = [active]
                print(f"active install: {active.name} (from Claude Code settings)")
            elif len(all_installs) == 1:
                installs = all_installs
            else:
                if disabled:
                    print(f"{len(all_installs)} claude-speak installs found; "
                          f"bindings for {disabled} are currently disabled "
                          f"in Claude Code (re-enable with /plugin to make "
                          f"uninstall auto-target):")
                else:
                    print(f"{len(all_installs)} claude-speak installs found, "
                          f"none currently enabled in Claude Code:")
                for p in all_installs:
                    size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                    marketplace = p.name[len("claude-speak-"):]
                    print(f"  {marketplace:30s}  {size // (1024 * 1024)}MB  ({p})")
                print()
                print("Refusing to wipe without an explicit choice. Pick one:")
                print("  --target <marketplace>   wipe only that install")
                print("  --all                    wipe all installs")
                return 1

        if not args.force:
            print(f"This will wipe {len(installs)} claude-speak install(s):")
            for p in installs:
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                print(f"  {p}  ({size // (1024 * 1024)}MB)")
            print()
            print("Contents: .venv/, kokoro/, models/, config.json, markers, lockfile.")
            if args.wipe_logs:
                print("speak.log will ALSO be deleted (--wipe-logs).")
            else:
                print("speak.log is preserved (pass --wipe-logs to also delete).")
            print()
            print("Re-run with --force to confirm.")
            return 1

        ok = True
        for p in installs:
            ok &= uninstall_all(
                keep_log=not args.wipe_logs,
                quiet=args.quiet,
                data_dir=p,
            )
        return 0 if ok else 1

    if not args.force and MARKER.exists():
        if not args.quiet:
            print(f"already installed (marker: {MARKER}).")
            print("re-run with --force to reinstall / pull latest models.")
        # Eagerly start the daemon at SessionStart so the first Stop hook
        # doesn't pay for daemon spawn + model preload.
        if args.background:
            _daemonize()
            _warm_daemon()
        return 0

    if args.background:
        _daemonize()
        # After daemonize we're in the grandchild with no tty; use quiet mode.
        ok = install_all(quiet=True)
        if ok:
            _warm_daemon()
        return 0

    ok = install_all(quiet=args.quiet)
    return 0 if ok else 1


def _warm_daemon() -> None:
    """Start the long-running daemon if it isn't already running. Best-effort —
    failures here are logged and don't interfere with the install/session."""
    try:
        from .daemon import ensure_daemon
        ensure_daemon()
    except Exception as e:
        log(f"warm-daemon failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
