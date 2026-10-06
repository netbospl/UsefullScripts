from __future__ import annotations
import curses, json, math, os, re, shlex, shutil, signal, statistics, subprocess, sys, tempfile, time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MIB=1024*1024; MAX_MBPS=6000
WINDOWS_FS={'exfat','fuseblk','ntfs','ntfs3','vfat'}
LINUX_FS={'btrfs','ext2','ext3','ext4','f2fs','xfs'}
VIRTUAL={'autofs','bpf','cgroup','cgroup2','configfs','debugfs','devpts','devtmpfs','efivarfs','fusectl','hugetlbfs','mqueue','proc','pstore','ramfs','securityfs','sysfs','tmpfs','tracefs'}
WIN_JUNK=['/$RECYCLE.BIN/***','/System Volume Information/***','/pagefile.sys','/hiberfil.sys','/swapfile.sys','/$WinREAgent/***','/Windows/Temp/***','/Windows/SoftwareDistribution/Download/***','/Users/*/AppData/Local/Temp/***']
REMOVABLE_JUNK=['/$RECYCLE.BIN/***','/System Volume Information/***','/.Spotlight-V100/***','/.fseventsd/***','/.Trashes/***']
BTRFS_SNAPS=['/.snapshots','/timeshift','/.timeshift','/var/lib/snapper/snapshots']

@dataclass
class Link: label:str='unknown'; mbps:int|None=None; cap:int=0
@dataclass
class Mount:
    target:str; source:str; fstype:str; options:str; total:int=0; free:int=0; disk:str=''; transport:str=''; rota:str='?'; model:str=''; media:str='Unknown'; link:Link=field(default_factory=Link)
@dataclass
class CPU: ok:bool=False; tmpfs:str=''; size:int=0; one:float=0; many:float=0; threads:int=1; error:str=''
@dataclass
class IO: ran:bool=False; src:float=0; write:float=0; read:float=0; src_size:int=0; dst_size:int=0; error:str=''
@dataclass
class Strat:
    kind:str; label:str; ext:str=''; reason:str=''; recommended:bool=False; level:int=3; threads:str='0'

def text(cmd):
    try:return subprocess.check_output(cmd,text=True,stderr=subprocess.DEVNULL).strip()
    except Exception:return ''

def hbytes(n):
    n=float(max(0,n))
    for u in ('B','KiB','MiB','GiB','TiB'):
        if n<1024:return f'{n:.1f} {u}' if u!='B' else f'{int(n)} B'
        n/=1024
    return f'{n:.1f} PiB'

def parent_disk(dev):
    if not dev.startswith('/dev/'):return ''
    cur=os.path.realpath(dev.split('[',1)[0])
    while True:
        p=text(['lsblk','-ndo','PKNAME',cur])
        if not p:return cur
        cur='/dev/'+p.strip()

def sysblock(d):
    try:return (Path('/sys/class/block')/os.path.basename(d)).resolve(strict=True)
    except:return None

def num(s):
    m=re.search(r'([0-9]+(?:\.[0-9]+)?)',s); return float(m.group(1)) if m else None

def link_info(disk,tran,media):
    p=sysblock(disk)
    if tran=='usb' and p:
        for n in (p,*p.parents):
            try:
                f=n/'speed'
                if f.is_file():
                    v=float(f.read_text().strip())
                    if 1<=v<=80000:
                        m=int(v); return Link(f'USB {m} Mb/s' if m<=480 else f'USB {m/1000:g} Gb/s',m,min(m,MAX_MBPS))
            except:pass
    if tran in {'sata','ata'} and p:
        atas={x for x in p.parts if re.fullmatch(r'ata\d+',x)}
        for l in Path('/sys/class/ata_link').glob('link*'):
            try:
                if atas and not atas.intersection(l.resolve().parts):continue
                g=num((l/'sata_spd').read_text())
                if g:
                    m=int(g*1000); return Link(f'SATA {g:g} Gb/s',m,min(m,MAX_MBPS))
            except:pass
    if (os.path.basename(disk).startswith('nvme') or tran=='nvme') and p:
        for n in (p,*p.parents):
            try:
                sf,wf=n/'current_link_speed',n/'current_link_width'
                if sf.is_file() and wf.is_file():
                    g=num(sf.read_text()); w=int(wf.read_text()); m=int(g*w*(800 if g<=5 else 985)); return Link(f'PCIe {sf.read_text().strip()} x{w}',m,min(m,MAX_MBPS))
            except:pass
        return Link('NVMe/PCIe (cap 6 Gb/s)',None,6000)
    caps={'usb':5000,'sata':6000,'ata':6000}
    if media=='SD/eMMC':return Link('SD/eMMC',None,400)
    return Link(f'{tran or media} (estimated)',None,min(caps.get(tran,1500 if 'HDD' in media else 6000),MAX_MBPS))

