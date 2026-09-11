"""
Thin, injectable wrappers around the `btrfs`, `zstd`, `pv` and `ssh` commands.

Every call is built as an argv list - never a shell string - so paths and config
values can never be interpreted by a shell (see issue #9). `BtrfsOps` is the real
implementation; tests pass a fake with the same surface.
"""
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence


def _kill_process_group(procs: Sequence[subprocess.Popen], grace: float = 3.0) -> None:
    """Tear down a pipeline that was interrupted (Ctrl-C, SIGTERM). Every stage is
    spawned with ``start_new_session=True`` so it leads its own process group;
    signalling that group also stops any child it forked and, for an ``ssh`` sink,
    drops the channel so the remote ``btrfs receive`` sees EOF and exits. SIGTERM
    first, then SIGKILL for whatever is still alive after `grace` seconds."""
    def _signal(p: subprocess.Popen, sig: int) -> None:
        if p.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(p.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.send_signal(sig)
            except OSError:
                pass

    for p in procs:
        _signal(p, signal.SIGTERM)
    deadline = time.monotonic() + grace
    for p in procs:
        try:
            p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    for p in procs:
        _signal(p, signal.SIGKILL)
        try:
            p.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024


def prune_paths(root: Path, patterns: Sequence[str],
                is_subvolume: Callable[[Path], bool] = lambda _p: False) -> List[str]:
    """Delete every path under `root` that matches a glob in `patterns` (caches,
    trash, crash dumps) so it never enters the `btrfs send` stream. A matched
    path that is itself a nested subvolume is left alone - its data is not part
    of this snapshot anyway. Returns the sorted removed paths, relative to root.
    """
    removed: List[str] = []
    for pat in patterns:
        for match in sorted(root.glob(pat)):
            try:
                rel = str(match.relative_to(root))
            except ValueError:
                continue
            if not match.exists() and not match.is_symlink():
                continue
            try:
                if match.is_symlink() or not match.is_dir():
                    match.unlink()
                elif is_subvolume(match):
                    continue
                else:
                    shutil.rmtree(match)
                removed.append(rel)
            except OSError:
                pass
    return sorted(set(removed))


def build_ssh_args(config, user: str) -> List[str]:
    """`ssh` + option flags as a list (never a shell string). Shared by the
    backup engine (remote target) and the restore engine (staging)."""
    from pathlib import Path
    argv = ["ssh", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
    if getattr(config, "remote_port", 22) and config.remote_port != 22:
        argv += ["-p", str(config.remote_port)]
    key = Path(f"/home/{user}/.ssh/id_ed25519")
    if key.exists():
        argv += ["-i", str(key)]
    kh = Path(f"/home/{user}/.ssh/known_hosts")
    if kh.exists():
        argv += ["-o", f"UserKnownHostsFile={kh}"]
    return argv

CommandError = subprocess.CalledProcessError

ProgressCallback = Callable[[str], None]

# pv between send and receive gives a live byte counter + rate + elapsed even
# though `btrfs send` reports nothing itself. `-f` forces output off a tty.
_PV_ARGS = ["pv", "-f", "-b", "-t", "-r", "-i", "1"]


class BtrfsOps:
    """Real filesystem operations. All methods raise CommandError on failure."""

    def __init__(self, progress_cb: Optional[ProgressCallback] = None):
        self._progress_cb = progress_cb
        self._use_pv = progress_cb is not None and shutil.which("pv") is not None
        # When pv is absent we still show progress via a built-in byte counter.
        self._count = progress_cb is not None and not self._use_pv

    # -- process helpers ------------------------------------------------

    def _run(self, cmd: Sequence[str], *, check: bool = True,
             capture: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(list(cmd), check=check, capture_output=capture, text=True)

    # -- local subvolumes --------------------------------------------

    def snapshot_ro(self, source: Path, dest: Path) -> None:
        self._run(["btrfs", "subvolume", "snapshot", "-r", str(source), str(dest)])

    def snapshot_rw(self, source: Path, dest: Path) -> None:
        """Writable snapshot - used when caches must be pruned before it is sent,
        then flipped read-only with set_readonly()."""
        self._run(["btrfs", "subvolume", "snapshot", str(source), str(dest)])

    def set_readonly(self, path: Path, value: bool = True) -> None:
        self._run(["btrfs", "property", "set", "-ts", str(path),
                   "ro", "true" if value else "false"])

    def prune_paths(self, root: Path, patterns: Sequence[str]) -> List[str]:
        return prune_paths(root, patterns, self.is_subvolume)

    def delete_subvolume(self, path: Path) -> None:
        self._run(["btrfs", "subvolume", "delete", str(path)])

    def is_subvolume(self, path: Path) -> bool:
        try:
            self._run(["btrfs", "subvolume", "show", str(path)])
            return True
        except CommandError:
            return False

    def is_btrfs(self, path: Path) -> bool:
        try:
            res = self._run(["findmnt", "-n", "-o", "FSTYPE", "-T", str(path)])
            return res.stdout.strip() == "btrfs"
        except (CommandError, FileNotFoundError):
            return False

    # -- send / receive pipelines ---------------------------------------
    #
    # A pipeline is a list of argv lists; stdout->stdin is wired between stages.
    # A stage whose argv[0] == "pv" has its stderr streamed to the progress
    # callback (line by line, \r-delimited). No shell involved.

    def run_pipeline(self, stages: List[Sequence[str]], final_stdout=None) -> int:
        procs: List[subprocess.Popen] = []
        readers: List[threading.Thread] = []
        prev_stdout = None
        try:
            for i, stage in enumerate(stages):
                is_last = i == len(stages) - 1
                is_pv = stage and stage[0] == "pv"
                p = subprocess.Popen(
                    list(stage),
                    stdin=prev_stdout,
                    stdout=(final_stdout if is_last and final_stdout is not None
                            else subprocess.PIPE),
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                procs.append(p)
                if prev_stdout is not None:
                    prev_stdout.close()
                prev_stdout = p.stdout
                if is_pv and self._progress_cb:
                    t = threading.Thread(target=self._pump_pv, args=(p,), daemon=True)
                    t.start()
                    readers.append(t)
        except FileNotFoundError:
            _kill_process_group(procs)
            raise

        try:
            for p in procs:
                p.wait()
        except BaseException:
            # Ctrl-C / SIGTERM mid-transfer: the stages are in their own session
            # and never saw the terminal's SIGINT, so kill them here instead of
            # leaving an orphaned `btrfs send | ssh` running.
            _kill_process_group(procs)
            for t in readers:
                t.join(timeout=1.0)
            raise
        for t in readers:
            t.join(timeout=1.0)

        for p in procs:
            if p.returncode not in (0, None):
                return p.returncode
        return procs[-1].returncode if procs else 0

    def _pump_pv(self, proc: subprocess.Popen) -> None:
        buf = b""
        try:
            while True:
                chunk = proc.stderr.read(64)
                if not chunk:
                    break
                buf += chunk
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    text = line.decode("utf-8", "replace").strip()
                    if text:
                        self._progress_cb(text)
        except (OSError, ValueError):
            pass

    def _send_argv(self, source: Path, parent: Optional[Path]) -> List[str]:
        send = ["btrfs", "send"]
        if parent:
            send += ["-p", str(parent)]
        send.append(str(source))
        return send

    def _send_pipe(self, send: Sequence[str], sink: Sequence[str],
                   final_stdout=None) -> int:
        """btrfs send | sink, with progress via pv when available, else a
        built-in byte counter, else plain."""
        if self._use_pv:
            return self.run_pipeline([send, _PV_ARGS, sink], final_stdout=final_stdout)
        if not self._count:
            return self.run_pipeline([send, sink], final_stdout=final_stdout)
        return self._run_counted(send, sink, final_stdout)

    def _run_counted(self, send: Sequence[str], sink: Sequence[str],
                     final_stdout) -> int:
        p_send = subprocess.Popen(list(send), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, start_new_session=True)
        p_sink = subprocess.Popen(list(sink), stdin=subprocess.PIPE,
                                  stdout=(final_stdout if final_stdout is not None
                                          else subprocess.PIPE),
                                  stderr=subprocess.PIPE, start_new_session=True)
        total = 0
        start = last = time.monotonic()
        try:
            while True:
                chunk = p_send.stdout.read(1 << 20)
                if not chunk:
                    break
                p_sink.stdin.write(chunk)
                total += len(chunk)
                now = time.monotonic()
                if now - last >= 1.0:
                    rate = total / max(now - start, 1e-6)
                    self._progress_cb(f"{_human(total)}  [{_human(rate)}/s]  "
                                      f"{int(now - start)}s")
                    last = now
        except (BrokenPipeError, OSError):
            pass
        except BaseException:
            _kill_process_group((p_send, p_sink))
            raise
        finally:
            for fh in (p_sink.stdin, p_send.stdout, p_send.stderr,
                       p_sink.stdout, p_sink.stderr):
                try:
                    if fh:
                        fh.close()
                except OSError:
                    pass
        p_send.wait()
        p_sink.wait()
        if total:
            self._progress_cb(f"{_human(total)} sent")
        return p_send.returncode or p_sink.returncode

    def send_local_receive(self, source: Path, parent: Optional[Path],
                           dest_dir: Path) -> int:
        return self._send_pipe(self._send_argv(source, parent),
                               ["btrfs", "receive", str(dest_dir)])

    def send_to_stream(self, source: Path, parent: Optional[Path],
                       out_file: Path, level: int = 3) -> int:
        with open(out_file, "wb") as fh:
            return self._send_pipe(self._send_argv(source, parent),
                                   ["zstd", f"-{level}", "-T0", "-c"], final_stdout=fh)

    def disk_usage_percent(self, path: Path) -> float:
        u = shutil.disk_usage(path)
        return u.used * 100.0 / u.total if u.total else 0.0

    # -- remote (SSH) helpers -----------------------------------------

    def ssh_capture(self, ssh_argv: Sequence[str], remote: str,
                    cmd_argv: Sequence[str], timeout: int = 25):
        """Run `cmd_argv` on `remote`; return the CompletedProcess."""
        return subprocess.run([*ssh_argv, remote, *cmd_argv],
                              capture_output=True, text=True, timeout=timeout)

    def send_ssh_receive(self, source: Path, parent: Optional[Path],
                         ssh_argv: Sequence[str], remote: str,
                         receive_argv: Sequence[str]) -> int:
        sink = [*ssh_argv, remote, *receive_argv]
        return self._send_pipe(self._send_argv(source, parent), sink)

    def push_tree(self, local_dir: Path, ssh_argv: Sequence[str],
                  remote: str, remote_dir: str, timeout: int = 120) -> tuple:
        """tar the local dir and untar it into remote_dir over ssh, giving up
        after `timeout` seconds so a stalled link (Tailscale hiccup) can't hang
        the whole backup. Returns (returncode, stderr_text); 124 == timed out.

        A missing or empty `local_dir` is reported as an error rather than a
        silent no-op success - an empty tar stream still extracts "successfully"
        on the receiving end, which used to hide the caller sending nothing."""
        local_dir = Path(local_dir)
        if not local_dir.is_dir() or not any(local_dir.iterdir()):
            return 1, f"nothing to push: {local_dir} is missing or empty"
        try:
            mk = subprocess.run([*ssh_argv, remote, "mkdir", "-p", remote_dir],
                                capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return 124, "timed out creating remote dir"
        if mk.returncode != 0:
            return mk.returncode, mk.stderr.strip()
        tar = subprocess.Popen(
            ["tar", "-C", str(local_dir), "-cf", "-", "."],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        recv = subprocess.Popen(
            [*ssh_argv, remote, "tar", "-C", remote_dir, "-xf", "-"],
            stdin=tar.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        if tar.stdout:
            tar.stdout.close()
        try:
            _, err = recv.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group((recv, tar))
            return 124, f"timed out after {timeout}s"
        except BaseException:
            _kill_process_group((recv, tar))
            raise
        tar.wait()
        if tar.returncode != 0:
            tar_err = (tar.stderr.read() or b"").decode("utf-8", "replace").strip() \
                if tar.stderr else ""
            return tar.returncode, tar_err or "local tar failed"
        return recv.returncode, (err.decode("utf-8", "replace").strip() if err else "")
