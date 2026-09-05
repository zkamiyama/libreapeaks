#!/usr/bin/env python3
"""Build the actual C++ extension and install a pinned host on disposable CI workers."""
from __future__ import annotations
import argparse, hashlib, json, os, pathlib, platform, shutil, subprocess, tarfile, urllib.request
ROOT = pathlib.Path(__file__).resolve().parents[2]
RESULTS = ROOT / 'host-results'
HOST = ROOT / 'host-runtime'

def run(*args):
    print('+', *map(str,args), flush=True)
    subprocess.run(list(map(str,args)), check=True, cwd=ROOT)

def build():
    rel = ROOT / 'extensions/reaper_rpkx/target/release'
    lib = rel / ('rpkx_bridge.lib' if os.name == 'nt' else 'librpkx_bridge.a')
    if not lib.is_file():
        candidates = list(rel.glob('*rpkx_bridge*.lib' if os.name == 'nt' else '*rpkx_bridge*.a'))
        if len(candidates) != 1: raise RuntimeError(f'Rust static library missing: {list(rel.iterdir())}')
        lib = candidates[0]
    run('cmake', '-S', ROOT/'extensions/reaper_rpkx', '-B', ROOT/'host-build', '-DCMAKE_BUILD_TYPE=Release', f'-DREAPER_SDK={ROOT / ".host-sdk"}', f'-DWDL_ROOT={ROOT / "third_party/WDL"}', f'-DBRIDGE_LIBRARY={lib}')
    run('cmake', '--build', ROOT/'host-build', '--config', 'Release', '--parallel', '2')

def install():
    HOST.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    sysname = platform.system()
    name = {'Linux':'reaper779_linux_x86_64.tar.xz', 'Darwin':'reaper779_universal.dmg', 'Windows':'reaper779_x64-install.exe'}[sysname]
    archive = HOST / name
    url = 'https://www.reaper.fm/files/7.x/' + name
    with urllib.request.urlopen(url, timeout=120) as src, archive.open('wb') as out: shutil.copyfileobj(src,out)
    info = {'url':url, 'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(), 'platform':platform.platform(), 'machine':platform.machine()}
    if sysname == 'Linux':
        with tarfile.open(archive) as t: t.extractall(HOST, filter='data')
        exe = next(p for p in HOST.rglob('reaper') if p.is_file())
    elif sysname == 'Darwin':
        mount = HOST / 'mount'; mount.mkdir(exist_ok=True)
        run('hdiutil','attach','-nobrowse','-readonly','-mountpoint',mount,archive)
        try:
            app = next(mount.glob('*.app'))
            run('ditto',app,HOST/'REAPER.app')
        finally: run('hdiutil','detach',mount)
        exe = HOST/'REAPER.app/Contents/MacOS/REAPER'
    else:
        dest = HOST/'app'
        run(archive,'/S',f'/D={dest}')
        exe = dest/'reaper.exe'
    if not exe.is_file(): raise RuntimeError(f'No REAPER executable: {exe}')
    ext = {'Linux':'.so','Darwin':'.dylib','Windows':'.dll'}[sysname]
    plugins = list((ROOT/'host-build').rglob('reaper_rpkx'+ext))
    if len(plugins) != 1: raise RuntimeError(f'Expected one extension, got {plugins}')
    info.update(reaper=str(exe.resolve()), plugin=str(plugins[0].resolve()), reaper_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(), plugin_sha256=hashlib.sha256(plugins[0].read_bytes()).hexdigest(), commit=os.getenv('GITHUB_SHA','local'))
    (RESULTS/'environment.json').write_text(json.dumps(info,indent=2)+'\n')
    print(json.dumps(info,indent=2),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--build-only',action='store_true'); p.add_argument('--install-only',action='store_true'); a=p.parse_args()
    if not a.install_only: build()
    if not a.build_only: install()
