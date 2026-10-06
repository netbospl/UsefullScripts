#!/usr/bin/env python3
from __future__ import annotations
import curses, importlib.util, sys
from pathlib import Path
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('backup_tui_core',HERE/'rsync-backup-tui-core.py')
core=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=core
spec.loader.exec_module(core)
import backup_tui_enhancements as enh
import backup_tui_policy as policy
policy.apply(enh)
if __name__=='__main__':
    if '--self-test' in sys.argv:
        raise SystemExit(0 if enh.selftest() else 1)
    core.requirements()
    try:
        raise SystemExit(curses.wrapper(lambda win: enh.main(core,win)))
    except KeyboardInterrupt:
        raise SystemExit(130)
