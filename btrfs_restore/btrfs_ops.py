"""
Thin, injectable wrappers around the `btrfs`, `zstd`, `pv` and `ssh` commands.

Every call is built as an argv list - never a shell string - so paths and config
values can never be interpreted by a shell (see issue #9). `BtrfsOps` is the real
implementation; tests pass a fake with the same surface.
"""
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024

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
            for p in procs:
                p.kill()
            raise

        for p in procs:
            p.wait()
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
        finally:
            try:
                p_sink.stdin.close()
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

    def send_ssh_receive(self, source: Path, parent: Optional[Path],
                         ssh_args: List[str], remote: str,
                         remote_receive_cmd: List[str]) -> int:
        return self._send_pipe(self._send_argv(source, parent),
                               ["ssh", *ssh_args, remote, *remote_receive_cmd])

    def disk_usage_percent(self, path: Path) -> float:
        u = shutil.disk_usage(path)
        return u.used * 100.0 / u.total if u.total else 0.0
