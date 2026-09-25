#!/usr/bin/env bash
set -u

# ============================================================
# Btrfs Maintenance TUI
#
# - Detects NVMe SSD / SATA SSD / HDD / mixed storage
# - Tracks last successful script run
# - Tracks scrub and balance separately
# - Warns if maintenance was run recently
# - Lets user choose balance aggressiveness
# - Dynamically recommends a balance option
# - Shows Btrfs Assistant-style filesystem statistics
#   before and after maintenance
# - Scrub always runs BEFORE balance
# - Stops balance if scrub detects errors
# - Live updating terminal TUI
# ============================================================

MOUNT="/"
POLL_INTERVAL=1

RECOMMENDED_SCRUB_DAYS=30
RECENT_BALANCE_DAYS=60

STATE_DIR="/var/lib/btrfs-maintenance-tui"

LAST_RUN_FILE="$STATE_DIR/last-successful-run"
LAST_SCRUB_FILE="$STATE_DIR/last-successful-scrub"
LAST_BALANCE_FILE="$STATE_DIR/last-successful-balance"

SCRUB_LOG=""
BALANCE_LOG=""

CURRENT_STAGE="idle"
CHILD_PID=""

# ============================================================
# TERMINAL COLOURS
# ============================================================

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'
    DIM=$'\033[2m'
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    RED=$'\033[31m'
    CYAN=$'\033[36m'
    RESET=$'\033[0m'

    CLEAR=$'\033[2J\033[H'
    HIDE_CURSOR=$'\033[?25l'
    SHOW_CURSOR=$'\033[?25h'
else
    BOLD=""
    DIM=""
    GREEN=""
    YELLOW=""
    RED=""
    CYAN=""
    RESET=""
    CLEAR=""
    HIDE_CURSOR=""
    SHOW_CURSOR=""
fi

# ============================================================
# CLEANUP
# ============================================================

cleanup() {
    printf '%s' "$SHOW_CURSOR"

    [[ -n "$SCRUB_LOG" && -f "$SCRUB_LOG" ]] &&
        rm -f "$SCRUB_LOG"

    [[ -n "$BALANCE_LOG" && -f "$BALANCE_LOG" ]] &&
        rm -f "$BALANCE_LOG"
}

interrupt() {
    printf '%s' "$SHOW_CURSOR"

    echo
    echo "Stopping current Btrfs operation..."

    if [[ "$CURRENT_STAGE" == "scrub" ]]; then
        btrfs scrub cancel "$MOUNT" >/dev/null 2>&1 || true
    elif [[ "$CURRENT_STAGE" == "balance" ]]; then
        btrfs balance cancel "$MOUNT" >/dev/null 2>&1 || true
    fi

    [[ -n "$CHILD_PID" ]] &&
        kill "$CHILD_PID" >/dev/null 2>&1 || true

    cleanup

    echo
    echo "Maintenance interrupted."
    echo "Successful-stage timestamps were preserved."

    exit 130
}

trap cleanup EXIT
trap interrupt INT TERM

# ============================================================
# HELPERS
# ============================================================

format_time() {
    local s="$1"

    printf "%02d:%02d:%02d" \
        $((s / 3600)) \
        $(((s % 3600) / 60)) \
        $((s % 60))
}

format_age() {
    local s="$1"
    local d h m

    d=$((s / 86400))
    h=$(((s % 86400) / 3600))
    m=$(((s % 3600) / 60))

    if (( d > 0 )); then
        printf "%d day%s, %d hour%s" \
            "$d" "$([[ $d -eq 1 ]] && echo "" || echo "s")" \
            "$h" "$([[ $h -eq 1 ]] && echo "" || echo "s")"
    elif (( h > 0 )); then
        printf "%d hour%s, %d minute%s" \
            "$h" "$([[ $h -eq 1 ]] && echo "" || echo "s")" \
            "$m" "$([[ $m -eq 1 ]] && echo "" || echo "s")"
    else
        printf "%d minute%s" \
            "$m" "$([[ $m -eq 1 ]] && echo "" || echo "s")"
    fi
}

file_age_seconds() {
    local file="$1"
    local now timestamp

    [[ -f "$file" ]] || return 1

    timestamp="$(cat "$file" 2>/dev/null || true)"
    [[ "$timestamp" =~ ^[0-9]+$ ]] || return 1

    now="$(date +%s)"

    if (( timestamp > now )); then
        timestamp="$now"
    fi

    printf '%s' $((now - timestamp))
}

