#!/usr/bin/env bash
set -Eeuo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CORE="$HERE/rsync-backup-tui-bash-core.sh"
[[ -r "$CORE" ]] || { echo "Missing core: $CORE" >&2; exit 1; }
# Load the previous stable implementation without executing its final main call.
# The wrapper below overrides only format-aware choices, summary and execution.
source <(sed '$d' "$CORE")
VER='2.1'

RSYNC_ACL=1; RSYNC_XATTR=1; RSYNC_HARD=1; RSYNC_NUMID=1; RSYNC_DELETE=0
TAR_META=1
ZIP_LEVEL=6; ZIP_SPLIT=0; ZIP_SPLIT_SIZE='3900m'
IMG_LIVE_OK=0

linux_target(){ [[ $DFS =~ ^(btrfs|ext[234]|xfs|f2fs)$ ]]; }
windows_target(){ [[ $DFS =~ ^(ntfs|ntfs3|fuseblk|exfat|vfat)$ ]]; }

format_options(){
  local c a=()
  ui
  echo "Selected format: $STRAT"
  echo
  case $STRAT in
    rsync)
      echo 'rsync keeps files directly browsable and supports efficient incremental reruns.'
      if linux_target; then
        echo 'Linux target detected: ACLs, xattrs, numeric ownership and hard links can be preserved faithfully.'
      else
        echo "Target filesystem: $DFS"
        echo 'Windows/portable filesystems cannot faithfully represent every Linux ACL/xattr/UID/GID.'
        echo 'For a Linux system backup on NTFS/exFAT, tar.zst is usually safer because metadata stays inside the archive.'
      fi
      echo
      waitkey
      RSYNC_ACL=0; RSYNC_XATTR=0; RSYNC_HARD=0; RSYNC_NUMID=0; RSYNC_DELETE=0
      yes 'Preserve ACLs (-A)?' "$([[ $(linux_target; echo $?) == 0 ]]&&echo Y||echo N)" && RSYNC_ACL=1 || :
      yes 'Preserve extended attributes/xattrs (-X)?' "$([[ $(linux_target; echo $?) == 0 ]]&&echo Y||echo N)" && RSYNC_XATTR=1 || :
      yes 'Preserve hard links (-H)?' Y && RSYNC_HARD=1 || :
      yes 'Preserve numeric UID/GID (--numeric-ids)?' "$([[ $(linux_target; echo $?) == 0 ]]&&echo Y||echo N)" && RSYNC_NUMID=1 || :
      ui
      echo 'Optional mirror mode:'
      echo '  --delete removes destination files that no longer exist in the source.'
      echo '  It is useful for an exact mirror, but it can destroy older backup-only files.'
      echo
      yes 'Enable destructive mirror deletions (--delete)?' N && RSYNC_DELETE=1 || :
      ;;
    tar.zst)
      echo 'tar.zst stores a Linux-aware tar archive and compresses it with parallel zstd.'
      echo 'ACLs, xattrs, owners and permissions can remain inside the archive even when the destination is NTFS/exFAT.'
      echo 'Unlike rsync, it is not directly incremental and individual files require archive extraction.'
      if [[ $SFS == btrfs ]]; then
        echo 'Btrfs snapshots remain excluded by the source rules unless you explicitly re-add them.'
      fi
      echo
      waitkey
      TAR_META=0
      yes 'Store Linux ACLs, xattrs and numeric ownership in the tar archive?' Y && TAR_META=1 || :
      a=('Level 1 — fastest / least compression' 'Level 3 — balanced [recommended]' 'Level 6 — stronger compression / more CPU' 'Level 9 — high compression / slower')
      c=$(choose 'zstd compression level' 1 "${a[@]}")
      case $c in 0) ZLVL=1;; 1) ZLVL=3;; 2) ZLVL=6;; 3) ZLVL=9;; esac
      ;;
    zip)
      echo 'ZIP prioritizes Windows compatibility and convenient file-by-file access.'
      echo 'It is NOT a faithful Linux system-restore format: ACLs, xattrs, device files and Unix ownership are limited.'
      [[ $DFS == vfat ]] && echo 'FAT32/VFAT detected: files larger than 4 GiB are not supported, so split ZIP is strongly recommended.'
      echo
      waitkey
      a=('Store only (-0) — no compression' 'Fast (-1)' 'Balanced (-6) [recommended]' 'Maximum (-9)')
      c=$(choose 'ZIP compression level' 2 "${a[@]}")
      case $c in 0) ZIP_LEVEL=0;; 1) ZIP_LEVEL=1;; 2) ZIP_LEVEL=6;; 3) ZIP_LEVEL=9;; esac
      ZIP_SPLIT=0
      if [[ $DFS == vfat ]]; then
        yes 'Split ZIP into ~3.9 GiB parts for FAT32 compatibility?' Y && ZIP_SPLIT=1 || :
      elif yes 'Create a split ZIP archive (useful for removable media / transfer)?' N; then
        ZIP_SPLIT=1
        ZIP_SPLIT_SIZE=$(ask 'ZIP split size, e.g. 1900m or 3900m' '3900m')
      fi
      ;;
    img|img.zst)
      echo 'IMG is a block-level image of the entire backing partition/device.'
      echo 'Filesystem exclusions, cache filters and Btrfs snapshot exclusions DO NOT apply: every allocated/unallocated block is represented.'
      echo 'A live mounted read/write filesystem can change during imaging and therefore produce an inconsistent image.'
      echo
      waitkey
      IMG_LIVE_OK=0
      if findmnt -rn -S "${SDEV%%\[*}" >/dev/null 2>&1; then
        yes 'Source block device appears mounted. Continue with a live image anyway?' N && IMG_LIVE_OK=1 || {
          echo 'Image mode cancelled because live block imaging was not confirmed.'
          waitkey
          strategy
          format_options
          return
        }
      else
        IMG_LIVE_OK=1
      fi
      if [[ $STRAT == img.zst ]]; then
        a=('Level 1 — fastest' 'Level 3 — balanced [recommended]' 'Level 6 — stronger compression' 'Level 9 — high compression')
        c=$(choose 'zstd compression level for image' 1 "${a[@]}")
        case $c in 0) ZLVL=1;; 1) ZLVL=3;; 2) ZLVL=6;; 3) ZLVL=9;; esac
      fi
      ;;
  esac
  log "FORMAT_OPTIONS strat=$STRAT rsync_acl=$RSYNC_ACL rsync_xattr=$RSYNC_XATTR rsync_hard=$RSYNC_HARD rsync_numid=$RSYNC_NUMID rsync_delete=$RSYNC_DELETE tar_meta=$TAR_META zip_level=$ZIP_LEVEL zip_split=$ZIP_SPLIT zstd_level=$ZLVL"
}