def classify(source,fstype):
    if fstype in VIRTUAL:return '','','?','','VIRTUAL',Link('virtual',None,0)
    if fstype.startswith('fuse.') or source in {'portal','protondrive'}:return '','','?','','FUSE',Link('FUSE',None,0)
    if not source.startswith('/dev/'):return '','','?','',fstype.upper(),Link(fstype.upper(),None,0)
    d=parent_disk(source); r=text(['lsblk','-ndo','ROTA',d]) or '?'; t=text(['lsblk','-ndo','TRAN',d]).lower(); model=text(['lsblk','-ndo','MODEL',d]); name=os.path.basename(d)
    if name.startswith('nvme') or t=='nvme':m='NVMe SSD'
    elif name.startswith('mmcblk') or t in {'mmc','sdio'}:m='SD/eMMC'
    elif t=='usb' and r=='1':m='USB HDD'
    elif t=='usb' and r=='0':m='USB SSD/flash'
    elif t in {'sata','ata'} and r=='1':m='SATA HDD'
    elif t in {'sata','ata'} and r=='0':m='SATA SSD'
    elif r=='1':m='HDD'
    else:m='SSD/flash'
    return d,t,r,model,m,link_info(d,t,m)

def mounts():
    raw=text(['findmnt','-J','-o','TARGET,SOURCE,FSTYPE,OPTIONS']); out=[]
    try:data=json.loads(raw)
    except:return []
    def walk(ns):
        for n in ns:
            tg,src,fs,opt=[str(n.get(k) or '') for k in ('target','source','fstype','options')]
            if tg:
                try:u=shutil.disk_usage(tg); total,free=u.total,u.free
                except:total=free=0
                d,t,r,mo,me,li=classify(src,fs); out.append(Mount(tg,src,fs,opt,total,free,d,t,r,mo,me,li))
            walk(n.get('children') or [])
    walk(data.get('filesystems') or []); return list({m.target:m for m in out}.values())

def inside(p,parent):
    try:return os.path.commonpath([os.path.realpath(p),os.path.realpath(parent)])==os.path.realpath(parent)
    except:return False

def mount_for(path,ms):
    p=os.path.realpath(path); c=[]
    for m in ms:
        if inside(p,m.target):c.append((len(os.path.realpath(m.target)),m))
    return max(c,default=(0,None),key=lambda x:x[0])[1]

def choose_dest(c,win,ms):
    cand=[m for m in ms if m.target!='/' and m.fstype in WINDOWS_FS|LINUX_FS and 'rw' in m.options.split(',')]
    cand.sort(key=lambda m:(m.target!='/run/media/netbos/ext4HDD',m.target)); labels=[f'{m.target} [{m.media}, {m.fstype}, {m.link.label}, {hbytes(m.free)} free]' for m in cand]+['Enter custom mounted path…']
    i=c.choose_dialog(win,'Destination device / mount',labels,0)
    return os.path.abspath(os.path.expanduser(c.input_dialog(win,'Destination','Mounted destination path:','/run/media/netbos/ext4HDD'))) if i==len(cand) else cand[i].target