spinner() {
    local n="$1"
    local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    printf '%s' "${frames[$((n % ${#frames[@]}))]}"
}

separator() {
    echo "${DIM}────────────────────────────────────────────────────────────────${RESET}"
}

header() {
    printf '%s' "$CLEAR"

    echo "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════════╗${RESET}"
    echo "${BOLD}${CYAN}║                  BTRFS MAINTENANCE TUI                      ║${RESET}"
    echo "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════════╝${RESET}"
    echo

    printf " Filesystem : ${BOLD}%s${RESET}\n" "$MOUNT"
    printf " Storage    : ${BOLD}%s${RESET}\n" "$STORAGE_SUMMARY"
    printf " Balance    : ${BOLD}%s${RESET}\n" "$BALANCE_LABEL"

    echo
}

status_line() {
    printf " ${BOLD}%-12s${RESET} %s\n" "$1" "$2"
}

bytes_to_gib() {
    local bytes="${1:-0}"

    awk -v b="$bytes" 'BEGIN {
        printf "%.2f GiB", b / 1073741824
    }'
}

percent_of() {
    local value="${1:-0}"
    local total="${2:-0}"

    awk -v v="$value" -v t="$total" 'BEGIN {
        if (t <= 0) printf "0.00"
        else printf "%.2f", (v / t) * 100
    }'
}

progress_bar() {
    local pct="${1:-0}"
    local width="${2:-42}"
    local filled empty i rounded

    rounded="$(awk -v p="$pct" 'BEGIN {
        if (p < 0) p=0;
        if (p > 100) p=100;
        printf "%.0f", p
    }')"

    filled=$((rounded * width / 100))
    empty=$((width - filled))

    printf '['
    for ((i=0; i<filled; i++)); do
        printf '█'
    done
    for ((i=0; i<empty; i++)); do
        printf '░'
    done
    printf ']'
}

# Return "total used" raw bytes for Data, Metadata or System.
# Data+Metadata mixed block groups count toward both Data and Metadata.
profile_totals() {
    local kind="$1"

    LC_ALL=C btrfs filesystem df -b "$MOUNT" 2>/dev/null |
    awk -v kind="$kind" '
        BEGIN {
            total=0
            used=0
        }

        {
            type=$1
            sub(/,$/, "", type)

            if (type == kind || type == "Data+Metadata") {
                for (i=1; i<=NF; i++) {
                    if ($i ~ /^total=/) {
                        x=$i
                        sub(/^total=/, "", x)
                        sub(/,.*$/, "", x)
                        total += x
                    }

                    if ($i ~ /^used=/) {
                        x=$i
                        sub(/^used=/, "", x)
                        sub(/,.*$/, "", x)
                        used += x
                    }
                }
            }
        }

        END {
            printf "%.0f %.0f\n", total, used
        }
    '
}

