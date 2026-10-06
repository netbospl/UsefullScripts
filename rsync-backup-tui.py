#!/usr/bin/env python3
"""Interactive Linux backup TUI.

Features:
- full-screen curses UI (redrawn in place)
- interactive source/destination selection
- mount/device detection with media classification and exclusion selector
- adaptive zstd CPU benchmark in tmpfs only
- strategy advisor: rsync, tar.zst, ZIP, raw .img/.img.zst where applicable
- final plan summary before any backup action
- sudo requested only when the selected action needs it
"""
from __future__ import annotations

import curses
import json
import os
import queue
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

APP_NAME = "Linux Backup TUI"
DEFAULT_SOURCE = "/"
PREFERRED_DEST = "/run/media/netbos/ext4HDD"
STATE_DIR = Path.home() / ".local/state/linux-backup-tui"

VIRTUAL_FS = {
    "autofs", "bpf", "cgroup", "cgroup2", "configfs", "debugfs", "devpts",
    "devtmpfs", "efivarfs", "fusectl", "hugetlbfs", "mqueue", "proc", "pstore",
    "ramfs", "securityfs", "sysfs", "tmpfs", "tracefs",
}
WINDOWS_FS = {"exfat", "fuseblk", "ntfs", "ntfs3", "vfat"}
LOCAL_FS = {
    "btrfs", "ext2", "ext3", "ext4", "exfat", "f2fs", "fuseblk", "ntfs",
    "ntfs3", "vfat", "xfs",
}
ROOT_EXCLUSIONS = [
    "/dev/***", "/proc/***", "/sys/***", "/run/***", "/tmp/***", "/mnt/***",
    "/media/***", "/var/cache/***", "/var/tmp/***", "/var/log/journal/***",
    "/var/lib/systemd/coredump/***", "/home/*/.cache/***",
    "/home/*/.local/share/Trash/***", "/home/*/.local/share/Steam/appcache/***",
    "/home/*/.local/share/Steam/logs/***", "/home/*/.config/*/Cache/***",
    "/home/*/.config/*/cache/***", "/home/*/.mozilla/firefox/*/cache2/***",
    "/home/*/.thumbnails/***", "/home/*/.Trash*/***", "/home/*/tmp/***",
    "/home/*/Temp/***", "/home/*/Downloads/*.part",
    "/home/*/Downloads/*.crdownload", "/root/.cache/***", "/lost+found",
    "/swapfile", "/download/***",
]
GENERIC_EXCLUSIONS = [
    "/.cache/***", "**/.cache/***", "/Cache/***", "**/Cache/***",
    "/cache/***", "**/cache/***", "/.local/share/Trash/***", "**/.local/share/Trash/***",
    "/.Trash*/***", "**/.Trash*/***", "/tmp/***", "**/tmp/***",
    "/Temp/***", "**/Temp/***", "**/Downloads/*.part", "**/Downloads/*.crdownload",
]
PROGRESS_RE = re.compile(r"^\s*([0-9][0-9,]*)\s+(\d{1,3})%\s+([^\s]+/s)\s+([0-9:]+)")
XFR_RE = re.compile(r"xfr#(\d+)")
CHECK_RE = re.compile(r"(?:to-chk|ir-chk)=(\d+)/(\d+)")


@dataclass
class MountInfo:
    target: str
    source: str
    fstype: str
    options: str
    total: int = 0
    free: int = 0
    disk: str = ""
    transport: str = ""
    rota: str = "?"
    model: str = ""
    media: str = "Unknown"


@dataclass
class MountSelection:
    mount: MountInfo
    exclude: bool
    locked: bool = False
    reason: str = ""


@dataclass
class BenchmarkResult:
    available: bool = False
    tmpfs: str = ""
    sample_bytes: int = 0
    repetitions: int = 0
    one_thread_mib_s: float = 0.0
    all_threads_mib_s: float = 0.0
    cpu_threads: int = 1
    error: str = ""


@dataclass
class Strategy:
    kind: str
    label: str
    extension: str = ""
    zstd_level: int = 3
    zstd_threads: str = "0"
    recommended: bool = False
    reason: str = ""


@dataclass
class ProgressState:
    percent: int = 0
    bytes_done: int = 0
    speed: str = "—"
    eta: str = "—"
    transferred_files: int = 0
    remaining_files: int | None = None
    total_files: int | None = None
    current: str = "Waiting…"
    paused: bool = False
    started: float = 0.0