summary(){
  ui
  cat <<EOF
FINAL PLAN
Source: $SRC | $SDEV | $SFS | $SMEDIA | ${SLINK} Mb/s
Destination: $DST | $DDEV | $DFS | $DMEDIA | ${DLINK} Mb/s
Strategy: $STRAT
Output: $OUT
CPU zstd: 1T=$CPU1 MiB/s all=$CPUALL MiB/s
I/O: write=$IOW MiB/s read=$IOR MiB/s sample=$(hb "$IOSZ")
Separate rsync: ${SIDE[*]:-none}
Log: $LOG
EOF
  case $STRAT in
    rsync)
      cat <<EOF
rsync options:
  preserve ACLs:          $([[ $RSYNC_ACL == 1 ]]&&echo yes||echo no)
  preserve xattrs:        $([[ $RSYNC_XATTR == 1 ]]&&echo yes||echo no)
  preserve hard links:    $([[ $RSYNC_HARD == 1 ]]&&echo yes||echo no)
  preserve numeric IDs:   $([[ $RSYNC_NUMID == 1 ]]&&echo yes||echo no)
  delete destination extras: $([[ $RSYNC_DELETE == 1 ]]&&echo YES||echo no)
EOF
      ;;
    tar.zst)
      cat <<EOF
tar.zst options:
  Linux metadata: $([[ $TAR_META == 1 ]]&&echo ACL/xattr/numeric-owner||echo basic-mode-only)
  zstd level: $ZLVL
  zstd threads: $([[ $ZTH == 0 ]]&&echo all||echo "$ZTH")
  GPG encryption: $([[ $GPG_PASS == 1 ]]&&echo yes||echo no)
