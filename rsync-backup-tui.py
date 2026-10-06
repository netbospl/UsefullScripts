#!/usr/bin/env python3
"""Interactive full-screen rsync backup TUI for Linux.

Designed for local filesystem backups (for example Btrfs/SSD -> ext4/USB HDD).
The interface is redrawn in place with curses; rsync output is captured and parsed
instead of being printed line-by-line to the terminal.
"""

from __future__ import annotations

import curses
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

APP_NAME = "Linux rsync Backup TUI"
DEFAULT_SOURCE = "/"
DEFAULT_BACKUP_DIR = "linux-root-backup"
PREFERRED_DEST_MOUNT = "/run/media/netbos/ext4HDD"
LOG_DIR = Path("/var/log/rsync-backup-tui")

VIRTUAL_FS = {
    "autofs", "bpf", "cgroup", "cgroup2", "configfs", "debugfs", "devpts",
    "devtmpfs", "efivarfs", "fusectl", "hugetlbfs", "mqueue", "proc",
    "pstore", "securityfs", "sysfs", "tmpfs", "tracefs",
}

LOCAL_DEST_FS = {
    "btrfs", "ext2", "ext3", "ext4", "exfat", "f2fs", "fuseblk", "ntfs3",
    "vfat", "xfs",
}

ROOT_EXCLUSIONS = [
    "/dev/***",
    "/proc/***",
    "/sys/***",
    "/run/***",
    "/tmp/***",
    "/mnt/***",
    "/media/***",
    "/var/cache/***",
    "/var/tmp/***",
    "/var/log/journal/***",
    "/var/lib/systemd/coredump/***",
    "/home/*/.cache/***",
    "/home/*/.local/share/Trash/***",
    "/home/*/.local/share/Steam/appcache/***",
    "/home/*/.local/share/Steam/logs/***",
    "/home/*/.config/*/Cache/***",
    "/home/*/.config/*/cache/***",
    "/home/*/.mozilla/firefox/*/cache2/***",
    "/home/*/.thumbnails/***",
    "/home/*/.Trash*/***",
    "/home/*/tmp/***",
    "/home/*/Temp/***",
    "/home/*/Downloads/*.part",
    "/home/*/Downloads/*.crdownload",
    "/root/.cache/***",
    "/lost+found",
    "/swapfile",
    "/download/***",
]

GENERIC_EXCLUSIONS = [
    "*/.cache/***",
    "*/Cache/***",
    "*/cache/***",
    "*/.local/share/Trash/***",
    "*/.Trash*/***",
    "*/tmp/***",
    "*/Temp/***",
    "*/Downloads/*.part",
    "*/Downloads/*.crdownload",
]

PROGRESS_RE = re.compile(
    r"^\s*([0-9][0-9,]*)\s+(\d{1,3})%\s+([^\s]+/s)\s+([0-9:]+)(?:\s+\((.*)\))?"
)
XFR_RE = re.compile(r"xfr#([0-9]+)")
CHECK_RE = re.compile(r"(?:to-chk|ir-chk)=([0-9]+)/([0-9]+)")


@dataclass
class MountInfo:
    target: str
    source: str
    fstype: str
    options: str
    total: int = 0
    free: int = 0
    rota: str = "?"
    model: str = ""


@dataclass
class ProgressState:
    percent: int = 0
    bytes_done: int = 0
    speed: str = "—"
    eta: str = "—"
    transferred_files: int = 0
    remaining_files: int | None = None
    total_files: int | None = None
    current_file: str = "Waiting for rsync…"
    last_message: str = ""
    paused: bool = False
    started_at: float = 0.0


def human_bytes(value: int) -> str:
    value_f = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if value_f < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value_f)} {unit}"
            return f"{value_f:.1f} {unit}"
        value_f /= 1024.0
    return f"{value_f:.1f} PiB"


def clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    text = str(text).replace("\n", " ").replace("\r", " ")
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: max(0, width - 1)] + "…"