def run_text(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def human_bytes(value: int) -> str:
    n = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024 or unit == "PiB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def safe_addstr(win: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        h, w = win.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            win.addnstr(y, x, str(text).replace("\n", " "), max(0, w - x - 1), attr)
    except curses.error:
        pass


def draw_frame(win: curses.window, title: str, footer: str = "") -> tuple[int, int]:
    win.erase()
    h, w = win.getmaxyx()
    if h < 18 or w < 76:
        safe_addstr(win, max(0, h // 2), max(0, (w - 28) // 2), "Resize terminal to at least 76×18", curses.A_BOLD)
        win.refresh()
        return h, w
    try:
        win.box()
    except curses.error:
        pass
    safe_addstr(win, 1, max(1, (w - len(title) - 2) // 2), f" {title} ", curses.A_BOLD)
    safe_addstr(win, 2, 1, "─" * max(0, w - 2))
    if footer:
        safe_addstr(win, h - 3, 1, "─" * max(0, w - 2))
        safe_addstr(win, h - 2, max(1, (w - len(footer)) // 2), footer, curses.A_DIM)
    return h, w


def choose_dialog(win: curses.window, title: str, choices: list[str], default: int = 0, footer: str | None = None) -> int:
    if not choices:
        raise ValueError("empty choices")
    idx = min(max(default, 0), len(choices) - 1)
    offset = 0
    footer = footer or "↑/↓ select   Enter choose   Esc cancel"
    while True:
        h, w = draw_frame(win, title, footer)
        body = max(1, h - 8)
        if idx < offset:
            offset = idx
        if idx >= offset + body:
            offset = idx - body + 1
        for row, i in enumerate(range(offset, min(len(choices), offset + body)), start=4):
            attr = curses.A_REVERSE | curses.A_BOLD if i == idx else 0
            safe_addstr(win, row, 4, choices[i], attr)
        win.refresh()
        key = win.getch()
        if key in (curses.KEY_DOWN, ord("j")):
            idx = min(len(choices) - 1, idx + 1)
        elif key in (curses.KEY_UP, ord("k")):
            idx = max(0, idx - 1)
        elif key in (10, 13, curses.KEY_ENTER):
            return idx
        elif key == 27:
            raise KeyboardInterrupt


def input_dialog(win: curses.window, title: str, prompt: str, default: str) -> str:
    value = list(default)
    pos = len(value)
    curses.curs_set(1)
    try:
        while True:
            h, w = draw_frame(win, title, "Enter accept   Esc cancel")
            safe_addstr(win, 5, 4, prompt, curses.A_BOLD)
            width = max(10, w - 10)
            text = "".join(value)
            offset = max(0, pos - width + 1)
            safe_addstr(win, 7, 4, " " * width, curses.A_REVERSE)
            safe_addstr(win, 7, 4, text[offset:offset + width], curses.A_REVERSE)
            try:
                win.move(7, min(w - 6, 4 + pos - offset))
            except curses.error:
                pass
            win.refresh()
            key = win.get_wch()
            if key in ("\n", "\r", curses.KEY_ENTER):
                result = "".join(value).strip()
                if result:
                    return result
            elif key == "\x1b":
                raise KeyboardInterrupt
            elif key in (curses.KEY_BACKSPACE, "\b", "\x7f") and pos > 0:
                del value[pos - 1]
                pos -= 1
            elif key == curses.KEY_DC and pos < len(value):
                del value[pos]
            elif key == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif key == curses.KEY_RIGHT:
                pos = min(len(value), pos + 1)
            elif key == curses.KEY_HOME:
                pos = 0
            elif key == curses.KEY_END:
                pos = len(value)
            elif isinstance(key, str) and key.isprintable():
                value.insert(pos, key)
                pos += 1
    finally:
        curses.curs_set(0)


def scroll_dialog(win: curses.window, title: str, lines: Iterable[str], footer: str = "Enter back") -> None:
    lines = list(lines)
    offset = 0
    while True:
        h, w = draw_frame(win, title, footer)
        body = max(1, h - 7)
        for row, line in enumerate(lines[offset:offset + body], start=4):
            safe_addstr(win, row, 4, line)
        win.refresh()
        key = win.getch()
        if key in (10, 13, curses.KEY_ENTER, 27):
            return
        if key in (curses.KEY_DOWN, ord("j")):
            offset = min(max(0, len(lines) - body), offset + 1)
        elif key in (curses.KEY_UP, ord("k")):
            offset = max(0, offset - 1)
        elif key == curses.KEY_NPAGE:
            offset = min(max(0, len(lines) - body), offset + body)
        elif key == curses.KEY_PPAGE:
            offset = max(0, offset - body)


def confirm_dialog(win: curses.window, title: str, lines: Iterable[str], yes_label: str = "continue") -> bool:
    lines = list(lines)
    offset = 0
    while True:
        h, w = draw_frame(win, title, f"Y {yes_label}   N/Esc cancel   ↑/↓ scroll")
        body = max(1, h - 7)
        for row, line in enumerate(lines[offset:offset + body], start=4):
            safe_addstr(win, row, 4, line)
        win.refresh()
        key = win.getch()
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), 27):
            return False
        if key in (curses.KEY_DOWN, ord("j")):
            offset = min(max(0, len(lines) - body), offset + 1)
        elif key in (curses.KEY_UP, ord("k")):
            offset = max(0, offset - 1)


def root_parent_disk(device: str) -> str:
    if not device.startswith("/dev/"):
        return ""
    current = os.path.realpath(device.split("[", 1)[0])
    while True:
        parent = run_text(["lsblk", "-ndo", "PKNAME", current]).strip()
        if not parent:
            return current
        current = "/dev/" + parent


def classify_device(source: str, fstype: str) -> tuple[str, str, str, str, str]:
    if fstype in VIRTUAL_FS:
        return "", "", "?", "", "VIRTUAL"
    if fstype.startswith("fuse.") or source in {"portal", "protondrive"}:
        return "", "", "?", "", "FUSE"
    if fstype.startswith(("nfs", "cifs", "smb", "sshfs", "9p")) or source.startswith("//"):
        return "", "", "?", "", "NETWORK"
    if not source.startswith("/dev/"):
        return "", "", "?", "", fstype.upper() or "OTHER"
    disk = root_parent_disk(source)
    rota = run_text(["lsblk", "-ndo", "ROTA", disk]) or "?"
    transport = run_text(["lsblk", "-ndo", "TRAN", disk]).lower()
    model = run_text(["lsblk", "-ndo", "MODEL", disk])
    name = os.path.basename(disk).lower()
    if name.startswith("nvme") or transport == "nvme":
        media = "NVMe SSD"
    elif name.startswith("mmcblk") or transport in {"mmc", "sdio"}:
        media = "SD/eMMC flash"
    elif transport == "usb" and rota == "1":
        media = "USB HDD"
    elif transport == "usb" and rota == "0":
        media = "USB SSD/flash"
    elif transport in {"sata", "ata"} and rota == "1":
        media = "SATA HDD"
    elif transport in {"sata", "ata"} and rota == "0":
        media = "SATA SSD"
    elif rota == "1":
        media = "HDD"
    elif rota == "0":
        media = "SSD/flash"
    else:
        media = "Block device"
    return disk, transport, rota, model, media


def find_mounts() -> list[MountInfo]:
    raw = run_text(["findmnt", "-J", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"])
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    result: list[MountInfo] = []

    def walk(nodes: list[dict]) -> None:
        for node in nodes:
            target = str(node.get("target") or "")
            source = str(node.get("source") or "")
            fstype = str(node.get("fstype") or "")
            options = str(node.get("options") or "")
            if target:
                try:
                    usage = shutil.disk_usage(target)
                    total, free = usage.total, usage.free
                except OSError:
                    total = free = 0
                disk, transport, rota, model, media = classify_device(source, fstype)
                result.append(MountInfo(target, source, fstype, options, total, free, disk, transport, rota, model, media))
            walk(node.get("children") or [])

    walk(data.get("filesystems") or [])
    return list({m.target: m for m in result}.values())


def path_is_within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(parent)]) == os.path.realpath(parent)
    except ValueError:
        return False


def mount_for_path(path: str, mounts: list[MountInfo]) -> MountInfo | None:
    real = os.path.realpath(path)
    candidates: list[tuple[int, MountInfo]] = []
    for mount in mounts:
        target = os.path.realpath(mount.target)
        try:
            if os.path.commonpath([real, target]) == target:
                candidates.append((len(target), mount))
        except ValueError:
            pass
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def format_mount(m: MountInfo) -> str:
    model = f", {m.model.strip()}" if m.model.strip() else ""
    return f"{m.target}  [{m.source}, {m.fstype}, {m.media}, {human_bytes(m.free)} free{model}]"


def choose_destination(win: curses.window, mounts: list[MountInfo]) -> str:
    candidates = [
        m for m in mounts
        if m.target != "/" and m.fstype in LOCAL_FS and m.fstype not in VIRTUAL_FS
        and not m.fstype.startswith("fuse.") and "rw" in m.options.split(",")
    ]
    candidates.sort(key=lambda m: (m.target != PREFERRED_DEST, m.target))
    labels = [format_mount(m) for m in candidates] + ["Enter a custom mounted destination path…"]
    default = next((i for i, m in enumerate(candidates) if m.target == PREFERRED_DEST), 0)
    idx = choose_dialog(win, "Destination device / mount", labels, default)
    if idx == len(candidates):
        return os.path.abspath(os.path.expanduser(input_dialog(win, "Destination", "Mounted destination path:", PREFERRED_DEST)))
    return candidates[idx].target


def initial_mount_selections(source: str, dest_mount: str, mounts: list[MountInfo]) -> list[MountSelection]:
    source_real = os.path.realpath(source)
    source_mount = mount_for_path(source, mounts)
    source_disk = source_mount.disk if source_mount else ""
    selections: list[MountSelection] = []
    for m in sorted(mounts, key=lambda x: (len(x.target), x.target)):
        target = os.path.realpath(m.target)
        if target == source_real or not path_is_within(target, source_real):
            continue
        if target == os.path.realpath(dest_mount):
            selections.append(MountSelection(m, True, True, "backup destination"))
        elif m.fstype in VIRTUAL_FS:
            selections.append(MountSelection(m, True, True, "virtual/runtime"))
        elif m.media in {"FUSE", "NETWORK"}:
            selections.append(MountSelection(m, True, False, "external/FUSE/network"))
        elif source_disk and m.disk == source_disk:
            selections.append(MountSelection(m, False, False, "same system disk"))
        else:
            selections.append(MountSelection(m, True, False, "other mounted device"))
    return selections


def select_mount_exclusions(win: curses.window, selections: list[MountSelection]) -> list[MountSelection]:
    if not selections:
        return selections
    idx = 0
    offset = 0
    while True:
        h, w = draw_frame(win, "Mounted devices inside source", "Space toggle exclude/include   Enter accept   Esc cancel")
        body = max(1, h - 9)
        safe_addstr(win, 3, 4, "[X] excluded   [ ] included   [!] mandatory exclusion", curses.A_BOLD)
        if idx < offset:
            offset = idx
        if idx >= offset + body:
            offset = idx - body + 1
        for row, i in enumerate(range(offset, min(len(selections), offset + body)), start=5):
            s = selections[i]
            mark = "!" if s.locked else "X" if s.exclude else " "
            label = f"[{mark}] {s.mount.target}  {s.mount.media} / {s.mount.fstype}  ← {s.mount.source}  ({s.reason})"
            attr = curses.A_REVERSE | curses.A_BOLD if i == idx else 0
            safe_addstr(win, row, 4, label, attr)
        win.refresh()
        key = win.getch()
        if key in (curses.KEY_DOWN, ord("j")):
            idx = min(len(selections) - 1, idx + 1)
        elif key in (curses.KEY_UP, ord("k")):
            idx = max(0, idx - 1)
        elif key == ord(" "):
            if not selections[idx].locked:
                selections[idx].exclude = not selections[idx].exclude
        elif key in (10, 13, curses.KEY_ENTER):
            return selections
        elif key == 27:
            raise KeyboardInterrupt


def mount_patterns(source: str, selections: list[MountSelection]) -> list[str]:
    result: list[str] = []
    source_real = os.path.realpath(source)
    for s in selections:
        if not s.exclude:
            continue
        rel = os.path.relpath(os.path.realpath(s.mount.target), source_real)
        if rel != ".":
            result.append("/" + rel.strip("/") + "/***")
    return result


def edit_exclusions(win: curses.window, exclusions: list[str]) -> list[str]:
    items = list(dict.fromkeys(exclusions))
    idx = 0
    offset = 0
    while True:
        h, w = draw_frame(win, "Path exclusions", "A add   D remove   Enter accept   Esc cancel")
        body = max(1, h - 9)
        safe_addstr(win, 3, 4, f"{len(items)} active rsync-style patterns", curses.A_BOLD)
        if items:
            idx = min(idx, len(items) - 1)
            if idx < offset:
                offset = idx
            if idx >= offset + body:
                offset = idx - body + 1
            for row, i in enumerate(range(offset, min(len(items), offset + body)), start=5):
                safe_addstr(win, row, 4, items[i], curses.A_REVERSE if i == idx else 0)
        win.refresh()
        key = win.getch()
        if key in (10, 13, curses.KEY_ENTER):
            return items
        if key == 27:
            raise KeyboardInterrupt
        if key in (ord("a"), ord("A")):
            new = input_dialog(win, "Add exclusion", "rsync exclusion pattern:", "")
            if new and new not in items:
                items.append(new)
                idx = len(items) - 1
        elif key in (ord("d"), ord("D"), curses.KEY_DC) and items:
            del items[idx]
            idx = max(0, idx - 1)
        elif key in (curses.KEY_DOWN, ord("j")) and items:
            idx = min(len(items) - 1, idx + 1)
        elif key in (curses.KEY_UP, ord("k")) and items:
            idx = max(0, idx - 1)


def mem_available_bytes() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def choose_tmpfs(mounts: list[MountInfo]) -> MountInfo | None:
    candidates = [m for m in mounts if m.fstype == "tmpfs" and "rw" in m.options.split(",") and os.access(m.target, os.W_OK)]
    preferred = ["/dev/shm", f"/run/user/{os.getuid()}"]
    for p in preferred:
        found = next((m for m in candidates if m.target == p), None)
        if found:
            return found
    return max(candidates, key=lambda m: m.free, default=None)


def adaptive_sample_size(tmp_free: int, mem_free: int, cpu_threads: int) -> int:
    cap = min(256 << 20, int(tmp_free * 0.10), int(mem_free * 0.03) if mem_free else 256 << 20)
    if cpu_threads <= 2:
        target = 32 << 20
    elif cpu_threads <= 4:
        target = 64 << 20
    elif cpu_threads <= 8:
        target = 96 << 20
    else:
        target = 128 << 20
    return max(0, min(cap, target))


def create_benchmark_sample(path: str, size: int) -> None:
    pattern = (b"Linux backup benchmark: config log database source code text\n" * 2048)
    random_chunk = os.urandom(1 << 20)
    written = 0
    with open(path, "wb", buffering=0) as f:
        while written < size:
            chunk = pattern if (written // (1 << 20)) % 4 != 3 else random_chunk
            chunk = chunk[: min(len(chunk), size - written)]
            f.write(chunk)
            written += len(chunk)


def zstd_speed(sample: str, size: int, threads: str) -> tuple[float, int]:
    speeds: list[float] = []
    reps = 0
    total_time = 0.0
    while reps < 4 and total_time < 1.2:
        start = time.monotonic()
        with open(os.devnull, "wb") as sink:
            proc = subprocess.run(["zstd", "-q", "-3", f"-T{threads}", "-c", sample], stdout=sink, stderr=subprocess.DEVNULL, timeout=12)
        elapsed = max(0.001, time.monotonic() - start)
        if proc.returncode != 0:
            raise RuntimeError(f"zstd returned {proc.returncode}")
        speeds.append((size / (1024 * 1024)) / elapsed)
        total_time += elapsed
        reps += 1
    return statistics.median(speeds), reps


def benchmark_zstd(win: curses.window, mounts: list[MountInfo]) -> BenchmarkResult:
    result = BenchmarkResult(cpu_threads=os.cpu_count() or 1)
    if shutil.which("zstd") is None:
        result.error = "zstd is not installed"
        return result
    tmp = choose_tmpfs(mounts)
    if not tmp:
        result.error = "no writable tmpfs was found"
        return result
    mem_free = mem_available_bytes()
    try:
        usage = shutil.disk_usage(tmp.target)
        tmp_free = usage.free
    except OSError:
        result.error = "cannot inspect tmpfs free space"
        return result
    size = adaptive_sample_size(tmp_free, mem_free, result.cpu_threads)
    if size < (16 << 20):
        result.error = f"not enough safe tmpfs/RAM headroom ({human_bytes(size)} available for sample)"
        return result
    h, w = draw_frame(win, "Compression benchmark", "RAM/tmpfs only — no HDD/SSD benchmark files")
    safe_addstr(win, 5, 4, f"tmpfs: {tmp.target}", curses.A_BOLD)
    safe_addstr(win, 6, 4, f"Sample: {human_bytes(size)} (adaptive safety cap)")
    safe_addstr(win, 7, 4, f"CPU threads: {result.cpu_threads}")
    safe_addstr(win, 9, 4, "Testing zstd level 3: 1 thread vs all threads…")
    win.refresh()
    workdir = tempfile.mkdtemp(prefix="backup-tui-bench-", dir=tmp.target)
    sample = os.path.join(workdir, "sample.bin")
    try:
        create_benchmark_sample(sample, size)
        one, reps1 = zstd_speed(sample, size, "1")
        many, reps2 = zstd_speed(sample, size, "0")
        result.available = True
        result.tmpfs = tmp.target
        result.sample_bytes = size
        result.repetitions = min(reps1, reps2)
        result.one_thread_mib_s = one
        result.all_threads_mib_s = many
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result.error = str(exc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return result


def approx_media_mib_s(media: str) -> int | None:
    table = {
        "NVMe SSD": 1400, "SATA SSD": 450, "USB SSD/flash": 220, "SSD/flash": 300,
        "SATA HDD": 150, "USB HDD": 110, "HDD": 140, "SD/eMMC flash": 60,
    }
    return table.get(media)


def strategy_reason(src: MountInfo | None, dst: MountInfo | None, bench: BenchmarkResult) -> tuple[str, str]:
    sm = approx_media_mib_s(src.media) if src else None
    dm = approx_media_mib_s(dst.media) if dst else None
    bottleneck = min(x for x in (sm, dm) if x is not None) if any(x is not None for x in (sm, dm)) else None
    if dst and dst.fstype in WINDOWS_FS:
        return "tar.zst", "Windows-readable filesystem detected; tar.zst keeps Linux metadata inside one portable file. ZIP is offered for Windows-first access."
    if dst and dst.media in {"USB HDD", "SATA HDD", "HDD", "SD/eMMC flash", "USB SSD/flash"} and bench.available:
        if bottleneck and bench.all_threads_mib_s > bottleneck * 1.25:
            return "tar.zst", f"Parallel zstd ({bench.all_threads_mib_s:.0f} MiB/s) is likely faster than the estimated storage bottleneck (~{bottleneck} MiB/s), so compression may reduce writes."
    return "rsync", "rsync is preferred for incremental backups, direct file access and efficient repeated runs."


def strategy_choices(src: MountInfo | None, dst: MountInfo | None, bench: BenchmarkResult) -> list[Strategy]:
    rec, reason = strategy_reason(src, dst, bench)
    rsync_label = "rsync directory — incremental, directly browsable"
    rsync_reason = reason if rec == "rsync" else "Best for repeated/incremental backups on Linux filesystems."
    if dst and dst.fstype in WINDOWS_FS:
        rsync_label = "rsync directory — advanced on Windows filesystem (Linux metadata may fail)"
        rsync_reason = "NTFS/exFAT/VFAT do not faithfully represent all Linux ownership, ACL, xattr and special-file metadata."
    choices = [Strategy("rsync", rsync_label, recommended=rec == "rsync", reason=rsync_reason)]
    if shutil.which("tar") and shutil.which("zstd"):
        choices.append(Strategy("tar.zst", "tar.zst archive — Linux metadata + parallel zstd", ".tar.zst", 3, "0", rec == "tar.zst", reason if rec == "tar.zst" else "Single compressed Linux-oriented archive."))
    if shutil.which("zip"):
        choices.append(Strategy("zip", "ZIP archive — Windows-first compatibility (reduced Linux metadata)", ".zip", recommended=False, reason="Easy native Windows access; not a faithful Linux system restore format."))
    if src and src.source.startswith("/dev/"):
        choices.append(Strategy("img", "raw .img — exact source partition image (advanced)", ".img", recommended=False, reason="Block-level image; mounted/live source may be inconsistent."))
        if shutil.which("zstd"):
            choices.append(Strategy("img.zst", "raw .img.zst — block image + parallel zstd (advanced)", ".img.zst", 3, "0", False, "Compressed block-level image; mounted/live source may be inconsistent."))
    return choices


def choose_strategy(win: curses.window, src: MountInfo | None, dst: MountInfo | None, bench: BenchmarkResult) -> Strategy:
    choices = strategy_choices(src, dst, bench)
    labels = []
    default = 0
    for i, s in enumerate(choices):
        star = "★ RECOMMENDED — " if s.recommended else ""
        labels.append(star + s.label)
        if s.recommended:
            default = i
    idx = choose_dialog(win, "Backup format / strategy", labels, default)
    selected = choices[idx]
    detail = [selected.label, "", selected.reason]
    if selected.kind in {"tar.zst", "img.zst"} and bench.available:
        detail += ["", f"zstd L3 benchmark: 1T {bench.one_thread_mib_s:.0f} MiB/s; all threads {bench.all_threads_mib_s:.0f} MiB/s"]
    if selected.kind in {"img", "img.zst"}:
        detail += ["", "WARNING: this is a raw block image, not a file backup.", "Imaging a mounted/read-write source can produce an inconsistent image."]
        if not confirm_dialog(win, "Advanced image mode", detail, "use image mode"):
            return choose_strategy(win, src, dst, bench)
    else:
        scroll_dialog(win, "Strategy details", detail)
    return selected


def configure_compression(win: curses.window, strategy: Strategy, bench: BenchmarkResult) -> Strategy:
    if strategy.kind not in {"tar.zst", "img.zst"}:
        return strategy
    cpu = max(1, bench.cpu_threads or (os.cpu_count() or 1))
    half = max(1, cpu // 2)
    labels = [f"All CPU threads ({cpu}) — recommended for fastest compression", f"Half CPU threads ({half}) — leave headroom for desktop use", "Single thread — lowest CPU pressure"]
    default = 0 if (not bench.available or bench.all_threads_mib_s >= bench.one_thread_mib_s * 1.15) else 2
    idx = choose_dialog(win, "zstd parallelism", labels, default)
    strategy.zstd_threads = "0" if idx == 0 else str(half) if idx == 1 else "1"
    level_labels = ["Level 1 — fastest / lower compression", "Level 3 — balanced (recommended)", "Level 6 — smaller archive / more CPU"]
    li = choose_dialog(win, "zstd compression level", level_labels, 1)
    strategy.zstd_level = (1, 3, 6)[li]
    return strategy


def verify_setup(source: str, dest_mount: str, mounts: list[MountInfo]) -> list[str]:
    errors: list[str] = []
    if not os.path.isdir(source):
        errors.append(f"Source does not exist: {source}")
    if not os.path.isdir(dest_mount):
        errors.append(f"Destination mount does not exist: {dest_mount}")
    if not os.path.ismount(dest_mount):
        errors.append(f"Destination is not a mountpoint: {dest_mount}")
    dm = mount_for_path(dest_mount, mounts)
    if dm and "rw" not in dm.options.split(","):
        errors.append("Destination mount is read-only.")
    return errors


def normalized_output(dest_mount: str, name: str, strategy: Strategy) -> str:
    name = name.strip().strip("/")
    if strategy.extension and not name.lower().endswith(strategy.extension):
        name += strategy.extension
    return os.path.join(dest_mount, name)


def needs_sudo(source: str, output: str, strategy: Strategy) -> bool:
    if os.geteuid() == 0:
        return False
    if source == "/" or strategy.kind in {"img", "img.zst"}:
        return True
    if not os.access(source, os.R_OK | os.X_OK):
        return True
    parent = os.path.dirname(output) or "."
    if os.path.exists(parent) and not os.access(parent, os.W_OK):
        return True
    return False


def request_sudo(win: curses.window) -> bool:
    if os.geteuid() == 0:
        return True
    if not shutil.which("sudo"):
        scroll_dialog(win, "sudo required", ["sudo is not installed, but this operation requires elevated privileges."])
        return False
    if not confirm_dialog(win, "Privileges required", ["The selected backup needs elevated privileges.", "sudo will now ask for your password if needed."], "request sudo"):
        return False
    curses.def_prog_mode()
    curses.endwin()
    print("\nLinux Backup TUI: requesting sudo credentials…", flush=True)
    rc = subprocess.call(["sudo", "-v"])
    curses.reset_prog_mode()
    curses.curs_set(0)
    win.refresh()
    return rc == 0


def tar_exclude(pattern: str) -> str:
    p = pattern.lstrip("/")
    p = p.replace("/***", "").rstrip("/")
    return "./" + p if p else "."


def zip_exclude(pattern: str) -> str:
    p = pattern.lstrip("/").replace("/***", "/*")
    return p


def build_command(source: str, output: str, exclusions: list[str], strategy: Strategy, sudo: bool, src_mount: MountInfo | None) -> tuple[list[str], bool]:
    prefix = ["sudo", "-n"] if sudo and os.geteuid() != 0 else []
    if strategy.kind == "rsync":
        cmd = prefix + ["rsync", "-aAXHS", "--numeric-ids", "--info=progress2", "--out-format=%n", "--outbuf=L", "--stats"]
        for p in exclusions:
            cmd += ["--exclude", p]
        cmd += [source.rstrip("/") + "/" if source != "/" else "/", output.rstrip("/") + "/"]
        return cmd, False
    if strategy.kind == "tar.zst":
        tar_cmd = ["tar", "--acls", "--xattrs", "--numeric-owner", "-C", source, "-cpf", "-"]
        for p in exclusions:
            tar_cmd += ["--exclude", tar_exclude(p)]
        tar_cmd += ["."]
        zstd_cmd = ["zstd", "-q", f"-{strategy.zstd_level}", f"-T{strategy.zstd_threads}", "-o", output]
        shell = " ".join(shlex_quote(x) for x in prefix + tar_cmd) + " | " + " ".join(shlex_quote(x) for x in prefix + zstd_cmd)
        return ["bash", "-o", "pipefail", "-c", shell], True
    if strategy.kind == "zip":
        cmd = prefix + ["bash", "-c", "cd \"$1\" && shift && exec zip -r -q \"$1\" . \"${@:2}\"", "bash", source, output]
        for p in exclusions:
            cmd += ["-x", zip_exclude(p)]
        return cmd, True
    if strategy.kind in {"img", "img.zst"}:
        device = src_mount.source.split("[", 1)[0] if src_mount else ""
        if not device.startswith("/dev/"):
            raise RuntimeError("image mode requires a block-device source")
        if strategy.kind == "img":
            return prefix + ["dd", f"if={device}", f"of={output}", "bs=16M", "status=progress", "conv=fsync"], True
        dd = " ".join(shlex_quote(x) for x in prefix + ["dd", f"if={device}", "bs=16M", "status=progress"])
        z = " ".join(shlex_quote(x) for x in prefix + ["zstd", "-q", f"-{strategy.zstd_level}", f"-T{strategy.zstd_threads}", "-o", output])
        return ["bash", "-o", "pipefail", "-c", dd + " | " + z], True
    raise RuntimeError("unsupported strategy")


def shlex_quote(value: str) -> str:
    import shlex
    return shlex.quote(value)


def parse_rsync_line(line: str, state: ProgressState) -> None:
    cleaned = line.strip()
    if not cleaned:
        return
    m = PROGRESS_RE.match(cleaned)
    if m:
        state.bytes_done = int(m.group(1).replace(",", ""))
        state.percent = max(0, min(100, int(m.group(2))))
        state.speed = m.group(3)
        state.eta = m.group(4)
        x = XFR_RE.search(cleaned)
        if x:
            state.transferred_files = int(x.group(1))
        c = CHECK_RE.search(cleaned)
        if c:
            state.remaining_files = int(c.group(1)); state.total_files = int(c.group(2))
    elif not cleaned.startswith("sending incremental file list"):
        state.current = cleaned


def reader(proc: subprocess.Popen[bytes], q: queue.Queue[str], log: Path) -> None:
    buf = b""
    assert proc.stdout is not None
    with log.open("ab", buffering=0) as f:
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            f.write(chunk); buf += chunk
            parts = re.split(br"[\r\n]+", buf); buf = parts.pop() if parts else b""
            for part in parts:
                if part:
                    q.put(part.decode("utf-8", "replace"))
        if buf:
            q.put(buf.decode("utf-8", "replace"))


def draw_running(win: curses.window, strategy: Strategy, source: str, output: str, state: ProgressState, log: Path) -> None:
    h, w = draw_frame(win, APP_NAME, "P pause/resume   L log   Q cancel")
    safe_addstr(win, 4, 4, "Strategy", curses.A_BOLD); safe_addstr(win, 4, 18, strategy.label)
    safe_addstr(win, 5, 4, "Source", curses.A_BOLD); safe_addstr(win, 5, 18, source)
    safe_addstr(win, 6, 4, "Output", curses.A_BOLD); safe_addstr(win, 6, 18, output)
    if strategy.kind == "rsync":
        width = max(12, w - 16); filled = int(width * state.percent / 100)
        safe_addstr(win, 8, 6, "█" * filled + "░" * (width - filled))
        safe_addstr(win, 9, 6, f"{state.percent:3d}%  {human_bytes(state.bytes_done)}  {state.speed}  ETA {state.eta}", curses.A_BOLD)
    else:
        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 10) % 10]
        safe_addstr(win, 8, 6, f"{spin} archive/image operation running…", curses.A_BOLD)
    elapsed = int(time.monotonic() - state.started)
    safe_addstr(win, 11, 4, f"Elapsed: {time.strftime('%H:%M:%S', time.gmtime(elapsed))}")
    safe_addstr(win, 12, 4, f"Status: {'PAUSED' if state.paused else 'RUNNING'}", curses.A_BOLD)
    safe_addstr(win, 14, 4, "Current", curses.A_BOLD); safe_addstr(win, 14, 18, state.current)
    if h > 20:
        safe_addstr(win, 16, 4, "Log", curses.A_BOLD); safe_addstr(win, 16, 18, str(log), curses.A_DIM)


def run_operation(win: curses.window, command: list[str], strategy: Strategy, source: str, output: str) -> tuple[int, Path]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = STATE_DIR / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".log")
    Path(os.path.dirname(output) or ".").mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as f:
        f.write("# Command: " + " ".join(shlex_quote(x) for x in command) + "\n")
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, start_new_session=True, env={**os.environ, "LC_ALL": "C"})
    q: queue.Queue[str] = queue.Queue(); t = threading.Thread(target=reader, args=(proc, q, log), daemon=True); t.start()
    state = ProgressState(started=time.monotonic(), current="Starting…")
    win.nodelay(True)
    try:
        while proc.poll() is None:
            while True:
                try:
                    line = q.get_nowait(); state.current = line.strip() or state.current
                    if strategy.kind == "rsync": parse_rsync_line(line, state)
                except queue.Empty:
                    break
            draw_running(win, strategy, source, output, state, log); win.refresh()
            key = win.getch()
            if key in (ord("p"), ord("P")):
                try:
                    os.killpg(proc.pid, signal.SIGCONT if state.paused else signal.SIGSTOP); state.paused = not state.paused
                except ProcessLookupError: pass
            elif key in (ord("l"), ord("L")):
                win.nodelay(False); scroll_dialog(win, "Log", [str(log)]); win.nodelay(True)
            elif key in (ord("q"), ord("Q")):
                if state.paused:
                    try: os.killpg(proc.pid, signal.SIGCONT); state.paused=False
                    except ProcessLookupError: pass
                win.nodelay(False); stop = confirm_dialog(win, "Cancel operation?", ["The running backup process will be interrupted."], "cancel"); win.nodelay(True)
                if stop:
                    try: os.killpg(proc.pid, signal.SIGINT)
                    except ProcessLookupError: pass
                    break
            time.sleep(0.08)
        try: rc = proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try: os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            rc = proc.wait()
        t.join(timeout=2)
        return rc, log
    finally:
        win.nodelay(False)


def summary_lines(source: str, sm: MountInfo | None, dest_mount: str, dm: MountInfo | None, output: str,
                  selections: list[MountSelection], exclusions: list[str], bench: BenchmarkResult,
                  strategy: Strategy, sudo_needed: bool) -> list[str]:
    lines = [
        "FINAL BACKUP PLAN", "",
        f"Source:      {source}",
        f"Source dev:  {sm.source if sm else 'unknown'}",
        f"Source type: {sm.media if sm else 'unknown'} / {sm.fstype if sm else 'unknown'}",
        "",
        f"Destination mount: {dest_mount}",
        f"Destination dev:   {dm.source if dm else 'unknown'}",
        f"Destination type:  {dm.media if dm else 'unknown'} / {dm.fstype if dm else 'unknown'}",
        f"Output:            {output}",
        "",
        f"Strategy: {strategy.label}",
        f"Reason:   {strategy.reason}",
    ]
    if strategy.kind in {"tar.zst", "img.zst"}:
        lines += [f"zstd: level {strategy.zstd_level}, threads={'all' if strategy.zstd_threads == '0' else strategy.zstd_threads}"]
    lines += ["", "Compression benchmark:"]
    if bench.available:
        speedup = bench.all_threads_mib_s / bench.one_thread_mib_s if bench.one_thread_mib_s else 0
        lines += [
            f"  tmpfs only: {bench.tmpfs}",
            f"  adaptive sample: {human_bytes(bench.sample_bytes)}; repetitions: {bench.repetitions}",
            f"  zstd L3 1 thread: {bench.one_thread_mib_s:.1f} MiB/s",
            f"  zstd L3 all threads: {bench.all_threads_mib_s:.1f} MiB/s ({speedup:.2f}×)",
        ]
    else:
        lines += [f"  skipped/unavailable: {bench.error}"]
    lines += ["", f"sudo required: {'yes' if sudo_needed else 'no'}", "", "Mounted devices:"]
    for s in selections:
        lines.append(f"  {'EXCLUDE' if s.exclude else 'INCLUDE'} {s.mount.target} [{s.mount.media}/{s.mount.fstype}] {s.reason}")
    lines += ["", f"Path exclusion patterns ({len(exclusions)}):"]
    lines.extend(f"  {x}" for x in exclusions)
    if dm and dm.fstype in WINDOWS_FS:
        lines += ["", "Windows-filesystem note: tar.zst preserves Linux metadata inside the archive; ZIP favors Windows access but loses Linux fidelity."]
        if dm.fstype == "vfat":
            lines += ["FAT32/VFAT warning: a single file cannot exceed 4 GiB; large archives/images will fail."]
    if strategy.kind in {"img", "img.zst"}:
        lines += ["", "IMAGE WARNING: raw imaging a mounted/read-write source can be inconsistent. Use an offline/snapshot source for a reliable block image.", "Mount/path exclusions do NOT apply to raw .img/.img.zst; the whole backing partition is imaged."]
    return lines


def main(win: curses.window) -> int:
    curses.curs_set(0); curses.use_default_colors(); win.keypad(True)
    mounts = find_mounts()
    source = os.path.abspath(os.path.expanduser(input_dialog(win, "Backup source", "Directory to back up:", DEFAULT_SOURCE)))
    dest_mount = choose_destination(win, mounts)
    mounts = find_mounts()
    errors = verify_setup(source, dest_mount, mounts)
    if errors:
        scroll_dialog(win, "Pre-flight failed", ["Cannot continue:", ""] + ["• " + x for x in errors]); return 2
    sm = mount_for_path(source, mounts); dm = mount_for_path(dest_mount, mounts)

    selections = select_mount_exclusions(win, initial_mount_selections(source, dest_mount, mounts))
    base = ROOT_EXCLUSIONS if os.path.realpath(source) == "/" else GENERIC_EXCLUSIONS
    exclusions = list(dict.fromkeys(list(base) + mount_patterns(source, selections)))
    exclusions = edit_exclusions(win, exclusions)

    do_bench = confirm_dialog(win, "CPU / compression test", [
        "Run an adaptive zstd benchmark before choosing the backup format?",
        "Safety: benchmark data and output stay in tmpfs/RAM only; no HDD/SSD benchmark file is created.",
        "Sample size is capped by available RAM and tmpfs free space.",
    ], "run benchmark")
    bench = benchmark_zstd(win, mounts) if do_bench else BenchmarkResult(error="skipped by user", cpu_threads=os.cpu_count() or 1)
    if bench.available:
        scroll_dialog(win, "Benchmark result", [
            f"tmpfs: {bench.tmpfs}", f"sample: {human_bytes(bench.sample_bytes)}", f"CPU threads: {bench.cpu_threads}",
            f"zstd L3 1T: {bench.one_thread_mib_s:.1f} MiB/s", f"zstd L3 all threads: {bench.all_threads_mib_s:.1f} MiB/s",
            f"parallel speedup: {(bench.all_threads_mib_s / bench.one_thread_mib_s if bench.one_thread_mib_s else 0):.2f}×",
        ])
    else:
        scroll_dialog(win, "Benchmark", ["Benchmark unavailable/skipped:", bench.error])

    strategy = configure_compression(win, choose_strategy(win, sm, dm, bench), bench)
    if strategy.kind == "rsync":
        default_name = "linux-root-backup" if source == "/" else Path(source).name + "-backup"
        name = input_dialog(win, "Output directory", "Backup directory name:", default_name)
    else:
        base_name = "linux-root" if source == "/" else (Path(source).name or "backup")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = input_dialog(win, "Output file", f"Output filename ({strategy.extension}):", f"{base_name}-{stamp}{strategy.extension}")
    output = normalized_output(dest_mount, name, strategy)
    while strategy.kind != "rsync" and os.path.exists(output):
        scroll_dialog(win, "Output already exists", [f"Refusing to overwrite: {output}", "Choose a different filename."])
        name = input_dialog(win, "Output file", f"Output filename ({strategy.extension}):", Path(output).stem + "-new" + strategy.extension)
        output = normalized_output(dest_mount, name, strategy)
    if path_is_within(output, source):
        rel = os.path.relpath(os.path.realpath(output), os.path.realpath(source))
        if rel != ".": exclusions = list(dict.fromkeys(exclusions + ["/" + rel.strip("/") + "/***"]))

    sudo_needed = needs_sudo(source, output, strategy)
    summary = summary_lines(source, sm, dest_mount, dm, output, selections, exclusions, bench, strategy, sudo_needed)
    if not confirm_dialog(win, "Review choices before starting", summary, "START BACKUP"):
        return 0
    if sudo_needed and not request_sudo(win):
        return 1

    command, _ = build_command(source, output, exclusions, strategy, sudo_needed, sm)
    rc, log = run_operation(win, command, strategy, source, output)
    if rc == 0:
        scroll_dialog(win, "Backup completed", ["✓ Operation completed successfully.", f"Output: {output}", f"Log: {log}"])
    else:
        scroll_dialog(win, "Backup failed / stopped", [f"Exit code: {rc}", f"Output: {output}", f"Log: {log}"])
    return rc


def requirements() -> None:
    missing = [x for x in ("rsync", "findmnt", "lsblk") if shutil.which(x) is None]
    if missing:
        raise SystemExit("Missing required command(s): " + ", ".join(missing))
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit("This program requires an interactive terminal.")


if __name__ == "__main__":
    requirements()
    try:
        raise SystemExit(curses.wrapper(main))
    except KeyboardInterrupt:
        raise SystemExit(130)
