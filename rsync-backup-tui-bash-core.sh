#!/usr/bin/env bash
# This path intentionally preserves the previous Bash implementation.
# rsync-backup-tui.sh loads the stable core and layers format-aware choices on top.
exec "$(dirname "$0")/rsync-backup-tui.sh" "$@"
