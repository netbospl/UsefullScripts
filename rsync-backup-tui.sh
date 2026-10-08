#!/usr/bin/env bash
set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_TUI_DIR=$HERE
CORE="$HERE/rsync-backup-tui-network-core.sh"
[[ -r $CORE ]] || { echo "Missing network-aware core: $CORE" >&2; exit 1; }

source <(sed '$d' "$CORE")
VER='2.4'

eval "$(declare -f transport_choose | sed '1s/^transport_choose /transport_choose_v23 /')"
eval "$(declare -f summary | sed '1s/^summary /summary_v23 /')"
eval "$(declare -f execute | sed '1s/^execute /execute_v23 /')"
eval "$(declare -f choose_dst | sed '1s/^choose_dst /choose_dst_v23 /')"

DST_IS_NFS=0
NFS_SOURCE=''
NFS_FSTYPE=''

choose_dst(){ choose_dst_v23; }

check_terminal(){
  local rows cols
  read -r rows cols < <(stty size 2>/dev/null) || return 0
  [[ $rows =~ ^[0-9]+$ && $cols =~ ^[0-9]+$ ]] || return 0
  if ((rows < 18 || cols < 80)); then
    printf 'Terminal too small (%sx%s). Resize to at least 80 columns × 18 rows and retry.\n' "$cols" "$rows" >&2
    return 2
  fi
}

sftp_quote(){
  local s=$1
  s=${s//\\/\\\\}; s=${s//\"/\\\"}
  printf '"%s"' "$s"
}

sftp_upload_execute(){
  local rc=0 batch local_q remote_q
  local -a cmd=(sftp -P "$SSH_PORT")
  [[ -n $SSH_KEY ]] && cmd+=(-i "$SSH_KEY")
  ((SSH_COMP)) && cmd+=(-C)
  cmd+=("$(ssh_target)")
  local_q=$(sftp_quote "$OUT"); remote_q=$(sftp_quote "$REMOTE_PATH")
  if [[ -d $OUT ]]; then batch="put -r $local_q $remote_q"; else batch="put $local_q $remote_q"; fi
  ui
  echo "Uploading via SFTP to $(ssh_target):$REMOTE_PATH"
  echo 'Authentication is handled by OpenSSH/SFTP; passwords and key passphrases are never logged.'
  echo
  log "OP_START sftp-after"
  log "SFTP target=$(ssh_target) port=$SSH_PORT source=$OUT remote=$REMOTE_PATH recursive=$([[ -d $OUT ]]&&echo yes||echo no)"
  set +e
  printf '%s\n' "$batch" | "${cmd[@]}" 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[1]:-1}
  set -e
  log "OP_END sftp-after rc=$rc"
  return "$rc"
}

sftp_after_execute(){ execute_v23 || return $?; sftp_upload_execute; }

summary(){
  summary_v23
  if ((DST_IS_NFS)); then
    echo 'Mounted network filesystem: NFS'
    echo "NFS export: $NFS_SOURCE"
    echo "NFS type: $NFS_FSTYPE"
    echo 'I/O benchmark meaning: end-to-end NFS path'
  fi
  [[ $TRANSPORT == sftp-after ]] && echo 'SFTP mode: create local backup first, then upload with native OpenSSH sftp'
  echo
}

execute(){ case $TRANSPORT in sftp-after) sftp_after_execute;; *) execute_v23;; esac; }

