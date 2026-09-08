"""
Thin, injectable wrappers around the `btrfs`, `zstd` and `ssh` commands.

Every call is built as an argv list - never a shell string - so paths and config
values can never be interpreted by a shell (see issue #9). `BtrfsOps` is the real
implementation; tests pass a fake with the same surface.
"""
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

CommandError = subprocess.CalledProcessError


class BtrfsOps:
    """Real filesystem operations. All methods raise CommandError on failure."""

    # -- process helpers ------------------------------------------------

    def _run(self, cmd: Sequence[str], *, check: bool = True,
             capture: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            list(cmd),
            check=check,
            capture_output=capture,
            text=True,
        )

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
    # A pipeline is a list of argv lists. We wire stdout->stdin between them and
    # wait on all, returning the exit code of the last process (or the first
    # non-zero one). No shell involved.

    def run_pipeline(self, stages: List[Sequence[str]],
                     final_stdout=None) -> int:
        procs: List[subprocess.Popen] = []
        prev_stdout = None
        try:
            for i, stage in enumerate(stages):
                is_last = i == len(stages) - 1
                procs.append(subprocess.Popen(
                    list(stage),
                    stdin=prev_stdout,
                    stdout=(final_stdout if is_last and final_stdout is not None
                            else subprocess.PIPE),
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                ))
                if prev_stdout is not None:
                    prev_stdout.close()
                prev_stdout = procs[-1].stdout
        except FileNotFoundError:
            for p in procs:
                p.kill()
            raise

        for p in procs:
            p.wait()
        # first failure wins, else the last stage's code
        for p in procs:
            if p.returncode not in (0, None):
                return p.returncode
        return procs[-1].returncode if procs else 0

    def send_local_receive(self, source: Path, parent: Optional[Path],
                           dest_dir: Path) -> int:
        send = ["btrfs", "send"]
        if parent:
            send += ["-p", str(parent)]
        send.append(str(source))
        return self.run_pipeline([send, ["btrfs", "receive", str(dest_dir)]])

    def send_to_stream(self, source: Path, parent: Optional[Path],
                       out_file: Path, level: int = 3) -> int:
        send = ["btrfs", "send"]
        if parent:
            send += ["-p", str(parent)]
        send.append(str(source))
        with open(out_file, "wb") as fh:
            return self.run_pipeline(
                [send, ["zstd", f"-{level}", "-T0", "-c"]], final_stdout=fh,
            )

    def send_ssh_receive(self, source: Path, parent: Optional[Path],
                         ssh_args: List[str], remote: str,
                         remote_receive_cmd: List[str]) -> int:
        send = ["btrfs", "send"]
        if parent:
            send += ["-p", str(parent)]
        send.append(str(source))
        ssh = ["ssh", *ssh_args, remote, *remote_receive_cmd]
        return self.run_pipeline([send, ssh])

    def disk_usage_percent(self, path: Path) -> float:
        import shutil
        u = shutil.disk_usage(path)
        return u.used * 100.0 / u.total if u.total else 0.0
