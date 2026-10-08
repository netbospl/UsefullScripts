#!/usr/bin/env python3
"""Safe regression checks for the Bash backup TUI; never starts a backup."""
from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "rsync-backup-tui.sh"
CORES = sorted(REPO.glob("rsync-backup-tui-*-core.sh"))


def receive(master: int, proc: subprocess.Popen[bytes], needle: bytes, timeout: float = 5) -> bytes:
    data = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                data.extend(os.read(master, 65536))
            except OSError:
                break
            if needle in data:
                break
        elif proc.poll() is not None:
            break
    return bytes(data)


def start_tui(rows: int, cols: int) -> tuple[int, subprocess.Popen[bytes]]:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = {**os.environ, "TERM": "xterm"}

    def make_controlling_tty() -> None:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(slave, os.getpgrp())

    proc = subprocess.Popen(
        [str(APP)], stdin=slave, stdout=slave, stderr=slave, cwd="/",
        env=env, close_fds=True, preexec_fn=make_controlling_tty,
    )
    os.close(slave)
    return master, proc


def stop_group(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def test_core_helpers() -> None:
    code = r'''source <(sed '$d' "$BACKUP_TUI_DIR/rsync-backup-tui-network-core.sh")
[[ $(kindof "$FIXTURE_DIR/file with spaces") == file ]]
[[ $(kindof "$FIXTURE_DIR/directory with spaces") == directory ]]
[[ $(media remote:/export fuse.sshfs) == NETWORK ]]
lsblk(){
  case "$*" in
    '-nrpo NAME,TYPE,SIZE') printf '/dev/fake1 part 100G\n/dev/fake2 part 50G\n';;
    '-no FSTYPE /dev/fake1') printf 'ext4\n';;
    '-no FSTYPE /dev/fake2') printf 'ntfs\n';;
    '-no MOUNTPOINTS /dev/fake1') :;;
    '-no MOUNTPOINTS /dev/fake2') printf '/mnt/already-mounted\n';;
  esac
}
devices=$(unmounted_devices)
[[ $devices == *'/dev/fake1'* && $devices != *'/dev/fake2'* ]]
SRC_KIND=directory; DFS=fuse.sshfs; DMEDIA=NETWORK
ui(){ :; }; waitkey(){ :; }
choose(){ CHOOSE_RESULT=$2; }
strategy
[[ $STRAT == tar.zst ]]
[[ -n $(mountinfo "$FIXTURE_DIR") ]]
SFS=btrfs; EX=(); fs_ex
[[ " ${EX[*]} " == *' /.snapshots/*** '* ]]
SRC_KIND=directory; choose(){ printf '%s' "$*" > "$CAPTURE_OPTIONS"; CHOOSE_RESULT=0; }
transport_choose
[[ $TRANSPORT == local ]]
SRC="$FIXTURE_DIR/directory with spaces"; STRAT=tar.zst; GPG_PASS=0
name=$(suggestname); [[ $name == 'directory with spaces-'*'.tar.zst' ]]
findmnt(){ printf '/mnt/nfs server:/export nfs4 rw,relatime\n'; }
ui(){ :; }; waitkey(){ :; }; DST=/mnt/nfs; nfs_probe
[[ $DST_IS_NFS == 1 && $NFS_FSTYPE == nfs4 && $NFS_SOURCE == server:/export ]]
findmnt(){
  if [[ " $* " == *' -o TARGET,SOURCE,FSTYPE '* ]]; then
    if [[ ${REMOTE_TEST_SINGLE:-0} == 1 ]]; then printf '/mnt/nfs server:/export nfs4\n'; else printf '/mnt/nfs server:/export nfs4\n/mnt/sshfs user@host:/share fuse.sshfs\n/mnt/local /dev/sdb1 ext4\n'; fi
  else
    printf '/mnt/nfs server:/export nfs4 rw,relatime\n'
  fi
}
remote=$(remote_mounts)
[[ $remote == *'/mnt/nfs'* && $remote == *'/mnt/sshfs'* && $remote != *'/mnt/local'* ]]
SRC=/mnt/nfs/source; DST=/mnt/sshfs/backup
if critical_mounts; then exit 41; fi
LOG="$FIXTURE_DIR/test.log"; :>"$CAPTURE_UNMOUNT"
REMOTE_TEST_SINGLE=1
yes_count=0
yes(){ yes_count=$((yes_count+1)); [[ $yes_count == 1 ]]; }
SRC=/home/user/data; DST=/mnt/local/backup
if critical_mounts; then exit 42; fi
yes_count=0
umount(){ printf '%s\n' "$*" >> "$CAPTURE_UNMOUNT"; }
offer_remote_unmount
[[ ! -s $CAPTURE_UNMOUNT && $yes_count == 2 ]]
yes_count=0
yes(){ yes_count=$((yes_count+1)); return 0; }
offer_remote_unmount
[[ $(<"$CAPTURE_UNMOUNT") == *'/mnt/nfs'* && $yes_count == 2 ]]
DMEDIA=NETWORK; DST=$FIXTURE_DIR; DFS=fuse.sshfs; DLINK=0; SLINK=0
io_bench
[[ ! -e $FIXTURE_DIR/.backup-io-$$ ]]
ui(){ :; }; choose(){ CHOOSE_RESULT=1; }
STRAT=tar.zst; SRC_KIND=directory; OUT="$FIXTURE_DIR/existing.tar.zst"
printf 'keep' > "$OUT"
resolve_out "fresh.tar.zst"
[[ $OUT != "$FIXTURE_DIR/existing.tar.zst" && $(<"$FIXTURE_DIR/existing.tar.zst") == keep ]]
if runlog expected-failure false; then exit 41; else [[ $? == 1 ]]; fi
'''
    with tempfile.TemporaryDirectory(prefix="backup tui test ") as tmp:
        root = Path(tmp)
        (root / "file with spaces").write_text("fixture", encoding="utf-8")
        (root / "directory with spaces").mkdir()
        options = root / "transport-options.txt"
        unmount_capture = root / "unmount-calls.txt"
        env = {
            **os.environ,
            "BACKUP_TUI_DIR": str(REPO),
            "FIXTURE_DIR": str(root),
            "CAPTURE_OPTIONS": str(options),
            "CAPTURE_UNMOUNT": str(unmount_capture),
        }
        result = subprocess.run(["bash", "-c", code], cwd="/", env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        displayed = options.read_text(encoding="utf-8")
        for command, label in (("ssh", "Direct rsync over SSH"), ("scp", "SCP result"), ("sftp", "SFTP upload")):
            if shutil.which(command):
                assert label in displayed, f"missing {label} transport option"


def test_confirmation_gate() -> None:
    with tempfile.TemporaryDirectory(prefix="backup source with spaces ") as tmp:
        source = Path(tmp) / f"source dir {os.getpid()}"
        source.mkdir()
        (source / "sample.txt").write_text("synthetic test data", encoding="utf-8")
        master, proc = start_tui(24, 100)
        try:
            output = receive(master, proc, b"Source file or directory")
            os.write(master, f"{source}\n".encode())
            output += receive(master, proc, b"Press Enter to continue")
            os.write(master, b"\n")
            output += receive(master, proc, b"Press Enter to continue")
            os.write(master, b"\n")
            output += receive(master, proc, b"Destination transport")
            os.write(master, b"1\n")
            output += receive(master, proc, b"Custom mounted path")
            match = re.search(rb"\s*(\d+)\) Custom mounted path", output)
            assert match, "custom mountpoint option was not listed"
            os.write(master, match.group(1) + b"\n")
            output += receive(master, proc, b"Mountpoint")
            os.write(master, b"/tmp\n")
            output += receive(master, proc, b"Toggle number, or Enter to accept")
            os.write(master, b"\n")
            output += receive(master, proc, b"Run safe zstd CPU benchmark")
            os.write(master, b"n\n")
            output += receive(master, proc, b"Run destination I/O benchmark")
            os.write(master, b"n\n")
            output += receive(master, proc, b"Backup format")
            os.write(master, b"1\n")
            output += receive(master, proc, b"Press Enter to continue")
            os.write(master, b"\n")
            for prompt in (
                b"Preserve ACLs", b"Preserve extended attributes", b"Preserve hard links",
                b"Preserve numeric UID/GID",
            ):
                output += receive(master, proc, prompt)
                os.write(master, b"\n")
            output += receive(master, proc, b"Enable destructive mirror deletions")
            os.write(master, b"n\n")
            output += receive(master, proc, b"Confirmation 1/2")
            os.write(master, b"y\n")
            output += receive(master, proc, b"Confirmation 2/2")
            os.write(master, b"n\n")
            rc = proc.wait(timeout=5)
            assert b"FINAL PLAN" in output, f"final plan was not shown: {output!r}"
            assert rc != 0, "refusal at confirmation 2 should cancel the flow"
            log = Path.home() / ".local/state/linux-backup-tui" / f"session-*-{proc.pid}.log"
            logs = list(log.parent.glob(log.name))
            assert logs and "OP_START" not in logs[0].read_text(encoding="utf-8"), "backup operation started despite refusal"
            output_path = Path("/tmp") / f"{source.name}-backup"
            assert not output_path.exists(), "fixture output was created"
        finally:
            stop_group(proc)
            os.close(master)


def main() -> None:
    for path in [APP, *CORES]:
        subprocess.run(["bash", "-n", str(path)], check=True)
    subprocess.run([str(APP), "--self-test"], cwd="/", check=True)
    test_core_helpers()


    master, proc = start_tui(10, 40)
    try:
        small = receive(master, proc, b"Terminal too small")
        rc = proc.wait(timeout=2)
        assert b"Terminal too small" in small, "small terminal was not rejected clearly"
        assert rc == 2, f"small-terminal exit code was {rc}, expected 2"
    finally:
        stop_group(proc)
        os.close(master)

    master, proc = start_tui(24, 100)
    try:
        data = receive(master, proc, b"Unmount ", timeout=1)
        while b"Unmount " in data and b"Source file or directory" not in data:
            os.write(master, b"n\n")
            chunk = receive(master, proc, b"Unmount ", timeout=0.5)
            data += chunk
            if b"Unmount " not in chunk:
                break
        if b"Detected unmounted" not in data and b"Source file or directory" not in data:
            data += receive(master, proc, b"Detected unmounted", timeout=1)
        if b"Detected unmounted" in data:
            os.write(master, b"\n")
        if b"Source file or directory" not in data:
            data += receive(master, proc, b"Source file or directory")
        assert b"Source file or directory" in data, "source prompt did not render"
        os.write(master, b"/tmp\n")
        data += receive(master, proc, b"Press Enter to continue")
        assert b"Source FS:" in data, "source summary did not render"
        os.write(master, b"\n")
        data += receive(master, proc, b"Press Enter to continue")
        os.write(master, b"\n")
        data += receive(master, proc, b"Destination transport")
        assert b"Destination transport" in data, f"transport menu did not render: {data!r}"
        data += receive(master, proc, b"Choice [1]:")
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            rc = proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            stop_group(proc)
            raise AssertionError("Ctrl+C did not stop the TUI")
        assert rc == 130, f"interrupt exit code was {rc}, expected 130"
    finally:
        stop_group(proc)
        os.close(master)

    print("Backup TUI regression checks: PASS (no backup operation executed)")


if __name__ == "__main__":
    main()