# Produce a Btrfs Assistant-style snapshot from current Btrfs allocation.
render_btrfs_stats() {
    local usage
    local fs_size allocated used free_est free_min
    local data_total data_used
    local meta_total meta_used
    local system_total system_used
    local alloc_pct used_pct free_est_pct free_min_pct
    local data_pct meta_pct system_pct

    usage="$(LC_ALL=C btrfs filesystem usage -b "$MOUNT" 2>/dev/null)"

    fs_size="$(
        awk '/^[[:space:]]*Device size:/ {print $3; exit}' <<<"$usage"
    )"

    allocated="$(
        awk '/^[[:space:]]*Device allocated:/ {print $3; exit}' <<<"$usage"
    )"

    used="$(
        awk '/^[[:space:]]*Used:/ {print $2; exit}' <<<"$usage"
    )"

    read -r free_est free_min < <(
        sed -n \
            's/^[[:space:]]*Free (estimated):[[:space:]]*\([0-9][0-9]*\).*min:[[:space:]]*\([0-9][0-9]*\).*/\1 \2/p' \
            <<<"$usage" |
        head -n1
    )

    fs_size="${fs_size:-0}"
    allocated="${allocated:-0}"
    used="${used:-0}"
    free_est="${free_est:-0}"
    free_min="${free_min:-0}"

    read -r data_total data_used < <(profile_totals "Data")
    read -r meta_total meta_used < <(profile_totals "Metadata")
    read -r system_total system_used < <(profile_totals "System")

    data_total="${data_total:-0}"
    data_used="${data_used:-0}"
    meta_total="${meta_total:-0}"
    meta_used="${meta_used:-0}"
    system_total="${system_total:-0}"
    system_used="${system_used:-0}"

    alloc_pct="$(percent_of "$allocated" "$fs_size")"
    used_pct="$(percent_of "$used" "$fs_size")"
    free_est_pct="$(percent_of "$free_est" "$fs_size")"
    free_min_pct="$(percent_of "$free_min" "$fs_size")"

    data_pct="$(percent_of "$data_used" "$data_total")"
    meta_pct="$(percent_of "$meta_used" "$meta_total")"
    system_pct="$(percent_of "$system_used" "$system_total")"

    printf '%s\n' "${BOLD}Information${RESET}"
    printf '\n'
    printf '  Filesystem Size:   %-12s\n' "$(bytes_to_gib "$fs_size")"
    printf '  Allocated:         %-12s (%6s%%)\n' \
        "$(bytes_to_gib "$allocated")" "$alloc_pct"
    printf '  Used:              %-12s (%6s%%)\n' \
        "$(bytes_to_gib "$used")" "$used_pct"
    printf '  Free (Estimated):  %-12s (%6s%%)\n' \
        "$(bytes_to_gib "$free_est")" "$free_est_pct"
    printf '  Free (Minimum):    %-12s (%6s%%)\n' \
        "$(bytes_to_gib "$free_min")" "$free_min_pct"

    printf '\n'
    printf '%s\n' "${BOLD}Internal Filesystem Statistics${RESET}"
    printf '\n'

    printf '  %-9s ' "Data:"
    progress_bar "$data_pct"
    printf ' %6s%%\n' "$data_pct"

    printf '  %-9s ' "Metadata:"
    progress_bar "$meta_pct"
    printf ' %6s%%\n' "$meta_pct"

    printf '  %-9s ' "System:"
    progress_bar "$system_pct"
    printf ' %6s%%\n' "$system_pct"

    printf '\n'
    printf '  %s\n' "${DIM}Internal percentages are usage of already allocated${RESET}"
    printf '  %s\n' "${DIM}Btrfs chunks, not percentage of the whole drive used.${RESET}"
}

# ============================================================
# ROOT
# ============================================================

if [[ $EUID -ne 0 ]]; then
    exec sudo bash "$0" "$@"
fi

# ============================================================
# REQUIREMENTS
# ============================================================

for cmd in btrfs findmnt lsblk awk grep sed readlink; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "ERROR: required command '$cmd' is missing."
        exit 1
    }
done

FSTYPE="$(findmnt -no FSTYPE "$MOUNT" 2>/dev/null || true)"

if [[ "$FSTYPE" != "btrfs" ]]; then
    echo "ERROR: $MOUNT is not Btrfs."
    echo "Detected filesystem: ${FSTYPE:-unknown}"
    exit 1
fi

mkdir -p "$STATE_DIR"

# ============================================================
# STORAGE DETECTION
# ============================================================

mapfile -t BTRFS_DEVS < <(
    btrfs filesystem show "$MOUNT" 2>/dev/null |
    awk '
        $1 == "devid" {
            for (i = 1; i <= NF; i++)
                if ($i == "path")
                    print $(i + 1)
        }
    '
)

