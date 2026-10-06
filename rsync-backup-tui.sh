#!/usr/bin/env bash
set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASE="$HERE/rsync-backup-tui-bash-core.sh"
PREV="$HERE/rsync-backup-tui-format-core.sh"
[[ -r $BASE && -r $PREV ]] || { echo "Missing backup TUI core files in $HERE" >&2; exit 1; }

# Load stable v2.0 + previous format-aware v2.1 without their final main calls.
source <(sed '$d' "$BASE")
source <(tail -n +10 "$PREV" | sed '$d')
VER='2.2'

# Keep callable copies of previous implementations.
eval "$(declare -f execute | sed '1s/^execute /execute_v21 /')"
eval "$(declare -f summary | sed '1s/^summary /summary_v21 /')"

SRC_KIND=unknown; SRC_HINT=''; OUT_KIND=new; OVERWRITE=0

kindof(){
  local p=$1 mt
  [[ -b $p ]] && { echo block; return; }
  [[ -f $p ]] && { echo file; return; }
  if [[ -d $p ]]; then
    mt=$(findmnt -T "$p" -rn -o TARGET 2>/dev/null||:)
    [[ -n $mt && $(real "$mt") == "$(real "$p")" ]] && echo mountpoint || echo directory
    return
  fi
  [[ -L $p ]] && echo symlink || echo missing
}
compressed_file(){ [[ $SRC_KIND == file && ${SRC,,} =~ \.(zip|7z|rar|gz|bz2|xz|zst|lz4|jpg|jpeg|png|gif|webp|avif|heic|mp3|aac|ogg|opus|flac|mp4|mkv|webm|mov|avi|pdf|apk|iso)$ ]]; }

source_info(){
  SRC_KIND=$(kindof "$SRC")
  case $SRC_KIND in
    file)
      if compressed_file; then SRC_HINT='Already-compressed/media/archive file detected: direct copy is normally preferable to recompression.'
      else SRC_HINT='Single file detected: direct rsync copy is simplest; tar.zst/ZIP are optional packaging formats.'
      fi;;
    directory) SRC_HINT='Directory detected: rsync is best for browsable/incremental backup; tar.zst/ZIP create one archive.';;
    mountpoint) SRC_HINT='Filesystem mountpoint detected: rsync/tar/ZIP are available; raw IMG is also offered as an advanced option.';;
    block) SRC_HINT='Block device detected: raw IMG/IMG.ZST is the appropriate backup family.';;
    *) SRC_HINT='Unsupported source object.';;
  esac
  ui; echo "Detected source: $SRC_KIND"; echo "Path: $SRC"; echo "Filesystem: ${SFS:-unknown}"; echo; echo "$SRC_HINT"
  if [[ $SRC_KIND == file ]]; then
    echo "Size: $(hb "$(stat -Lc %s "$SRC" 2>/dev/null||echo 0)")"
    have file && echo "MIME: $(file -Lb --mime-type "$SRC" 2>/dev/null||echo unknown)"
  fi
  echo; waitkey
}