EOF
      ;;
    zip)
      cat <<EOF
ZIP options:
  compression level: $ZIP_LEVEL
  password encryption: $([[ $ZIP_PASS == 1 ]]&&echo yes||echo no)
  split archive: $([[ $ZIP_SPLIT == 1 ]]&&echo "$ZIP_SPLIT_SIZE"||echo no)
  Linux metadata fidelity: limited
EOF
      ;;
    img|img.zst)
      cat <<EOF
Image options:
  raw block image: yes
  live mounted source confirmed: $([[ $IMG_LIVE_OK == 1 ]]&&echo yes||echo no)
  exclusions applied: NO
EOF
      [[ $STRAT == img.zst ]] && printf '  zstd level: %s\n  zstd threads: %s\n  GPG encryption: %s\n' "$ZLVL" "$([[ $ZTH == 0 ]]&&echo all||echo "$ZTH")" "$([[ $GPG_PASS == 1 ]]&&echo yes||echo no)"
      ;;
  esac
  echo
  echo 'Exclusions:'
  printf '  %s\n' "${EX[@]}"
  echo
}

execute(){
  local sudo= need=0 x p dir tgt dev flags tarflags=() zipargs=()
  local -a args=()
  [[ $SRC == / || $STRAT == img* || ! -w $DST ]] && need=1
  ((need)) && priv
  ((EUID!=0&&need)) && sudo='sudo -n '
  case $STRAT in
    rsync)
      mkdir -p "$OUT" 2>/dev/null || $sudo mkdir -p "$OUT"
      flags='-aS'
      ((RSYNC_ACL)) && flags+='A'
      ((RSYNC_XATTR)) && flags+='X'
      ((RSYNC_HARD)) && flags+='H'
      args=(rsync "$flags" --info=progress2 --stats)
      ((RSYNC_NUMID)) && args+=(--numeric-ids)
      ((RSYNC_DELETE)) && args+=(--delete)
      for x in "${EX[@]}"; do args+=(--exclude "$x"); done
      args+=("${SRC%/}/" "${OUT%/}/")
      ((EUID!=0&&need)) && runlog rsync sudo -n "${args[@]}" || runlog rsync "${args[@]}"
      ;;
    tar.zst)
      tarflags=(tar -C "$SRC" -cpf -)
      if ((TAR_META)); then tarflags=(tar --acls --xattrs --numeric-owner -C "$SRC" -cpf -); fi
      p="${sudo}$(q "${tarflags[@]}")$(tarex) . | zstd -q -$ZLVL -T$ZTH -c"
      if ((GPG_PASS)); then
        export GPG_TTY="$(tty 2>/dev/null||:)"
        ui; echo 'GPG/pinentry will ask for the passphrase; it is never logged.'; waitkey
        runshell tar_zst_gpg "$p | gpg --symmetric --cipher-algo AES256 --output $(printf %q "$OUT")"
      else
        runshell tar_zst "$p > $(printf %q "$OUT")"
      fi
      ;;
    zip)
      zipargs=(zip -r "-$ZIP_LEVEL")
      ((ZIP_PASS)) && zipargs+=(-e)
      ((ZIP_SPLIT)) && zipargs+=(-s "$ZIP_SPLIT_SIZE")
      zipargs+=("$OUT" .)
      for x in "${EX[@]}"; do p=${x#/}; p=${p%/\*\*\*}; zipargs+=(-x "$p" "$p/*"); done
      ui; ((ZIP_PASS)) && echo 'zip will ask for the password twice; it is never logged.'
      log "ZIP_ENCRYPTED=$ZIP_PASS ZIP_LEVEL=$ZIP_LEVEL ZIP_SPLIT=$ZIP_SPLIT"
      (cd "$SRC" && runlog zip "${zipargs[@]}")
      ;;
    img)
      dev=${SDEV%%\[*}
      runshell img "${sudo}dd if=$(printf %q "$dev") of=$(printf %q "$OUT") bs=16M status=progress conv=fsync"
      ;;
    img.zst)
      dev=${SDEV%%\[*}
      p="${sudo}dd if=$(printf %q "$dev") bs=16M status=progress | zstd -q -$ZLVL -T$ZTH -c"
      if ((GPG_PASS)); then
        export GPG_TTY="$(tty 2>/dev/null||:)"
        runshell img_zst_gpg "$p | gpg --symmetric --cipher-algo AES256 --output $(printf %q "$OUT")"
      else
        runshell img_zst "$p > $(printf %q "$OUT")"
      fi
      ;;
  esac
  if ((${#SIDE[@]})); then
    dir="$OUT.uncompressed"; $sudo mkdir -p "$dir"
    for p in "${SIDE[@]}"; do
      tgt="$dir/${p#/}"; $sudo mkdir -p "$tgt"
      args=(rsync -aAXHS --numeric-ids --info=progress2 --stats "${p%/}/" "${tgt%/}/")
      ((EUID!=0&&need)) && runlog side-rsync sudo -n "${args[@]}" || runlog side-rsync "${args[@]}"
    done
  fi
}

main(){
  [[ ${1:-} == --self-test ]] && { self; return; }
  [[ -t 0 && -t 1 ]] || { echo 'TTY required'; exit 1; }
  tput civis 2>/dev/null||:
  SRC=$(real "$(ask 'Source' /)")
  read -r _ SDEV SFS _ <<<"$(mountinfo "$SRC")"
  SMEDIA=$(media "$SDEV" "$SFS"); SLINK=$(linkmb "$SDEV")
  explain
  choose_dst
  [[ -d $DST && $(findmnt -T "$DST" -rn -o TARGET) == "$DST" ]] || { echo 'Destination must be a mountpoint'; exit 2; }
  read -r _ DDEV DFS _ <<<"$(mountinfo "$DST")"
  DMEDIA=$(media "$DDEV" "$DFS"); DLINK=$(linkmb "$DDEV")
  log "SRC=$SRC DEV=$SDEV FS=$SFS MEDIA=$SMEDIA LINK=$SLINK"
  log "DST=$DST DEV=$DDEV FS=$DFS MEDIA=$DMEDIA LINK=$DLINK"
  base_ex
  mount_selector
  yes 'Run safe zstd CPU benchmark in tmpfs?' Y && cpu_bench || :
  yes 'Run destination I/O benchmark with temporary file?' N && io_bench || :
  strategy
  if [[ $STRAT == tar.zst || $STRAT == img.zst ]]; then
    local n c; local -a a
    n=$(nproc 2>/dev/null||echo 1)
    a=("All threads ($n)" "Half threads ($((n>1?n/2:1)))" 'Single thread')
    c=$(choose 'zstd parallelism' 0 "${a[@]}")
    case $c in 0) ZTH=0;; 1) ZTH=$((n>1?n/2:1));; 2) ZTH=1;; esac
  fi
  format_options
  home_root
  passwords
  nameout
  summary
  yes 'Confirmation 1/2: Is this plan correct?' N || return
  yes 'Confirmation 2/2: Are you SURE you want to start?' N || return
  local r
  if execute; then r=0; else r=$?; fi
  ui
  ((r==0)) && echo "Backup completed. Output: $OUT" || echo "Backup failed rc=$r"
  echo "Log: $LOG"
  return "$r"
}

log "START app=$APP ver=$VER uid=$UID bash=$BASH_VERSION"
main "$@"