if (( ${#BTRFS_DEVS[@]} == 0 )); then
    ROOT_SOURCE="$(
        findmnt -no SOURCE "$MOUNT" |
        sed 's/\[.*$//'
    )"

    BTRFS_DEVS=("$ROOT_SOURCE")
fi

declare -A SEEN_DISKS=()
declare -a DEVICE_LINES=()
declare -a DEVICE_CLASSES=()

for dev in "${BTRFS_DEVS[@]}"; do
    [[ "$dev" == "missing" ]] && continue
    [[ ! -e "$dev" ]] && continue

    node="$(readlink -f "$dev")"

    while :; do
        parent="$(
            lsblk -ndo PKNAME "$node" 2>/dev/null |
            head -n1 |
            tr -d '[:space:]'
        )"

        [[ -z "$parent" ]] && break

        node="/dev/$parent"
    done

    [[ -n "${SEEN_DISKS[$node]:-}" ]] && continue
    SEEN_DISKS[$node]=1

    rota="$(
        lsblk -ndo ROTA "$node" 2>/dev/null |
        head -n1 |
        tr -d '[:space:]'
    )"

    tran="$(
        lsblk -ndo TRAN "$node" 2>/dev/null |
        head -n1 |
        sed 's/^ *//;s/ *$//'
    )"

    model="$(
        lsblk -ndo MODEL "$node" 2>/dev/null |
        head -n1 |
        sed 's/^ *//;s/ *$//'
    )"

    name="$(basename "$node")"

    if [[ "$rota" == "1" ]]; then
        if [[ "$tran" == "sata" || "$tran" == "ata" ]]; then
            class="SATA HDD"
        else
            class="HDD${tran:+ ($tran)}"
        fi
    elif [[ "$rota" == "0" ]]; then
        if [[ "$tran" == "nvme" || "$name" == nvme* ]]; then
            class="NVMe SSD"
        elif [[ "$tran" == "sata" || "$tran" == "ata" ]]; then
            class="SATA SSD"
        elif [[ "$tran" == "usb" ]]; then
            class="USB SSD / non-rotating"
        else
            class="SSD / non-rotating${tran:+ ($tran)}"
        fi
    else
        class="Unknown media type${tran:+ ($tran)}"
    fi

    DEVICE_CLASSES+=("$class")
    DEVICE_LINES+=("$node | $class${model:+ | $model}")
done

