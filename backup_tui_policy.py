"""Small policy overlay for Linux Backup TUI recommendations."""
def apply(enh):
    def strategies(sm, dm, cpu, io):
        measured = [x for x in (io.src, io.write) if x > 0]
        if measured:
            bottleneck = min(measured)
            basis = "measured"
        else:
            estimated = []
            for m in (sm, dm):
                if m and m.link and m.link.cap:
                    estimated.append(m.link.cap / 8.0)
            bottleneck = min(estimated) if estimated else None
            basis = "link-estimated"

        rec = "rsync"
        reason = "rsync is preferred for incremental, directly browsable repeated backups."
        if dm and dm.fstype in enh.WINDOWS_FS:
            rec = "tar.zst"
            reason = "Windows-readable target detected: tar.zst retains Linux metadata inside one portable file."
        elif cpu.ok and bottleneck and cpu.many > bottleneck * 1.25 and dm and ("HDD" in dm.media or dm.media == "SD/eMMC"):
            rec = "tar.zst"
            reason = (f"Parallel zstd ({cpu.many:.0f} MiB/s) exceeds the {basis} source/destination "
                      f"bottleneck (~{bottleneck:.0f} MiB/s), so compression should reduce destination I/O.")

        out = [enh.Strat("rsync", "rsync directory — incremental/direct access",
                         reason=reason if rec == "rsync" else "Best for repeated Linux-to-Linux backups.",
                         recommended=rec == "rsync")]
        if enh.shutil.which("tar") and enh.shutil.which("zstd"):
            out.append(enh.Strat("tar.zst", "tar.zst — Linux metadata + multithread zstd", ".tar.zst",
                                 reason, rec == "tar.zst"))
        if enh.shutil.which("zip"):
            out.append(enh.Strat("zip", "ZIP — Windows-first compatibility", ".zip",
                                 "Easy Windows access; loses Linux ACL/xattr/ownership fidelity."))
        if sm and sm.source.startswith("/dev/"):
            out.append(enh.Strat("img", "raw .img — exact partition image", ".img",
                                 "Block-level; ignores exclusions and live mounted images may be inconsistent."))
            if enh.shutil.which("zstd"):
                out.append(enh.Strat("img.zst", "raw .img.zst — compressed partition image", ".img.zst",
                                     "Block-level + multithread zstd; live mounted image may be inconsistent."))
        return out

    enh.strategies = strategies
