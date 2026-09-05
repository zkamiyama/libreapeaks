#!/usr/bin/env python3
"""Build real extensions and extract pinned REAPER on disposable CI workers."""
from __future__ import annotations
import argparse, hashlib, json, os, pathlib, platform, shutil, subprocess, tarfile, urllib.request
ROOT = pathlib.Path(__file__).resolve().parents[2]
RESULTS = ROOT / 'host-results'
HOST = ROOT / 'host-runtime'

def run(*args, input_text=None):
    print('+', *map(str,args), flush=True)
    subprocess.run(list(map(str,args)), check=True, cwd=ROOT, timeout=300, input=input_text, text=True)

def build():
    rel = ROOT / 'extensions/reaper_rpkx/target/release'
    lib = rel / ('rpkx_bridge.lib' if os.name == 'nt' else 'librpkx_bridge.a')
    if not lib.is_file(): raise RuntimeError(f'Rust static library missing: {lib}')
    for directory,hooks in [('host-build','OFF'),('host-diagnostic','ON')]:
        run('cmake','-S',ROOT/'extensions/reaper_rpkx','-B',ROOT/directory,'-DCMAKE_BUILD_TYPE=Release',f'-DREAPER_SDK={ROOT / ".host-sdk"}',f'-DWDL_ROOT={ROOT / "third_party/WDL"}',f'-DBRIDGE_LIBRARY={lib}',f'-DLRPK_ENABLE_TEST_HOOKS={hooks}')
        run('cmake','--build',ROOT/directory,'--config','Release','--parallel','2')

def install():
    HOST.mkdir(exist_ok=True); RESULTS.mkdir(exist_ok=True)
    sysname = platform.system()
    name = {'Linux':'reaper779_linux_x86_64.tar.xz','Darwin':'reaper779_universal.dmg','Windows':'reaper779_x64-install.exe'}[sysname]
    archive = HOST / name
    url = 'https://www.reaper.fm/files/7.x/' + name
    print('Downloading',url,flush=True)
    with urllib.request.urlopen(url, timeout=120) as src, archive.open('wb') as out: shutil.copyfileobj(src,out)
    info={'url':url,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'platform':platform.platform(),'machine':platform.machine()}
    if sysname=='Linux':
        with tarfile.open(archive) as t: t.extractall(HOST,filter='data')
        exe=next(p for p in HOST.rglob('reaper') if p.is_file())
    elif sysname=='Darwin':
        mount=HOST/'mount'; mount.mkdir(exist_ok=True)
        # Authorized, disposable evaluation install. Keep the publisher EULA
        # intact and answer its normal hdiutil prompt; never alter the host.
        run('hdiutil','attach','-nobrowse','-readonly','-mountpoint',mount,archive,input_text='Y\n')
        try: run('ditto',next(mount.glob('*.app')),HOST/'REAPER.app')
        finally: run('hdiutil','detach',mount)
        exe=HOST/'REAPER.app/Contents/MacOS/REAPER'
    else:
        # NSIS installation requests elevation (1223 on unattended workers).
        # Extract the publisher archive into a private portable runtime instead.
        dest=HOST/'app'; run('7z','x','-y',f'-o{dest}',archive)
        candidates=list(dest.rglob('reaper.exe'))
        if len(candidates)!=1: raise RuntimeError(f'No unique extracted REAPER: {candidates}')
        exe=candidates[0]
    if not exe.is_file(): raise RuntimeError(f'No REAPER executable: {exe}')
    ext={'Linux':'.so','Darwin':'.dylib','Windows':'.dll'}[sysname]
    for key,directory in [('plugin','host-build'),('diagnostic_plugin','host-diagnostic')]:
        plugins=list((ROOT/directory).rglob('reaper_rpkx'+ext))
        if len(plugins)!=1: raise RuntimeError(f'Expected one extension: {plugins}')
        info[key]=str(plugins[0].resolve());info[key+'_sha256']=hashlib.sha256(plugins[0].read_bytes()).hexdigest()
    info.update(reaper=str(exe.resolve()),reaper_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),commit=os.getenv('GITHUB_SHA','local'))
    (RESULTS/'environment.json').write_text(json.dumps(info,indent=2)+'\n')
    print(json.dumps(info,indent=2),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--build-only',action='store_true');p.add_argument('--install-only',action='store_true');a=p.parse_args()
    if not a.install_only: build()
    if not a.build_only: install()