if (( ${#DEVICE_LINES[@]} == 0 )); then
    STORAGE_SUMMARY="Unknown"
elif (( ${#DEVICE_LINES[@]} == 1 )); then
    STORAGE_SUMMARY="${DEVICE_CLASSES[0]}"
else
    first="${DEVICE_CLASSES[0]}"
    mixed=0

    for class in "${DEVICE_CLASSES[@]}"; do
        [[ "$class" != "$first" ]] && mixed=1
    done

    if (( mixed )); then
        STORAGE_SUMMARY="Mixed storage (${#DEVICE_LINES[@]} devices)"
    else
        STORAGE_SUMMARY="$first (${#DEVICE_LINES[@]} devices)"
    fi
fi

# ============================================================
# PRE-FLIGHT SCREEN
# ============================================================

printf '%s' "$CLEAR"

echo "${BOLD}${CYAN}Btrfs Maintenance - Pre-flight${RESET}"
separator
echo

echo "Detected root storage: ${BOLD}$STORAGE_SUMMARY${RESET}"
echo

for line in "${DEVICE_LINES[@]}"; do
    echo "  • $line"
done

echo

# ============================================================
# LAST SCRIPT RUN
# ============================================================

if AGE_RUN="$(file_age_seconds "$LAST_RUN_FILE")"; then
    LAST_RUN_TS="$(cat "$LAST_RUN_FILE")"

    echo "${BOLD}Last successful script run:${RESET}"
    echo
    echo "  $(date -d "@$LAST_RUN_TS" '+%d %B %Y at %H:%M:%S')"
    echo "  $(format_age "$AGE_RUN") ago"
    echo

    if (( AGE_RUN < RECOMMENDED_SCRUB_DAYS * 86400 )); then
        echo "${YELLOW}${BOLD}⚠ Maintenance was run less than ${RECOMMENDED_SCRUB_DAYS} days ago.${RESET}"
        echo
        echo "Running it again is normally unnecessary unless you"
        echo "have a specific reason."
        echo

        read -r -p "Continue anyway? [y/N]: " answer

        case "$answer" in
            y|Y|yes|YES|Yes)
                ;;
            *)
                echo
                echo "${GREEN}Cancelled. Nothing changed.${RESET}"
                exit 0
                ;;
        esac
    fi
else
    echo "No previous successful script run is recorded."
    echo
fi

# ============================================================
# DETERMINE RECOMMENDED BALANCE
# ============================================================

BALANCE_RECOMMENDED=2
RECOMMEND_REASON="A conservative filtered balance avoids relocating well-used chunks."

if AGE_BAL="$(file_age_seconds "$LAST_BALANCE_FILE")"; then
    if (( AGE_BAL < RECENT_BALANCE_DAYS * 86400 )); then
        BALANCE_RECOMMENDED=0
        RECOMMEND_REASON="Balance already ran $(format_age "$AGE_BAL") ago; another balance is unlikely to be useful."
    fi
fi

if [[ "$STORAGE_SUMMARY" == *"HDD"* &&
      "$BALANCE_RECOMMENDED" -ne 0 ]]; then
    RECOMMEND_REASON="On HDDs balance can be slow and I/O-heavy; use only a conservative filtered balance when needed."
elif [[ "$STORAGE_SUMMARY" == *"NVMe SSD"* &&
        "$BALANCE_RECOMMENDED" -ne 0 ]]; then
    RECOMMEND_REASON="On NVMe SSDs full balance causes unnecessary relocation and writes; use a conservative filtered balance."
elif [[ "$STORAGE_SUMMARY" == *"SATA SSD"* &&
        "$BALANCE_RECOMMENDED" -ne 0 ]]; then
    RECOMMEND_REASON="On SATA SSDs full balance causes unnecessary relocation and writes; use a conservative filtered balance."
elif [[ "$STORAGE_SUMMARY" == *"Mixed storage"* &&
        "$BALANCE_RECOMMENDED" -ne 0 ]]; then
    RECOMMEND_REASON="Multiple storage types were detected; the conservative filtered balance is the safest routine choice."
fi

# ============================================================
# BALANCE MENU
# ============================================================

echo "${BOLD}Choose what should happen after the scrub:${RESET}"
echo

printf "  0) Scrub only / skip balance%s\n" \
    "$([[ $BALANCE_RECOMMENDED -eq 0 ]] && echo "  ${GREEN}← RECOMMENDED${RESET}" || true)"

printf "  1) Light filtered        -dusage=5  -musage=5%s\n" \
    "$([[ $BALANCE_RECOMMENDED -eq 1 ]] && echo "  ${GREEN}← RECOMMENDED${RESET}" || true)"

printf "  2) Conservative filtered -dusage=10 -musage=5%s\n" \
    "$([[ $BALANCE_RECOMMENDED -eq 2 ]] && echo "  ${GREEN}← RECOMMENDED${RESET}" || true)"

printf "  3) Moderate filtered     -dusage=25 -musage=10%s\n" \
    "$([[ $BALANCE_RECOMMENDED -eq 3 ]] && echo "  ${GREEN}← RECOMMENDED${RESET}" || true)"

echo "  4) FULL balance          no filters  ${RED}← NOT routine maintenance${RESET}"

echo
echo "${BOLD}Recommendation:${RESET}"
echo "${GREEN}$RECOMMEND_REASON${RESET}"

if AGE_BAL="$(file_age_seconds "$LAST_BALANCE_FILE")"; then
    LAST_BALANCE_TS="$(cat "$LAST_BALANCE_FILE")"

    echo
    echo "Last successful balance:"
    echo "  $(date -d "@$LAST_BALANCE_TS" '+%d %B %Y at %H:%M:%S')"
    echo "  $(format_age "$AGE_BAL") ago"
fi

echo

read -r -p "Selection [default: $BALANCE_RECOMMENDED]: " BALANCE_CHOICE
BALANCE_CHOICE="${BALANCE_CHOICE:-$BALANCE_RECOMMENDED}"

# ============================================================
# INTERPRET BALANCE SELECTION
# ============================================================

case "$BALANCE_CHOICE" in
    0)
        RUN_BALANCE=0
        BALANCE_LABEL="Skipped (scrub only)"
        ;;
    1)
        RUN_BALANCE=1
        BALANCE_ARGS=(-dusage=5 -musage=5)
        BALANCE_LABEL="Light filtered (data≤5%, metadata≤5%)"
        ;;
    2)
        RUN_BALANCE=1
        BALANCE_ARGS=(-dusage=10 -musage=5)
        BALANCE_LABEL="Conservative filtered (data≤10%, metadata≤5%)"
        ;;
    3)
        RUN_BALANCE=1
        BALANCE_ARGS=(-dusage=25 -musage=10)
        BALANCE_LABEL="Moderate filtered (data≤25%, metadata≤10%)"
        ;;
    4)
        echo
        echo "${RED}${BOLD}WARNING: FULL BALANCE may relocate a very large amount of data.${RESET}"
        echo
        echo "It is NOT recommended as routine maintenance"
        echo "on SSD, NVMe, or HDD."
        echo

        read -r -p "Type FULL to confirm: " confirm

        if [[ "$confirm" != "FULL" ]]; then
            echo
            echo "Full balance cancelled."
            exit 0
        fi

        RUN_BALANCE=1
        BALANCE_ARGS=()
        BALANCE_LABEL="FULL BALANCE"
        ;;
    *)
        echo "Invalid selection."
        exit 1
        ;;