def safe_addstr(win: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        h, w = win.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            win.addnstr(y, x, text, max(0, w - x - 1), attr)
    except curses.error:
        pass


def centered(win: curses.window, y: int, text: str, attr: int = 0) -> None:
    _, w = win.getmaxyx()
    x = max(0, (w - len(text)) // 2)
    safe_addstr(win, y, x, text, attr)


def draw_frame(stdscr: curses.window, title: str, footer: str = "") -> tuple[int, int]:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if h < 18 or w < 72:
        centered(stdscr, max(0, h // 2 - 1), "Terminal too small", curses.A_BOLD)
        centered(stdscr, min(h - 1, h // 2), "Resize to at least 72×18")
        stdscr.refresh()
        return h, w

    try:
        stdscr.box()
    except curses.error:
        pass
    centered(stdscr, 1, f" {title} ", curses.A_BOLD)
    safe_addstr(stdscr, 2, 1, "─" * max(0, w - 2))
    if footer:
        safe_addstr(stdscr, h - 3, 1, "─" * max(0, w - 2))
        centered(stdscr, h - 2, footer, curses.A_DIM)
    return h, w


def input_dialog(stdscr: curses.window, title: str, prompt: str, default: str) -> str:
    curses.curs_set(1)
    value = list(default)
    pos = len(value)
    while True:
        h, w = draw_frame(stdscr, title, "Enter: accept   Esc: cancel")
        if h < 18 or w < 72:
            time.sleep(0.1)
            continue
        safe_addstr(stdscr, 5, 4, prompt, curses.A_BOLD)
        field_w = max(10, w - 10)
        shown = "".join(value)
        offset = max(0, pos - field_w + 1)
        safe_addstr(stdscr, 7, 4, " " * field_w, curses.A_REVERSE)
        safe_addstr(stdscr, 7, 4, shown[offset : offset + field_w], curses.A_REVERSE)
        try:
            stdscr.move(7, min(w - 6, 4 + pos - offset))
        except curses.error:
            pass
        stdscr.refresh()
        ch = stdscr.get_wch()
        if ch in ("\n", "\r", curses.KEY_ENTER):
            result = "".join(value).strip()
            if result:
                curses.curs_set(0)
                return result
        if ch == "\x1b":
            curses.curs_set(0)
            raise KeyboardInterrupt
        if ch in (curses.KEY_BACKSPACE, "\b", "\x7f"):
            if pos > 0:
                del value[pos - 1]
                pos -= 1
        elif ch == curses.KEY_DC:
            if pos < len(value):
                del value[pos]
        elif ch == curses.KEY_LEFT:
            pos = max(0, pos - 1)
        elif ch == curses.KEY_RIGHT:
            pos = min(len(value), pos + 1)
        elif ch == curses.KEY_HOME:
            pos = 0
        elif ch == curses.KEY_END:
            pos = len(value)
        elif isinstance(ch, str) and ch.isprintable():
            value.insert(pos, ch)
            pos += 1


def message_dialog(stdscr: curses.window, title: str, lines: Iterable[str], footer: str = "Enter: continue") -> None:
    lines = list(lines)
    offset = 0
    while True:
        h, w = draw_frame(stdscr, title, footer)
        body_h = max(1, h - 7)
        for row, line in enumerate(lines[offset : offset + body_h], start=4):
            safe_addstr(stdscr, row, 4, clip(line, w - 8))
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER, 27):
            return
        if ch in (curses.KEY_DOWN, ord("j")):
            offset = min(max(0, len(lines) - body_h), offset + 1)
        elif ch in (curses.KEY_UP, ord("k")):
            offset = max(0, offset - 1)
        elif ch == curses.KEY_NPAGE:
            offset = min(max(0, len(lines) - body_h), offset + body_h)
        elif ch == curses.KEY_PPAGE:
            offset = max(0, offset - body_h)


def confirm_dialog(stdscr: curses.window, title: str, lines: Iterable[str], yes_label: str = "Start backup") -> bool:
    lines = list(lines)
    while True:
        h, w = draw_frame(stdscr, title, f"Y: {yes_label}   N/Esc: cancel")
        for row, line in enumerate(lines[: max(0, h - 8)], start=4):
            safe_addstr(stdscr, row, 4, clip(line, w - 8))
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27):
            return False


def choose_dialog(stdscr: curses.window, title: str, choices: list[str], default_index: int = 0) -> int:
    idx = min(max(default_index, 0), max(0, len(choices) - 1))
    offset = 0
    while True:
        h, w = draw_frame(stdscr, title, "↑/↓: select   Enter: choose   Esc: cancel")
        body_h = max(1, h - 8)
        if idx < offset:
            offset = idx
        if idx >= offset + body_h:
            offset = idx - body_h + 1
        for visual_row, choice_idx in enumerate(range(offset, min(len(choices), offset + body_h)), start=4):
            attr = curses.A_REVERSE | curses.A_BOLD if choice_idx == idx else 0
            safe_addstr(stdscr, visual_row, 4, clip(choices[choice_idx], w - 8), attr)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (curses.KEY_DOWN, ord("j")):
            idx = min(len(choices) - 1, idx + 1)
        elif ch in (curses.KEY_UP, ord("k")):
            idx = max(0, idx - 1)
        elif ch in (10, 13, curses.KEY_ENTER):
            return idx
        elif ch == 27:
            raise KeyboardInterrupt


def edit_exclusions(stdscr: curses.window, exclusions: list[str]) -> list[str]:
    items = list(dict.fromkeys(exclusions))
    idx = 0
    offset = 0
    while True:
        h, w = draw_frame(
            stdscr,
            "Exclusions",
            "A: add   D: remove selected   Enter: accept   Esc: back",
        )
        body_h = max(1, h - 9)
        safe_addstr(stdscr, 3, 4, f"{len(items)} active exclusions", curses.A_BOLD)
        if items:
            idx = min(idx, len(items) - 1)
            if idx < offset:
                offset = idx
            if idx >= offset + body_h:
                offset = idx - body_h + 1
            for row, item_idx in enumerate(range(offset, min(len(items), offset + body_h)), start=5):
                attr = curses.A_REVERSE if item_idx == idx else 0
                safe_addstr(stdscr, row, 4, clip(items[item_idx], w - 8), attr)
        else:
            safe_addstr(stdscr, 5, 4, "No exclusions configured.", curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return items
        if ch == 27:
            return items
        if ch in (ord("a"), ord("A")):
            new_item = input_dialog(stdscr, "Add exclusion", "rsync exclude pattern:", "")
            if new_item and new_item not in items:
                items.append(new_item)
                idx = len(items) - 1
        elif ch in (ord("d"), ord("D"), curses.KEY_DC):
            if items:
                del items[idx]
                idx = max(0, idx - 1)
        elif ch in (curses.KEY_DOWN, ord("j")) and items:
            idx = min(len(items) - 1, idx + 1)
        elif ch in (curses.KEY_UP, ord("k")) and items:
            idx = max(0, idx - 1)


def run_text(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def find_mounts() -> list[MountInfo]:
    raw = run_text(["findmnt", "-J", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"])
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []

    found: list[MountInfo] = []

    def walk(nodes: list[dict]) -> None:
        for node in nodes:
            target = str(node.get("target") or "")
            source = str(node.get("source") or "")
            fstype = str(node.get("fstype") or "")
            options = str(node.get("options") or "")
            if target:
                mi = MountInfo(target=target, source=source, fstype=fstype, options=options)
                try:
                    usage = shutil.disk_usage(target)
                    mi.total, mi.free = usage.total, usage.free
                except OSError:
                    pass
                if source.startswith("/dev/"):
                    block_source = source.split("[", 1)[0]
                    mi.rota = run_text(["lsblk", "-ndo", "ROTA", block_source]) or "?"
                    mi.model = run_text(["lsblk", "-ndo", "MODEL", block_source])
                found.append(mi)
            walk(node.get("children") or [])

    walk(payload.get("filesystems") or [])
    return found


def destination_candidates(mounts: list[MountInfo]) -> list[MountInfo]:
    result = []
    for m in mounts:
        if m.fstype in VIRTUAL_FS:
            continue
        if m.fstype not in LOCAL_DEST_FS:
            continue
        if "rw" not in m.options.split(","):
            continue
        if m.target == "/":
            continue
        result.append(m)
    result.sort(key=lambda m: (m.target != PREFERRED_DEST_MOUNT, m.target))
    return result


def mount_for_path(path: str, mounts: list[MountInfo]) -> MountInfo | None:
    path = os.path.realpath(path)
    matches = []
    for m in mounts:
        target = os.path.realpath(m.target)
        try:
            if os.path.commonpath([path, target]) == target:
                matches.append((len(target), m))
        except ValueError:
            pass
    return max(matches, default=(0, None), key=lambda t: t[0])[1]


def root_parent_disk(device: str) -> str:
    if not device.startswith("/dev/"):
        return ""
    device = device.split("[", 1)[0]
    current = os.path.realpath(device)
    while True:
        parent = run_text(["lsblk", "-ndo", "PKNAME", current])
        if not parent:
            return current
        current = "/dev/" + parent.strip()


def path_is_within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(parent)]) == os.path.realpath(parent)
    except ValueError:
        return False


def dynamic_mount_exclusions(source: str, dest_mount: str, mounts: list[MountInfo]) -> list[str]:
    source_real = os.path.realpath(source)
    exclusions: list[str] = []
    source_mount = mount_for_path(source_real, mounts)
    source_disk = root_parent_disk(source_mount.source) if source_mount else ""

    for m in mounts:
        target = os.path.realpath(m.target)
        if target == source_real or not path_is_within(target, source_real):
            continue

        # Always exclude the backup destination if it is visible below the source.
        if os.path.realpath(m.target) == os.path.realpath(dest_mount):
            keep = False
        else:
            m_disk = root_parent_disk(m.source)
            same_disk = bool(source_disk and m_disk and source_disk == m_disk)
            # Keep persistent subvolumes/partitions from the same system disk.
            keep = same_disk and m.fstype not in VIRTUAL_FS and not m.fstype.startswith("fuse.")

        if keep:
            continue

        rel = os.path.relpath(target, source_real)
        if rel == ".":
            continue
        exclusions.append("/" + rel.strip("/") + "/***")

    return exclusions


def detect_mode(dest_mount: MountInfo | None) -> str:
    if dest_mount is None:
        return "Sequential"
    if dest_mount.rota.strip() == "1":
        return "HDD / Sequential"
    if dest_mount.rota.strip() == "0":
        return "SSD / Sequential"
    return "Sequential"


def format_mount_choice(m: MountInfo) -> str:
    free = human_bytes(m.free) if m.free else "? free"
    media = "HDD" if m.rota.strip() == "1" else "SSD" if m.rota.strip() == "0" else m.fstype
    device = m.source
    model = f" • {m.model.strip()}" if m.model.strip() else ""
    return f"{m.target}   [{device}, {m.fstype}, {media}, {free}{model}]"


def choose_destination_mount(stdscr: curses.window, mounts: list[MountInfo]) -> str:
    candidates = destination_candidates(mounts)
    labels = [format_mount_choice(m) for m in candidates]
    labels.append("Enter a custom mounted destination path…")
    default_idx = 0
    for i, m in enumerate(candidates):
        if m.target == PREFERRED_DEST_MOUNT:
            default_idx = i
            break
    idx = choose_dialog(stdscr, "Choose destination mount", labels, default_idx)
    if idx == len(candidates):
        return input_dialog(stdscr, "Destination mount", "Mounted destination path:", PREFERRED_DEST_MOUNT)
    return candidates[idx].target


def verify_setup(source: str, dest_mount: str, dest: str, mounts: list[MountInfo]) -> list[str]:
    errors: list[str] = []
    if not os.path.isdir(source):
        errors.append(f"Source directory does not exist: {source}")
    if not os.path.isdir(dest_mount):
        errors.append(f"Destination mount path does not exist: {dest_mount}")
    if not os.path.ismount(dest_mount):
        errors.append(f"Destination is not a mountpoint: {dest_mount}")
    if os.path.realpath(source) == os.path.realpath(dest):
        errors.append("Source and destination cannot be the same directory.")
    m = mount_for_path(dest_mount, mounts)
    if m and "rw" not in m.options.split(","):
        errors.append(f"Destination mount is not writable: {dest_mount}")
    if m and m.fstype in {"vfat", "exfat", "fuseblk", "ntfs3"}:
        errors.append(
            f"Destination filesystem {m.fstype} is unsuitable for -A/-X Linux metadata; use ext4/Btrfs/XFS."
        )
    return errors


def progress_bar(percent: int, width: int) -> str:
    width = max(10, width)
    percent = max(0, min(100, percent))
    filled = int(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def parse_progress_line(line: str, state: ProgressState) -> None:
    cleaned = line.strip()
    if not cleaned:
        return
    match = PROGRESS_RE.match(cleaned)
    if match:
        state.bytes_done = int(match.group(1).replace(",", ""))
        state.percent = max(0, min(100, int(match.group(2))))
        state.speed = match.group(3)
        state.eta = match.group(4)
        details = match.group(5) or ""
        xfr = XFR_RE.search(details)
        if xfr:
            state.transferred_files = int(xfr.group(1))
        chk = CHECK_RE.search(details)
        if chk:
            state.remaining_files = int(chk.group(1))
            state.total_files = int(chk.group(2))
        return

    # --out-format=%n emits item names as ordinary lines.
    if not cleaned.startswith(("sending incremental file list", "receiving incremental file list")):
        state.current_file = cleaned
        state.last_message = cleaned


def reader_thread(proc: subprocess.Popen[bytes], updates: queue.Queue[str], log_path: Path) -> None:
    buffer = b""
    assert proc.stdout is not None
    with log_path.open("ab", buffering=0) as log:
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            log.write(chunk)
            buffer += chunk
            parts = re.split(br"[\r\n]+", buffer)
            buffer = parts.pop() if parts else b""
            for part in parts:
                if part:
                    updates.put(part.decode("utf-8", errors="replace"))
        if buffer:
            updates.put(buffer.decode("utf-8", errors="replace"))


def draw_backup_screen(
    stdscr: curses.window,
    source: str,
    dest: str,
    mode: str,
    state: ProgressState,
    exclusions: list[str],
    log_path: Path,
) -> None:
    h, w = draw_frame(
        stdscr,
        APP_NAME,
        "P: pause/resume   L: log path   E: exclusions   Q: cancel",
    )
    if h < 18 or w < 72:
        return
    safe_addstr(stdscr, 4, 4, "Source", curses.A_BOLD)
    safe_addstr(stdscr, 4, 18, clip(source, w - 22))
    safe_addstr(stdscr, 5, 4, "Destination", curses.A_BOLD)
    safe_addstr(stdscr, 5, 18, clip(dest, w - 22))
    safe_addstr(stdscr, 6, 4, "Mode", curses.A_BOLD)
    safe_addstr(stdscr, 6, 18, mode)

    bar_w = max(20, w - 16)
    safe_addstr(stdscr, 8, 6, progress_bar(state.percent, bar_w))
    centered(stdscr, 9, f"{state.percent:3d}%   {human_bytes(state.bytes_done)}   {state.speed}   ETA {state.eta}", curses.A_BOLD)

    elapsed = max(0, int(time.monotonic() - state.started_at)) if state.started_at else 0
    elapsed_s = time.strftime("%H:%M:%S", time.gmtime(elapsed))
    file_stats = f"Transferred files: {state.transferred_files}"
    if state.total_files is not None and state.remaining_files is not None:
        file_stats += f"   Remaining: {state.remaining_files}/{state.total_files}"
    safe_addstr(stdscr, 11, 4, file_stats)
    safe_addstr(stdscr, 12, 4, f"Elapsed: {elapsed_s}   Exclusions: {len(exclusions)}")

    status = "PAUSED" if state.paused else "RUNNING"
    safe_addstr(stdscr, 14, 4, "Status", curses.A_BOLD)
    safe_addstr(stdscr, 14, 18, status, curses.A_BOLD | (curses.A_REVERSE if state.paused else 0))
    safe_addstr(stdscr, 15, 4, "Current", curses.A_BOLD)
    safe_addstr(stdscr, 15, 18, clip(state.current_file, w - 22))
    if h > 20:
        safe_addstr(stdscr, 17, 4, "Log", curses.A_BOLD)
        safe_addstr(stdscr, 17, 18, clip(str(log_path), w - 22), curses.A_DIM)


def run_backup(
    stdscr: curses.window,
    source: str,
    dest: str,
    exclusions: list[str],
    mode: str,
) -> tuple[int, Path]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".log")
    Path(dest).mkdir(parents=True, exist_ok=True)

    cmd = [
        "rsync",
        "-aAXHS",
        "--numeric-ids",
        "--info=progress2",
        "--out-format=%n",
        "--outbuf=L",
        "--stats",
    ]
    for pattern in exclusions:
        cmd.extend(["--exclude", pattern])
    cmd.extend([source.rstrip("/") + "/" if source != "/" else "/", dest.rstrip("/") + "/"])

    with log_path.open("w", encoding="utf-8") as log:
        log.write("# Command: " + " ".join(repr(part) for part in cmd) + "\n")
        log.write("# Started: " + datetime.now().isoformat() + "\n\n")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        start_new_session=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    updates: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=reader_thread, args=(proc, updates, log_path), daemon=True)
    reader.start()

    state = ProgressState(started_at=time.monotonic())
    stdscr.nodelay(True)
    try:
        while proc.poll() is None:
            while True:
                try:
                    parse_progress_line(updates.get_nowait(), state)
                except queue.Empty:
                    break

            draw_backup_screen(stdscr, source, dest, mode, state, exclusions, log_path)
            stdscr.refresh()
            try:
                ch = stdscr.getch()
            except curses.error:
                ch = -1

            if ch in (ord("p"), ord("P")):
                try:
                    if state.paused:
                        os.killpg(proc.pid, signal.SIGCONT)
                        state.paused = False
                    else:
                        os.killpg(proc.pid, signal.SIGSTOP)
                        state.paused = True
                except ProcessLookupError:
                    pass
            elif ch in (ord("e"), ord("E")):
                stdscr.nodelay(False)
                message_dialog(stdscr, "Active exclusions", exclusions, "↑/↓: scroll   Enter: back")
                stdscr.nodelay(True)
            elif ch in (ord("l"), ord("L")):
                stdscr.nodelay(False)
                message_dialog(stdscr, "Backup log", [str(log_path)], "Enter: back")
                stdscr.nodelay(True)
            elif ch in (ord("q"), ord("Q")):
                if state.paused:
                    try:
                        os.killpg(proc.pid, signal.SIGCONT)
                        state.paused = False
                    except ProcessLookupError:
                        pass
                stdscr.nodelay(False)
                cancel = confirm_dialog(stdscr, "Cancel backup?", ["The running rsync process will be interrupted."], "cancel backup")
                stdscr.nodelay(True)
                if cancel:
                    try:
                        os.killpg(proc.pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
                    break
            time.sleep(0.08)

        try:
            rc = proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            rc = proc.wait()

        reader.join(timeout=2)
        while True:
            try:
                parse_progress_line(updates.get_nowait(), state)
            except queue.Empty:
                break
        if rc == 0:
            state.percent = 100
        return rc, log_path
    finally:
        stdscr.nodelay(False)


def setup_wizard(stdscr: curses.window) -> tuple[str, str, str, list[str], MountInfo | None]:
    mounts = find_mounts()
    source = input_dialog(stdscr, "Backup source", "Directory to copy from:", DEFAULT_SOURCE)
    source = os.path.abspath(os.path.expanduser(source))

    dest_mount = choose_destination_mount(stdscr, mounts)
    dest_mount = os.path.abspath(os.path.expanduser(dest_mount))

    default_name = DEFAULT_BACKUP_DIR if source == "/" else Path(source).name + "-backup"
    backup_name = input_dialog(
        stdscr,
        "Backup directory",
        f"Directory inside {dest_mount}:",
        default_name,
    ).strip("/")
    dest = os.path.join(dest_mount, backup_name)

    # Refresh mount info after interactive choices.
    mounts = find_mounts()
    errors = verify_setup(source, dest_mount, dest, mounts)
    if errors:
        message_dialog(stdscr, "Pre-flight failed", ["Cannot start:", ""] + [f"• {e}" for e in errors])
        raise RuntimeError("pre-flight failed")

    base = ROOT_EXCLUSIONS if os.path.realpath(source) == "/" else GENERIC_EXCLUSIONS
    exclusions = list(base)
    exclusions.extend(dynamic_mount_exclusions(source, dest_mount, mounts))
    exclusions = list(dict.fromkeys(exclusions))
    exclusions = edit_exclusions(stdscr, exclusions)

    dest_info = mount_for_path(dest_mount, mounts)
    return source, dest_mount, dest, exclusions, dest_info


def main(stdscr: curses.window) -> int:
    curses.curs_set(0)
    curses.use_default_colors()
    stdscr.keypad(True)

    try:
        source, dest_mount, dest, exclusions, dest_info = setup_wizard(stdscr)
    except KeyboardInterrupt:
        return 130
    except RuntimeError:
        return 2

    mode = detect_mode(dest_info)
    try:
        usage = shutil.disk_usage(dest_mount)
        free_line = f"Free space: {human_bytes(usage.free)} of {human_bytes(usage.total)}"
    except OSError:
        free_line = "Free space: unknown"

    summary = [
        f"Source:      {source}",
        f"Destination: {dest}",
        f"Filesystem:  {dest_info.fstype if dest_info else 'unknown'}",
        f"Mode:        {mode}",
        free_line,
        f"Exclusions:  {len(exclusions)}",
        "",
        "rsync options: -aAXHS --numeric-ids --info=progress2",
        "No --delete will be used.",
    ]
    if not confirm_dialog(stdscr, "Ready to start", summary, "start backup"):
        return 0

    rc, log_path = run_backup(stdscr, source, dest, exclusions, mode)
    if rc == 0:
        message_dialog(
            stdscr,
            "Backup completed",
            ["✓ rsync completed successfully.", f"Destination: {dest}", f"Log: {log_path}"],
        )
    else:
        message_dialog(
            stdscr,
            "Backup stopped / failed",
            [f"rsync exit code: {rc}", f"Destination: {dest}", f"Check log: {log_path}"],
        )
    return rc


def ensure_root() -> None:
    if os.geteuid() == 0:
        return
    script = os.path.realpath(__file__)
    try:
        os.execvp("sudo", ["sudo", sys.executable, script, *sys.argv[1:]])
    except FileNotFoundError:
        print("ERROR: root privileges are required and sudo is not installed.", file=sys.stderr)
        raise SystemExit(1)


def requirements() -> None:
    missing = [cmd for cmd in ("rsync", "findmnt", "lsblk") if shutil.which(cmd) is None]
    if missing:
        print("ERROR: missing required command(s): " + ", ".join(missing), file=sys.stderr)
        raise SystemExit(1)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("ERROR: this program requires an interactive terminal.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    requirements()
    ensure_root()
    try:
        raise SystemExit(curses.wrapper(main))
    except KeyboardInterrupt:
        raise SystemExit(130)