strategy(){
  local -a a=() k=(); local d=0 c i
  case $SRC_KIND in
    block)
      a+=('raw .img — exact block image'); k+=(img)
      have zstd && { a+=('raw .img.zst — compressed block image'); k+=(img.zst); d=$((${#k[@]}-1)); };;
    file)
      a+=('rsync file copy — direct and simple'); k+=(rsync)
      have tar && have zstd && { a+=('tar.zst — compressed file archive with Linux metadata'); k+=(tar.zst); }
      have zip && { a+=('ZIP — Windows-friendly packaged file'); k+=(zip); }
      if ! compressed_file && windows_target && have tar && have zstd; then for i in "${!k[@]}"; do [[ ${k[i]} == tar.zst ]]&&d=$i; done; fi;;
    directory)
      a+=('rsync directory — incremental and browsable'); k+=(rsync)
      have tar && have zstd && { a+=('tar.zst — compressed directory archive'); k+=(tar.zst); }
      have zip && { a+=('ZIP — Windows-friendly directory archive'); k+=(zip); }
      if windows_target && have tar && have zstd; then for i in "${!k[@]}"; do [[ ${k[i]} == tar.zst ]]&&d=$i; done; fi;;
    mountpoint)
      a+=('rsync filesystem tree — incremental and browsable'); k+=(rsync)
      have tar && have zstd && { a+=('tar.zst — compressed filesystem archive'); k+=(tar.zst); }
      have zip && { a+=('ZIP — Windows-friendly, limited Linux restore fidelity'); k+=(zip); }
      [[ $SDEV == /dev/* ]] && { a+=('raw .img — whole backing partition'); k+=(img); }
      [[ $SDEV == /dev/* ]] && have zstd && { a+=('raw .img.zst — compressed backing partition'); k+=(img.zst); }
      if windows_target && have tar && have zstd; then for i in "${!k[@]}"; do [[ ${k[i]} == tar.zst ]]&&d=$i; done; fi;;
    *) echo "Unsupported source type: $SRC_KIND" >&2; return 2;;
  esac
  c=$(choose "Backup format for $SRC_KIND source" "$d" "${a[@]}"); STRAT=${k[c]}
  log "STRATEGY kind=$SRC_KIND selected=$STRAT"
  ui; echo "Source type: $SRC_KIND"; echo "Selected format: $STRAT"; echo
  case $STRAT in
    rsync) [[ $SRC_KIND == file ]] && echo 'The file will be copied directly; selecting a directory target places the file inside it.' || echo 'The tree remains directly browsable and efficiently updateable.';;
    tar.zst) echo 'Creates one compressed file. Linux metadata can stay inside the archive even on NTFS/exFAT.'; compressed_file&&echo 'The source already appears compressed, so further compression may save little.';;
    zip) echo 'Creates a Windows-friendly ZIP; Linux ACL/xattr/ownership fidelity is limited.';;
    img*) echo 'Creates a whole-device/partition image; path exclusions do not apply.';;
  esac
  echo; waitkey
}

suggestname(){
  local b s e=''; b=$([[ $SRC == / ]]&&echo linux-root||basename "$SRC"); s=$(date +%Y%m%d-%H%M%S)
  case $STRAT in
    rsync) [[ $SRC_KIND == file ]]&&echo "$b"||echo "$b-backup";;
    tar.zst) e=.tar.zst;((GPG_PASS))&&e+=.gpg;echo "$b-$s$e";;
    zip) echo "$b-$s.zip";;
    img) echo "$b-$s.img";;
    img.zst) e=.img.zst;((GPG_PASS))&&e+=.gpg;echo "$b-$s$e";;
  esac
}
outask(){
  local p=$1 d=$2 x
  while :; do
    x=$(ask "$p" "$d")
    if [[ $x == /* ]]; then
      inside "$x" "$DST" && { echo "$x"; return; }
      ui; echo "Output must remain inside selected destination mount: $DST"; echo "Rejected: $x"; echo; waitkey
    else echo "$DST/${x#/}"; return
    fi
  done
}
altname(){
  local p=$1 s; s=$(date +%Y%m%d-%H%M%S)
  case $p in
    *.tar.zst.gpg) echo "${p%.tar.zst.gpg}-$s.tar.zst.gpg";;
    *.tar.zst) echo "${p%.tar.zst}-$s.tar.zst";;
    *.img.zst.gpg) echo "${p%.img.zst.gpg}-$s.img.zst.gpg";;
    *.img.zst) echo "${p%.img.zst}-$s.img.zst";;
    *.img) echo "${p%.img}-$s.img";;
    *.zip) echo "${p%.zip}-$s.zip";;
    *) echo "$p-$s";;
  esac
}
resolve_out(){
  local def=$1 c fname
  OVERWRITE=0
  while :; do
    OUT_KIND=$(kindof "$OUT")
    case "$STRAT:$SRC_KIND:$OUT_KIND" in
      rsync:file:directory|rsync:file:mountpoint)
        ui; echo "Detected destination directory: $OUT"; echo "Suggested file: $OUT/$(basename "$SRC")"; echo
        yes 'Place the source file inside this directory?' Y && { OUT="${OUT%/}/$(basename "$SRC")"; continue; }
        OUT=$(outask 'Another destination path/name' "$def");;
      rsync:file:file)
        ui; echo "Destination file exists: $OUT"; echo 'rsync can update/replace it if the source differs.'; echo
        c=$(choose 'Existing file' 1 'Use/update this file' 'Use a new timestamped filename' 'Enter another path/name')
        case $c in 0)OVERWRITE=1;return;;1)OUT=$(altname "$OUT");return;;2)OUT=$(outask 'Destination path/name' "$def");;esac;;
      rsync:directory:directory|rsync:directory:mountpoint|rsync:mountpoint:directory|rsync:mountpoint:mountpoint)
        ui; echo "Existing destination directory: $OUT"; echo 'Suggested action: update/merge it with rsync.'; ((RSYNC_DELETE))&&echo 'WARNING: --delete is enabled.'; echo
        yes 'Use/update this directory?' Y&&return
        OUT=$(outask 'Another backup directory' "$def");;
      rsync:directory:file|rsync:mountpoint:file)
        ui; echo "A file exists where a directory is required: $OUT"; echo "Suggested: $(altname "$OUT")"; echo
        yes 'Use the suggested new directory?' Y&&{ OUT=$(altname "$OUT");return; }
        OUT=$(outask 'Another backup directory' "$def");;
      *:*:directory|*:*:mountpoint)
        fname=$(suggestname); ui; echo "Detected destination directory: $OUT"; echo "Format '$STRAT' creates a file."; echo "Suggested: $OUT/$fname"; echo
        yes 'Create the output file inside this directory?' Y&&{ OUT="${OUT%/}/$fname";continue; }
        OUT=$(outask 'Another output file path/name' "$def");;
      *:*:file)
        ui; echo "Output file already exists: $OUT"; echo 'Safe default: do not overwrite it.'; echo
        c=$(choose 'Existing output' 1 'Overwrite existing file' 'Use a new timestamped filename' 'Enter another path/name')
        case $c in 0)OVERWRITE=1;return;;1)OUT=$(altname "$OUT");return;;2)OUT=$(outask 'Output path/name' "$def");;esac;;
      *:*:missing) return;;
      *) return;;
    esac
  done
}
nameout(){
  local d p; d=$(suggestname)
  if [[ $STRAT == rsync && $SRC_KIND == file ]]; then p='Destination file or directory'
  elif [[ $STRAT == rsync ]]; then p='Backup directory'
  else p="Output file or directory for $STRAT"
  fi
  OUT=$(outask "$p" "$d"); resolve_out "$d"; OUT_KIND=$(kindof "$OUT"); [[ $OUT_KIND == missing ]]&&OUT_KIND=new
  log "OUTPUT kind=$OUT_KIND path=$OUT overwrite=$OVERWRITE"
}

summary(){
  summary_v21
  echo "Detected source object: $SRC_KIND"
  echo "Detected/planned output object: $OUT_KIND"
  echo "Output overwrite/update approved: $([[ $OVERWRITE == 1 ]]&&echo yes||echo no)"
  echo "Suggestion: $SRC_HINT"
  echo
}

execute_file(){
  local sudo= need=0 flags p sp sn; local -a a=()
  [[ ! -w $DST ]]&&need=1; ((need))&&priv; ((EUID!=0&&need))&&sudo='sudo -n '
  mkdir -p "$(dirname "$OUT")" 2>/dev/null||$sudo mkdir -p "$(dirname "$OUT")"
  case $STRAT in
    rsync)
      flags='-aS';((RSYNC_ACL))&&flags+='A';((RSYNC_XATTR))&&flags+='X';((RSYNC_HARD))&&flags+='H'
      a=(rsync "$flags" --info=progress2 --stats);((RSYNC_NUMID))&&a+=(--numeric-ids);a+=("$SRC" "$OUT")
      ((EUID!=0&&need))&&runlog rsync-file sudo -n "${a[@]}"||runlog rsync-file "${a[@]}";;
    tar.zst)
      ((OVERWRITE))&&rm -f -- "$OUT";sp=$(dirname "$SRC");sn=$(basename "$SRC")
      a=(tar -C "$sp" -cpf -);((TAR_META))&&a=(tar --acls --xattrs --numeric-owner -C "$sp" -cpf -)
      p="${sudo}$(q "${a[@]}") $(printf %q "$sn") | zstd -q -$ZLVL -T$ZTH -c"
      if ((GPG_PASS));then export GPG_TTY="$(tty 2>/dev/null||:)";runshell tar-file-gpg "$p | gpg --symmetric --cipher-algo AES256 --output $(printf %q "$OUT")";else runshell tar-file "$p > $(printf %q "$OUT")";fi;;
    zip)
      ((OVERWRITE))&&rm -f -- "$OUT";sp=$(dirname "$SRC");sn=$(basename "$SRC");a=(zip "-$ZIP_LEVEL");((ZIP_PASS))&&a+=(-e);((ZIP_SPLIT))&&a+=(-s "$ZIP_SPLIT_SIZE");a+=("$OUT" "$sn")
      (cd "$sp"&&runlog zip-file "${a[@]}");;
  esac
}
execute(){
  if [[ $SRC_KIND == file ]]; then execute_file; return; fi
  if ((OVERWRITE))&&[[ $STRAT != rsync && -e $OUT ]];then
    if [[ -w $(dirname "$OUT") ]];then rm -f -- "$OUT";else priv;sudo -n rm -f -- "$OUT";fi
  fi
  mkdir -p "$(dirname "$OUT")" 2>/dev/null||:
  execute_v21
}

self(){ [[ $(iosize 480) -ge 67108864 && $(iosize 10000) -eq 1073741824 ]];[[ $(kindof /) == mountpoint ]];echo 'self-test: OK'; }

main(){
  [[ ${1:-} == --self-test ]]&&{ self;return; };[[ -t 0 && -t 1 ]]||{ echo 'TTY required';exit 1;};tput civis 2>/dev/null||:
  local rq;rq=$(ask 'Source file or directory' /);[[ -e $rq || -b $rq ]]||{ ui;echo "Source does not exist: $rq";waitkey;return 2;};SRC=$(real "$rq");SRC_KIND=$(kindof "$SRC")
  if [[ $SRC_KIND == block ]];then SDEV=$SRC;SFS=$(lsblk -ndo FSTYPE "$SRC" 2>/dev/null|head -1);SFS=${SFS:-unknown};else read -r _ SDEV SFS _ <<<"$(mountinfo "$SRC")";fi
  SMEDIA=$(media "$SDEV" "$SFS");SLINK=$(linkmb "$SDEV");explain;source_info
  choose_dst;[[ -d $DST && $(findmnt -T "$DST" -rn -o TARGET) == "$DST" ]]||{ echo 'Destination must be a mountpoint';exit 2;};read -r _ DDEV DFS _ <<<"$(mountinfo "$DST")";DMEDIA=$(media "$DDEV" "$DFS");DLINK=$(linkmb "$DDEV")
  log "SRC=$SRC KIND=$SRC_KIND DEV=$SDEV FS=$SFS MEDIA=$SMEDIA LINK=$SLINK";log "DST=$DST DEV=$DDEV FS=$DFS MEDIA=$DMEDIA LINK=$DLINK"
  if [[ $SRC_KIND == directory || $SRC_KIND == mountpoint ]];then base_ex;mount_selector;else EX=();fi
  yes 'Run safe zstd CPU benchmark in tmpfs?' Y&&cpu_bench||:;yes 'Run destination I/O benchmark with temporary file?' N&&io_bench||:
  strategy;format_options;home_root;passwords;nameout;summary
  yes 'Confirmation 1/2: Is this plan correct?' N||return;yes 'Confirmation 2/2: Are you SURE you want to start?' N||return
  local r;if execute;then r=0;else r=$?;fi;ui;((r==0))&&echo "Backup completed. Output: $OUT"||echo "Backup failed rc=$r";echo "Log: $LOG";return "$r"
}
log "START app=$APP ver=$VER uid=$UID bash=$BASH_VERSION";main "$@"