esac

# ============================================================
# CAPTURE AND SHOW BTRFS STATISTICS BEFORE MAINTENANCE
# ============================================================

BEFORE_STATS="$(render_btrfs_stats)"

printf '%s' "$CLEAR"

echo "${BOLD}${CYAN}Btrfs Statistics - BEFORE maintenance${RESET}"
separator
echo
printf '%s\n' "$BEFORE_STATS"
echo
separator
echo
printf "Selected balance mode: ${BOLD}%s${RESET}\n" "$BALANCE_LABEL"
echo

read -r -p "Start maintenance? [Y/n]: " start_answer

case "$start_answer" in
    n|N|no|NO|No)
        echo
        echo "Maintenance cancelled."
        exit 0
        ;;
esac

printf '%s' "$HIDE_CURSOR"

# ============================================================
# STEP 1 - SCRUB
# ============================================================

SCRUB_LOG="$(mktemp)"
SCRUB_START="$(date +%s)"
CURRENT_STAGE="scrub"

btrfs scrub start -B "$MOUNT" >"$SCRUB_LOG" 2>&1 &
CHILD_PID=$!

FRAME=0

while kill -0 "$CHILD_PID" 2>/dev/null; do
    ELAPSED=$(($(date +%s) - SCRUB_START))

    SCRUB_STATUS="$(
        btrfs scrub status "$MOUNT" 2>&1 || true
    )"

    header

    status_line \
        "SCRUB" \
        "${GREEN}$(spinner "$FRAME") RUNNING${RESET}   elapsed $(format_time "$ELAPSED")"

    if (( RUN_BALANCE )); then
        status_line "BALANCE" "${DIM}Waiting for scrub${RESET}"
    else
        status_line "BALANCE" "${DIM}Skipped by user${RESET}"
    fi

    separator
    echo
    echo "${BOLD}${CYAN}SCRUB STATUS${RESET}"
    echo
    echo "$SCRUB_STATUS" | sed 's/^/  /'
    echo
    separator
    echo
    echo "${DIM}Verifying data and metadata checksums...${RESET}"
    echo "${DIM}Ctrl+C cancels the active operation.${RESET}"

    FRAME=$((FRAME + 1))
    sleep "$POLL_INTERVAL"
done

wait "$CHILD_PID"
SCRUB_EXIT=$?

CHILD_PID=""
CURRENT_STAGE="idle"

SCRUB_FINAL="$(cat "$SCRUB_LOG")"
SCRUB_DURATION=$(($(date +%s) - SCRUB_START))

# ============================================================
# CHECK SCRUB RESULT
# ============================================================

if [[ $SCRUB_EXIT -ne 0 ]]; then
    header
    status_line "SCRUB" "${RED}✗ FAILED${RESET}"
    status_line "BALANCE" "${RED}NOT STARTED${RESET}"
    separator
    echo
    echo "$SCRUB_FINAL" | sed 's/^/  /'
    echo
    echo "${RED}Scrub failed.${RESET}"
    echo "Balance was not started."
    exit 1
fi

if ! grep -Eqi \
    'Error summary:[[:space:]]*no errors found' \
    <<<"$SCRUB_FINAL"
then
    header
    status_line "SCRUB" "${YELLOW}⚠ ERRORS DETECTED${RESET}"
    status_line "BALANCE" "${RED}NOT STARTED${RESET}"
    separator
    echo
    echo "$SCRUB_FINAL" | sed 's/^/  /'
    echo
    echo "${YELLOW}Scrub did not report a clean filesystem.${RESET}"
    echo
    echo "Balance was deliberately NOT started."
    echo "Investigate the scrub errors first."
    exit 2
fi

date +%s > "$LAST_SCRUB_FILE"

# ============================================================
# STEP 2 - OPTIONAL BALANCE
# ============================================================

BALANCE_DURATION=0