def mount_selector(c,win,source,dest,ms):
    sm=mount_for(source,ms); sd=sm.disk if sm else ''; rows=[]
    for m in sorted(ms,key=lambda x:(len(x.target),x.target)):
        if os.path.realpath(m.target)==os.path.realpath(source) or not inside(m.target,source):continue
        if os.path.realpath(m.target)==os.path.realpath(dest): ex,lock,why=True,True,'destination'
        elif m.fstype in VIRTUAL: ex,lock,why=True,True,'runtime/virtual'
        elif m.media in {'FUSE','NETWORK'}: ex,lock,why=True,False,'external/FUSE/network'
        elif sd and m.disk==sd: ex,lock,why=False,False,'same system disk'
        else: ex,lock,why=True,False,'other mounted device'
        rows.append([m,ex,lock,why])
    if not rows:return rows
    idx=off=0
    while True:
        h,_=c.draw_frame(win,'Mounted-device selector','Space include/exclude   Enter accept'); body=max(1,h-9); c.safe_addstr(win,3,4,'[!] mandatory  [X] exclude  [ ] include',curses.A_BOLD)
        if idx<off:off=idx
        if idx>=off+body:off=idx-body+1
        for y,j in enumerate(range(off,min(len(rows),off+body)),start=5):
            m,e,l,w=rows[j]; mark='!' if l else 'X' if e else ' '; c.safe_addstr(win,y,4,f'[{mark}] {m.target} {m.media}/{m.fstype} {m.link.label} ({w})',curses.A_REVERSE if j==idx else 0)
        win.refresh();k=win.getch()
        if k in (curses.KEY_DOWN,ord('j')):idx=min(len(rows)-1,idx+1)
        elif k in (curses.KEY_UP,ord('k')):idx=max(0,idx-1)
        elif k==ord(' ') and not rows[idx][2]:rows[idx][1]=not rows[idx][1]
        elif k in (10,13,curses.KEY_ENTER):return rows

def mount_excludes(source,rows):
    out=[]
    for m,e,_,_ in rows:
        if e:
            r=os.path.relpath(os.path.realpath(m.target),os.path.realpath(source))
            if r!='.':out.append('/'+r.strip('/')+'/***')
    return out

def fs_rules(c,win,source,sm):
    rules=list(c.ROOT_EXCLUSIONS if os.path.realpath(source)=='/' else c.GENERIC_EXCLUSIONS); notes=[]; fs=(sm.fstype if sm else '').lower()
    if fs=='btrfs':
        notes=['Btrfs detected. Snapshot directories are proposed for exclusion because reflink snapshots can expand massively on ext4/NTFS/exFAT.']
        for p in BTRFS_SNAPS:
            q=p if source=='/' else os.path.join(source,p.lstrip('/'))
            if os.path.exists(q):rules.append('/'+os.path.relpath(q,source).strip('/')+'/***')
    elif fs in {'ntfs','ntfs3','fuseblk'}:notes=['NTFS source: Windows paging, hibernation, recycle-bin and update-cache files are excluded by default.'];rules+=WIN_JUNK
    elif fs in {'exfat','vfat'}:notes=[f'{fs.upper()} source: removable-media recycle/index files are excluded by default.'];rules+=REMOVABLE_JUNK
    else:notes=[f'{fs.upper() or "Unknown"} source: cache/tmp exclusions are enabled; Linux metadata is preserved when the target supports it.']
    c.scroll_dialog(win,'Source filesystem detected',[f'Filesystem: {fs or "unknown"}',*notes,'','You can edit every suggested exclusion next.'])
    return list(dict.fromkeys(rules)),notes

def memavail():
    try:
        for l in Path('/proc/meminfo').read_text().splitlines():
            if l.startswith('MemAvailable:'):return int(l.split()[1])*1024
    except:pass
    return 0

def cpu_bench(c,win,ms):
    r=CPU(threads=os.cpu_count() or 1)
    if not shutil.which('zstd'):r.error='zstd missing';return r
    t=next((m for p in ('/dev/shm',f'/run/user/{os.getuid()}','/tmp') for m in ms if m.target==p and m.fstype=='tmpfs' and os.access(p,os.W_OK)),None)
    if not t:r.error='no writable tmpfs';return r
    free=shutil.disk_usage(t.target).free; ma=memavail(); gib=ma/(1024**3) if ma else 0; target=32*MIB if gib and gib<=6 else 64*MIB if gib and gib<=10 else 96*MIB if gib and gib<=18 else 128*MIB
    size=min(target,int(free*.1),int(ma*.03) if ma else target,256*MIB)
    if size<8*MIB:r.error='insufficient safe RAM/tmpfs headroom';return r
    c.draw_frame(win,'Compression benchmark','tmpfs/RAM only');c.safe_addstr(win,5,4,f'{t.target}, sample {hbytes(size)}');c.safe_addstr(win,7,4,'zstd L3: 1 thread vs all threads…');win.refresh()
    wd=tempfile.mkdtemp(prefix='backup-bench-',dir=t.target);f=os.path.join(wd,'sample')
    try:
        chunk=(b'Linux backup benchmark config source log text\n'*4096)[:MIB];rnd=os.urandom(MIB)
        with open(f,'wb') as o:
            for i in range(math.ceil(size/MIB)):o.write((rnd if i%4==3 else chunk)[:min(MIB,size-i*MIB)])
        def speed(th):
            vals=[]
            for _ in range(2):
                st=time.monotonic();p=subprocess.run(['zstd','-q','-3',f'-T{th}','-c',f],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);dt=time.monotonic()-st
                if p.returncode:raise RuntimeError('zstd failed')
                vals.append((size/MIB)/dt)
            return statistics.median(vals)
        r.one,r.many=speed('1'),speed('0');r.ok=True;r.tmpfs=t.target;r.size=size
    except Exception as e:r.error=str(e)
    finally:shutil.rmtree(wd,ignore_errors=True)
    return r

