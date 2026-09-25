#!/usr/bin/env bash
set -u

MOUNT="/"
POLL_INTERVAL=5

# Conservative balance thresholds:
# Data chunks <= 10% used
# Metadata chunks <= 5% used
DATA_USAGE=10
META_USAGE=5

# Re-run as root if necessary
if [[ $EUID -ne 0 ]]; then
    exec sudo bash "$0" "$@"
fi

# Basic checks
if ! command -v btrfs >/dev/null 2>&1; then
    echo "ERROR: btrfs-progs is not installed."
    exit 1
fi

FSTYPE=$(findmnt -no FSTYPE "$MOUNT")

if [[ "$FSTYPE" != "btrfs" ]]; then
    echo "ERROR: $MOUNT is not a Btrfs filesystem."
    echo "Detected filesystem: $FSTYPE"
    exit 1
fi

echo "============================================"
echo " Btrfs maintenance"
echo " Filesystem: $MOUNT"
echo "============================================"
echo

echo "Filesystem usage BEFORE maintenance:"
btrfs filesystem usage "$MOUNT"
echo

# --------------------------------------------------
# 1. SCRUB
# --------------------------------------------------

echo "============================================"
echo " STEP 1/2 - Btrfs SCRUB"
echo "============================================"
echo
echo "Checking filesystem data and metadata..."
echo

SCRUB_LOG=$(mktemp)

btrfs scrub start -B "$MOUNT" >"$SCRUB_LOG" 2>&1 &
SCRUB_PID=$!

while kill -0 "$SCRUB_PID" 2>/dev/null; do
    echo
    echo "----- Scrub progress: $(date '+%H:%M:%S') -----"
    btrfs scrub status "$MOUNT" || true
    sleep "$POLL_INTERVAL"
done

wait "$SCRUB_PID"
SCRUB_EXIT=$?

echo
echo "----- Final scrub result -----"
cat "$SCRUB_LOG"

if [[ $SCRUB_EXIT -ne 0 ]]; then
    echo
    echo "ERROR: Scrub command failed."
    echo "Balance will NOT be started."
    rm -f "$SCRUB_LOG"
    exit 1
fi

# Don't relocate data if scrub detected filesystem/data errors.
if ! grep -qi "Error summary:.*no errors found" "$SCRUB_LOG"; then
    echo
    echo "WARNING: Scrub did not report a clean filesystem."
    echo "Balance will NOT be started."
    echo
    echo "Investigate the scrub result above first."
    rm -f "$SCRUB_LOG"
    exit 2
fi

rm -f "$SCRUB_LOG"

echo
echo "✓ Scrub completed successfully with no errors."
echo

# --------------------------------------------------
# 2. FILTERED BALANCE
# --------------------------------------------------

echo "============================================"
echo " STEP 2/2 - Btrfs FILTERED BALANCE"
echo "============================================"
echo
echo "Balancing:"
echo "  Data chunks     <= ${DATA_USAGE}% used"
echo "  Metadata chunks <= ${META_USAGE}% used"
echo

BALANCE_LOG=$(mktemp)

btrfs balance start \
    -dusage="$DATA_USAGE" \
    -musage="$META_USAGE" \
    "$MOUNT" >"$BALANCE_LOG" 2>&1 &

BALANCE_PID=$!

while kill -0 "$BALANCE_PID" 2>/dev/null; do
    echo
    echo "----- Balance progress: $(date '+%H:%M:%S') -----"
    btrfs balance status "$MOUNT" || true
    sleep "$POLL_INTERVAL"
done

wait "$BALANCE_PID"
BALANCE_EXIT=$?

echo
echo "----- Final balance result -----"
cat "$BALANCE_LOG"
rm -f "$BALANCE_LOG"

if [[ $BALANCE_EXIT -ne 0 ]]; then
    echo
    echo "WARNING: Balance exited with an error."
    exit 3
fi

echo
echo "✓ Balance completed successfully."
echo

# --------------------------------------------------
# FINAL STATUS
# --------------------------------------------------

echo "============================================"
echo " MAINTENANCE COMPLETE"
echo "============================================"
echo
echo "Filesystem usage AFTER maintenance:"
btrfs filesystem usage "$MOUNT"

echo
echo "Final scrub status:"
btrfs scrub status "$MOUNT"

echo
echo "Done."