if (( RUN_BALANCE )); then
    BALANCE_LOG="$(mktemp)"
    BALANCE_START="$(date +%s)"
    CURRENT_STAGE="balance"

    btrfs balance start \
        "${BALANCE_ARGS[@]}" \
        "$MOUNT" \
        >"$BALANCE_LOG" 2>&1 &

    CHILD_PID=$!
    FRAME=0

    while kill -0 "$CHILD_PID" 2>/dev/null; do
        ELAPSED=$(($(date +%s) - BALANCE_START))

        BALANCE_STATUS="$(
            btrfs balance status "$MOUNT" 2>&1 || true
        )"

        header

        status_line \
            "SCRUB" \
            "${GREEN}✓ COMPLETE${RESET}   $(format_time "$SCRUB_DURATION")"

        status_line \
            "BALANCE" \
            "${GREEN}$(spinner "$FRAME") RUNNING${RESET}   elapsed $(format_time "$ELAPSED")"

        separator
        echo
        echo "${BOLD}${CYAN}BALANCE STATUS${RESET}"
        echo
        echo "$BALANCE_STATUS" | sed 's/^/  /'
        echo
        separator
        echo
        echo " Selected: ${BOLD}$BALANCE_LABEL${RESET}"
        echo
        echo "${DIM}Ctrl+C cancels the active balance.${RESET}"

        FRAME=$((FRAME + 1))
        sleep "$POLL_INTERVAL"
    done

    wait "$CHILD_PID"
    BALANCE_EXIT=$?

    CHILD_PID=""
    CURRENT_STAGE="idle"

    BALANCE_FINAL="$(cat "$BALANCE_LOG")"
    BALANCE_DURATION=$(($(date +%s) - BALANCE_START))

    if [[ $BALANCE_EXIT -ne 0 ]]; then
        header

        status_line \
            "SCRUB" \
            "${GREEN}✓ COMPLETE${RESET}   $(format_time "$SCRUB_DURATION")"

        status_line "BALANCE" "${RED}✗ FAILED${RESET}"

        separator
        echo
        echo "$BALANCE_FINAL" | sed 's/^/  /'
        echo
        echo "Scrub succeeded and its timestamp was saved."
        echo
        echo "Balance and overall-run timestamps were NOT updated."
        exit 3
    fi

    date +%s > "$LAST_BALANCE_FILE"
fi

# ============================================================
# COMPLETE WORKFLOW SUCCESS
# ============================================================

COMPLETION_TIME="$(date +%s)"
printf '%s\n' "$COMPLETION_TIME" > "$LAST_RUN_FILE"

chmod 644 \
    "$LAST_RUN_FILE" \
    "$LAST_SCRUB_FILE" \
    2>/dev/null || true

if [[ -f "$LAST_BALANCE_FILE" ]]; then
    chmod 644 "$LAST_BALANCE_FILE" 2>/dev/null || true
fi

# ============================================================
# CAPTURE AFTER STATISTICS
# ============================================================

AFTER_STATS="$(render_btrfs_stats)"

TOTAL_DURATION=$((SCRUB_DURATION + BALANCE_DURATION))

# ============================================================
# FINAL RESULTS
# ============================================================

header

status_line \
    "SCRUB" \
    "${GREEN}✓ COMPLETE${RESET}   $(format_time "$SCRUB_DURATION")"

if (( RUN_BALANCE )); then
    status_line \
        "BALANCE" \
        "${GREEN}✓ COMPLETE${RESET}   $(format_time "$BALANCE_DURATION")"
else
    status_line "BALANCE" "${DIM}SKIPPED${RESET}"
fi

separator
echo
echo "${GREEN}${BOLD}✓ Selected Btrfs maintenance completed successfully.${RESET}"
echo
separator

echo
echo "${BOLD}${CYAN}BTRFS STATISTICS — BEFORE${RESET}"
echo
printf '%s\n' "$BEFORE_STATS"

echo
separator

echo
echo "${BOLD}${CYAN}BTRFS STATISTICS — AFTER${RESET}"
echo
printf '%s\n' "$AFTER_STATS"

echo
separator
echo

printf " Scrub time   : %s\n" "$(format_time "$SCRUB_DURATION")"
printf " Balance time : %s\n" "$(format_time "$BALANCE_DURATION")"
printf " Total time   : ${BOLD}%s${RESET}\n" "$(format_time "$TOTAL_DURATION")"

echo

printf \
    " Completed    : %s\n" \
    "$(date -d "@$COMPLETION_TIME" '+%d %B %Y at %H:%M:%S')"

echo
echo "${GREEN}${BOLD}All done.${RESET}"
echo