def io_size(link,free=None):
    mbps=max(100,min(MAX_MBPS,(link.cap if link else 1000) or 1000));n=int((mbps/8)*1.4*MIB);n=max(32*MIB,min(1024*MIB,n));n=min(n,int(free*.05)) if free is not None else n;return (n//(4*MIB))*(4*MIB)
def sudo(c,win,why):
    if os.geteuid()==0:return True
    if not shutil.which('sudo'):c.scroll_dialog(win,'sudo required',[why,'sudo is not installed']);return False
    if not c.confirm_dialog(win,'Privileges required',[why,'sudo may ask for your password.'],'request sudo'):return False
    curses.def_prog_mode();curses.endwin();rc=subprocess.call(['sudo','-v']);curses.reset_prog_mode();curses.curs_set(0);win.refresh();return rc==0
def io_bench(c,win,source,dest,sm,dm):
    r=IO();ss=io_size(sm.link if sm else Link());ds=io_size(dm.link if dm else Link(),shutil.disk_usage(dest).free)
    if not c.confirm_dialog(win,'Storage I/O benchmark',[f'Source read-only test: {hbytes(ss)}',f'Destination temp write/read: {hbytes(ds)}',f'Source link: {sm.link.label if sm else "unknown"}',f'Destination link: {dm.link.label if dm else "unknown"}','Sizing is capped at 6 Gb/s and 1 GiB. Temp destination file is removed.'],'run I/O benchmark'):return r
    need=bool(sm and sm.source.startswith('/dev/') and not os.access(sm.source.split('[',1)[0],os.R_OK)) or not os.access(dest,os.W_OK)
    if need and not sudo(c,win,'Storage benchmark needs raw-source read and/or destination write access.'):r.error='sudo denied';return r
    pre=['sudo','-n'] if need and os.geteuid()!=0 else[]
    def dd(args,size):st=time.monotonic();p=subprocess.run(pre+['dd']+args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);dt=time.monotonic()-st;return (size/MIB)/dt if p.returncode==0 else 0
    r.ran=True
    if sm and sm.source.startswith('/dev/'):
        cnt=math.ceil(ss/(4*MIB));r.src_size=cnt*4*MIB;r.src=dd([f'if={sm.source.split("[",1)[0]}','of=/dev/null','bs=4M',f'count={cnt}','iflag=direct','status=none'],r.src_size)
    path=os.path.join(dest,f'.backup-tui-iobench-{os.getpid()}');cnt=math.ceil(ds/(4*MIB));r.dst_size=cnt*4*MIB
    try:
        r.write=dd(['if=/dev/zero',f'of={path}','bs=4M',f'count={cnt}','oflag=direct','conv=fdatasync','status=none'],r.dst_size);r.read=dd([f'if={path}','of=/dev/null','bs=4M','iflag=direct','status=none'],r.dst_size)
    finally:subprocess.run(pre+['rm','-f',path],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return r

def strategies(sm,dm,cpu,io):
    bott=min([x for x in (io.src,io.write) if x>0],default=None);rec='rsync';reason='Incremental, browsable and efficient for repeated backups.'
    if dm and dm.fstype in WINDOWS_FS:rec='tar.zst';reason='Windows-readable target: tar.zst stores Linux metadata inside one portable file.'
    elif cpu.ok and bott and cpu.many>bott*1.25 and dm and ('HDD' in dm.media or dm.media=='SD/eMMC'):rec='tar.zst';reason=f'Parallel zstd ({cpu.many:.0f} MiB/s) exceeds storage bottleneck (~{bott:.0f} MiB/s), reducing destination writes.'
    out=[Strat('rsync','rsync directory — incremental/direct access',reason=reason if rec=='rsync' else 'Best for repeated Linux-to-Linux backups.',recommended=rec=='rsync')]
    if shutil.which('tar') and shutil.which('zstd'):out.append(Strat('tar.zst','tar.zst — Linux metadata + multithread zstd','.tar.zst',reason,rec=='tar.zst'))
    if shutil.which('zip'):out.append(Strat('zip','ZIP — Windows-first compatibility','.zip','Easy Windows access; loses Linux ACL/xattr/ownership fidelity.'))
    if sm and sm.source.startswith('/dev/'):
        out.append(Strat('img','raw .img — exact partition image','.img','Block-level; ignores exclusions and live mounted images may be inconsistent.'))
        if shutil.which('zstd'):out.append(Strat('img.zst','raw .img.zst — compressed partition image','.img.zst','Block-level + multithread zstd; live mounted image may be inconsistent.'))
    return out

def choose_strategy(c,win,sm,dm,cpu,io):
    ss=strategies(sm,dm,cpu,io);d=next((i for i,s in enumerate(ss) if s.recommended),0);s=ss[c.choose_dialog(win,'Backup format / strategy',[('★ RECOMMENDED — ' if x.recommended else '')+x.label for x in ss],d)]
    lines=[s.label,'',s.reason,'']
    if s.kind=='rsync':lines+=['Linux target: preserves permissions/UID/GID/ACL/xattrs with -aAXH.','NTFS/exFAT: compatibility copy cannot represent all Linux metadata.']
    elif s.kind=='tar.zst':lines+=['Preserves Linux metadata inside archive even on NTFS/exFAT.','Uses parallel zstd; requires extraction for ordinary browsing.']
    elif s.kind=='zip':lines+=['Best Windows interoperability.','Not appropriate for exact Linux system restore.']
    else:lines+=['Raw block image includes filesystem internals/free space.','Path and Btrfs snapshot exclusions do not apply. Offline imaging is safer than live imaging.']
    if sm and sm.fstype=='btrfs':lines+=['','Btrfs snapshots are skipped by default in file-level modes to avoid reflink expansion.']
    c.scroll_dialog(win,'Format behaviour',lines)
    return s

def configure(c,win,s,cpu):
    if s.kind not in {'tar.zst','img.zst'}:return s
    n=max(1,cpu.threads);half=max(1,n//2);i=c.choose_dialog(win,'zstd parallelism',[f'All threads ({n})',f'Half ({half})','Single thread'],0);s.threads='0' if i==0 else str(half) if i==1 else '1';s.level=(1,3,6)[c.choose_dialog(win,'zstd level',['1 fastest','3 balanced','6 smaller'],1)];return s

def archive_split(c,win,source,dm,s):
    if s.kind!='tar.zst' or source!='/' or not dm or dm.fstype not in LINUX_FS:return True,True
    c.scroll_dialog(win,'Linux archive layout',['Choose whether /home and /root are compressed inside tar.zst.','If NO, that tree is backed up separately with uncompressed rsync, preserving Linux metadata.'])
    h=c.confirm_dialog(win,'Compress /home?',['YES = inside tar.zst','NO = separate rsync copy'],'compress /home') if os.path.isdir('/home') else True
    r=c.confirm_dialog(win,'Compress /root?',['YES = inside tar.zst','NO = separate rsync copy'],'compress /root') if os.path.isdir('/root') else True
    return h,r

def tar_ex(p):return './'+p.lstrip('/').replace('/***','').rstrip('/')
def zip_ex(p):return p.lstrip('/').replace('/***','/*')
def cmd_for(source,out,ex,s,sm,dm,elev):
    pre=['sudo','-n'] if elev and os.geteuid()!=0 else[]
    if s.kind=='rsync':
        flags=['-rtS','--modify-window=1','--no-perms','--no-owner','--no-group','--omit-dir-times'] if dm and dm.fstype in WINDOWS_FS else ['-aAXHS','--numeric-ids'];cmd=pre+['rsync']+flags+['--info=progress2','--outbuf=L']
        for p in ex:cmd+=['--exclude',p]
        return cmd+[source.rstrip('/')+'/' if source!='/' else '/',out.rstrip('/')+'/']
    if s.kind=='tar.zst':
        tar=['tar','--acls','--xattrs','--numeric-owner','-C',source,'-cpf','-']
        for p in ex:tar+=['--exclude',tar_ex(p)]
        return ['bash','-o','pipefail','-c',' '.join(map(shlex.quote,pre+tar+['.']))+' | '+' '.join(map(shlex.quote,pre+['zstd','-q',f'-{s.level}',f'-T{s.threads}','-o',out]))]
    if s.kind=='zip':
        cmd=pre+['bash','-c','cd "$1" && shift && exec zip -r -q "$1" . "${@:2}"','bash',source,out]
        for p in ex:cmd+=['-x',zip_ex(p)]
        return cmd
    dev=sm.source.split('[',1)[0]
    if s.kind=='img':return pre+['dd',f'if={dev}',f'of={out}','bs=16M','status=progress','conv=fsync']
    return ['bash','-o','pipefail','-c',' '.join(map(shlex.quote,pre+['dd',f'if={dev}','bs=16M','status=progress']))+' | '+' '.join(map(shlex.quote,pre+['zstd','-q',f'-{s.level}',f'-T{s.threads}','-o',out]))]
def needs_sudo(source,out,s):return os.geteuid()!=0 and (source=='/' or s.kind.startswith('img') or not os.access(source,os.R_OK|os.X_OK) or not os.access(os.path.dirname(out) or '.',os.W_OK))
def run_cmd(c,win,cmd,label,source,out,rsync=False):
    Path(os.path.dirname(out) or '.').mkdir(parents=True,exist_ok=True); log=Path.home()/'.local/state/linux-backup-tui'/f'{datetime.now():%Y%m%d-%H%M%S-%f}.log';log.parent.mkdir(parents=True,exist_ok=True);p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1,start_new_session=True);start=time.monotonic();lines=[]
    while p.poll() is None:
        c.draw_frame(win,c.APP_NAME,'Q cancel');c.safe_addstr(win,4,4,label,curses.A_BOLD);c.safe_addstr(win,6,4,f'Source: {source}');c.safe_addstr(win,7,4,f'Output: {out}');c.safe_addstr(win,9,4,f'Elapsed: {time.strftime("%H:%M:%S",time.gmtime(time.monotonic()-start))}');c.safe_addstr(win,11,4,(lines[-1] if lines else 'Running…'));win.refresh()
        if p.stdout:
            import select
            ready,_,_=select.select([p.stdout],[],[],.1)
            if ready:
                line=p.stdout.readline()
                if line:lines.append(line.strip())
        win.nodelay(True);k=win.getch();win.nodelay(False)
        if k in (ord('q'),ord('Q')) and c.confirm_dialog(win,'Cancel?',['Interrupt running backup?'],'cancel'):
            try:os.killpg(p.pid,signal.SIGINT)
            except:pass
            break
    rc=p.wait();log.write_text('\n'.join(lines));return rc,log

def summary(sm,dm,source,dest,out,s,notes,rows,ex,cpu,io,elev,h,r):
    L=['FINAL BACKUP PLAN','',f'Source: {source}',f'Source FS/device: {sm.fstype if sm else "?"} / {sm.source if sm else "?"}',f'Source media/link: {sm.media if sm else "?"} / {sm.link.label if sm else "?"}',f'Destination: {dest}',f'Destination FS/device: {dm.fstype if dm else "?"} / {dm.source if dm else "?"}',f'Destination media/link: {dm.media if dm else "?"} / {dm.link.label if dm else "?"}',f'Output: {out}','',f'Format: {s.label}',f'Recommendation: {s.reason}']
    if s.kind in {'tar.zst','img.zst'}:L+=[f'zstd level {s.level}, threads={"all" if s.threads=="0" else s.threads}']
    if s.kind=='tar.zst' and source=='/' and dm and dm.fstype in LINUX_FS:L += [f'/home compressed: {h}',f'/root compressed: {r}']
    L += ['','Filesystem handling:']+['  '+x for x in notes]
    L += ['',f'CPU benchmark: {cpu.one:.1f} MiB/s 1T / {cpu.many:.1f} MiB/s all threads' if cpu.ok else f'CPU benchmark: {cpu.error or "skipped"}']
    L += [f'I/O benchmark: source {io.src:.1f}, dest write {io.write:.1f}, read {io.read:.1f} MiB/s' if io.ran else 'I/O benchmark: skipped','',f'sudo for backup: {elev}','', 'Mounts:']+[f'  {"EXCLUDE" if x[1] else "INCLUDE"} {x[0].target} ({x[3]})' for x in rows]+['',f'Exclusions ({len(ex)}):']+[f'  {x}' for x in ex]
    return L

def selftest():
    assert 76*MIB<=io_size(Link(cap=480))<=92*MIB;assert 840*MIB<=io_size(Link(cap=5000))<=920*MIB;assert io_size(Link(cap=10000))==1024*MIB;return True

def main(c,win):
    ms=mounts();source=os.path.abspath(os.path.expanduser(c.input_dialog(win,'Backup source','Directory to back up:','/')));dest=choose_dest(c,win,ms);ms=mounts();sm,dm=mount_for(source,ms),mount_for(dest,ms)
    if not os.path.isdir(source) or not os.path.ismount(dest):c.scroll_dialog(win,'Pre-flight failed',['Source must exist and destination must be a mounted filesystem.']);return 2
    ex,notes=fs_rules(c,win,source,sm);rows=mount_selector(c,win,source,dest,ms);ex=list(dict.fromkeys(ex+mount_excludes(source,rows)));ex=c.edit_exclusions(win,ex)
    cpu=cpu_bench(c,win,ms) if c.confirm_dialog(win,'CPU compression benchmark',['Run adaptive zstd benchmark?','Only tmpfs/RAM is used; size scales down with available memory.'],'run') else CPU(error='skipped',threads=os.cpu_count() or 1)
    io=io_bench(c,win,source,dest,sm,dm);s=configure(c,win,choose_strategy(c,win,sm,dm,cpu,io),cpu);home,root=archive_split(c,win,source,dm,s)
    if s.kind=='tar.zst' and source=='/':
        if not home:ex.append('/home/***')
        if not root:ex.append('/root/***')
    base='linux-root-backup' if s.kind=='rsync' else f'linux-root-{datetime.now():%Y%m%d-%H%M%S}{s.ext}';name=c.input_dialog(win,'Output','Directory/file name:',base);out=os.path.join(dest,name if not s.ext or name.endswith(s.ext) else name+s.ext)
    elev=needs_sudo(source,out,s);plan=summary(sm,dm,source,dest,out,s,notes,rows,ex,cpu,io,elev,home,root)
    if not c.confirm_dialog(win,'Review choices — 1/2',plan,'CONFIRM PLAN'):return 0
    if not c.confirm_dialog(win,'Final safety check — 2/2',['You reviewed all choices.','Next action starts real backup I/O.',f'Source: {source}',f'Destination: {out}',f'Format: {s.label}','','Are you absolutely sure?'],'START NOW'):return 0
    if elev and not sudo(c,win,'Backup requires protected-file read and/or destination write access.'):return 1
    cmd=cmd_for(source,out,ex,s,sm,dm,elev);rc,log=run_cmd(c,win,cmd,s.label,source,out,s.kind=='rsync')
    if rc==0 and s.kind=='tar.zst' and source=='/' and dm and dm.fstype in LINUX_FS:
        pre=['sudo','-n'] if elev and os.geteuid()!=0 else[];stem=Path(out).name[:-len(s.ext)]
        for path,use in (('/home',home),('/root',root)):
            if not use and os.path.isdir(path):
                d=os.path.join(dest,stem+'-uncompressed',path.lstrip('/'));rc,log=run_cmd(c,win,pre+['rsync','-aAXHS','--numeric-ids','--info=progress2',path+'/',d+'/'],f'Uncompressed {path}',path,d,True)
                if rc:break
    c.scroll_dialog(win,'Backup completed' if rc==0 else 'Backup failed',[f'Exit code: {rc}',f'Output: {out}',f'Log: {log}']);return rc