main(){
  [[ ${1:-} == --self-test ]] && { self; private_host 192.168.1.2; ! private_host 8.8.8.8; echo 'SFTP/NFS self-test: OK'; return; }
  [[ -t 0 && -t 1 ]] || { echo 'TTY required' >&2; exit 1; }
  check_terminal || exit $?
  tput civis 2>/dev/null||:
  offer_remote_unmount
  local unmounted
  unmounted=$(unmounted_devices)
  if [[ -n $unmounted ]]; then
    ui
    echo 'Detected unmounted filesystem devices (left untouched; no automatic mount):'
    printf '  %-18s %-10s %-14s %s\n' DEVICE FILESYSTEM MEDIA SIZE
    while IFS=$'\t' read -r dev fs hw size; do printf '  %-18s %-10s %-14s %s\n' "$dev" "$fs" "$hw" "$size"; done <<<"$unmounted"
    echo
    waitkey
  fi
  local rq r
  rq=$(ask 'Source file or directory' /)
  [[ -e $rq || -b $rq ]] || { ui; echo "Source does not exist: $rq"; waitkey; return 2; }
  SRC=$(real "$rq"); SRC_KIND=$(kindof "$SRC")
  if [[ $SRC_KIND == block ]]; then SDEV=$SRC; SFS=$(lsblk -ndo FSTYPE "$SRC" 2>/dev/null|head -1); SFS=${SFS:-unknown}; else read -r _ SDEV SFS _ <<<"$(mountinfo "$SRC")"; fi
  SMEDIA=$(media "$SDEV" "$SFS"); SLINK=$(linkmb "$SDEV")
  explain; source_info; transport_choose

  if [[ $TRANSPORT == rsync-ssh ]]; then
    network_config || return $?
    remote_fs_probe; DST='/__remote_destination__'
    log "SRC=$SRC KIND=$SRC_KIND DEV=$SDEV FS=$SFS MEDIA=$SMEDIA LINK=$SLINK"
    log "DST_REMOTE=$(ssh_target):$REMOTE_PATH FS=$DFS MEDIA=$DMEDIA"
    if [[ $SRC_KIND == directory || $SRC_KIND == mountpoint ]]; then base_ex; mount_selector; else EX=(); fi
    yes 'Run safe zstd CPU benchmark in tmpfs?' N && cpu_bench || :
    STRAT=rsync
    ui; echo 'Direct network mode selected: rsync over SSH.'; echo 'SCP/SFTP modes are available when you prefer to create a local archive/image first.'; echo; waitkey
    format_options; SIDE=(); ZIP_PASS=0; GPG_PASS=0; OUT="$(ssh_target):$REMOTE_PATH"; OUT_KIND=remote
  else
    choose_dst
    [[ -d $DST && $(findmnt -T "$DST" -rn -o TARGET) == "$DST" ]] || { echo 'Destination must be a mountpoint'; exit 2; }
    read -r _ DDEV DFS _ <<<"$(mountinfo "$DST")"
    DMEDIA=$(media "$DDEV" "$DFS"); DLINK=$(linkmb "$DDEV"); nfs_probe
    log "SRC=$SRC KIND=$SRC_KIND DEV=$SDEV FS=$SFS MEDIA=$SMEDIA LINK=$SLINK"
    log "DST=$DST DEV=$DDEV FS=$DFS MEDIA=$DMEDIA LINK=$DLINK NFS=$DST_IS_NFS NFS_SOURCE=$NFS_SOURCE"
    if [[ $SRC_KIND == directory || $SRC_KIND == mountpoint ]]; then base_ex; mount_selector; else EX=(); fi
    yes 'Run safe zstd CPU benchmark in tmpfs?' Y && cpu_bench || :
    if [[ $DST_IS_NFS == 1 || $DMEDIA == NETWORK || $DMEDIA == FUSE || $DFS == nfs* || $DFS == fuse.* ]]; then log "IO_BENCH skip remote_or_fuse target=$DFS"; echo 'I/O benchmark skipped for network/FUSE destinations; no remote test file will be written.'; else yes 'Run destination I/O benchmark with temporary file?' N && io_bench || :; fi
    strategy; format_options; home_root; passwords; nameout
    if [[ $TRANSPORT == scp-after || $TRANSPORT == sftp-after ]]; then network_config || return $?; fi
  fi

  critical_mounts || { ui; echo 'Backup cancelled: critical filesystem coverage was not confirmed.'; waitkey; return 2; }
  summary
  yes 'Confirmation 1/2: Is this plan correct?' N || return
  yes 'Confirmation 2/2: Are you SURE you want to start?' N || return
  if execute; then r=0; else r=$?; fi
  ui
  if ((r==0)); then
    case $TRANSPORT in rsync-ssh) echo "Network backup completed: $(ssh_target):$REMOTE_PATH";; sftp-after) echo "Backup completed and uploaded via SFTP: $(ssh_target):$REMOTE_PATH";; scp-after) echo "Backup completed and uploaded via SCP: $(ssh_target):$REMOTE_PATH";; *) echo "Backup completed. Output: $OUT";; esac
  else echo "Backup failed rc=$r"; fi
  echo "Log: $LOG"; return "$r"
}

log "START app=$APP ver=$VER uid=$UID bash=$BASH_VERSION"
main "$@"
