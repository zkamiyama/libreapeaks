#!/usr/bin/env python3
"""Real REAPER host integration suite, never a direct call to the build API.
All profiles and workspaces are disposable. Errors are accumulated, never skipped
or relabelled as passes. Missing host load evidence is a failure.
"""
from __future__ import annotations
import hashlib, json, math, os, pathlib, platform, shutil, struct, subprocess, sys, time, wave
ROOT=pathlib.Path(__file__).resolve().parents[2]
OUT=ROOT/'host-results'
INFO=json.loads((OUT/'environment.json').read_text())
SCRIPT=ROOT/'tools/reaper_plugin/host_actions.lua'
FIXED_MTIME=1700000000

def sha(b): return hashlib.sha256(b).hexdigest()
def standard_end(b):
    if len(b)<18 or b[:4] not in (b'RPKN',b'RPKL'): raise ValueError('missing/invalid standard cache')
    ch,n=b[4],b[5]; pos=18+8*n
    for i in range(n):
        div,count=struct.unpack_from('<iI',b,18+i*8)
        if div>0: width=4 if b[:4]==b'RPKN' else 8
        elif div in (-115,-103,-114): width=4
        else: raise ValueError(f'unsupported layer {div}')
        pos+=ch*count*width
    if pos>len(b): raise ValueError('truncated standard')
    return pos

def rpkx_tail(std,mib):
    payload=hashlib.sha256(b'host-acceptance-payload').digest()*((mib*1024*1024)//32)
    stamp=std[10:18]
    head=b'RPKX'+struct.pack('<HHIIQ',1,32,0,1,80+len(payload))+stamp
    entry=b'\x71'*16+b'TEST'+struct.pack('<IIIQQ',1,0,0,80,len(payload))
    return head+entry+payload

def fixture(path,fmt='pcm16'):
    rate=48000; frames=rate*2; ch=2
    vals=[int(16000*math.sin(2*math.pi*997*i/rate)) for i in range(frames)]
    if fmt=='pcm16':
        with wave.open(str(path),'wb') as w:
            w.setnchannels(ch);w.setsampwidth(2);w.setframerate(rate)
            w.writeframes(b''.join(struct.pack('<hh',x,-x) for x in vals))
    else:
        data=b''.join(struct.pack('<ff',x/32768.,-x/32768.) for x in vals)
        form=struct.pack('<HHIIHH',3,ch,rate,rate*ch*4,ch*4,32)
        path.write_bytes(b'RIFF'+struct.pack('<I',36+len(data))+b'WAVEfmt '+struct.pack('<I',16)+form+b'data'+struct.pack('<I',len(data))+data)
    os.utime(path,(FIXED_MTIME,FIXED_MTIME))

def run_case(name, *, plugin=True, action='import', seed=None, tail_mib=None, fmt='pcm16', show=1, genmode=3, stale=False, fail=False):
    case=OUT/name; case.mkdir(parents=True,exist_ok=False)
    media=case/'audio.wav';fixture(media,fmt)
    cache=pathlib.Path(str(media)+'.reapeaks')
    tail=None
    if seed is not None:
        tail=rpkx_tail(seed,tail_mib) if tail_mib is not None else b''
        cache.write_bytes(seed+tail)
    before=cache.read_bytes() if cache.exists() else None
    if stale: os.utime(media,(FIXED_MTIME+120,FIXED_MTIME+120))
    cfg=case/'reaper.ini'
    cfg.write_text('[REAPER]\npeakcachegenmode='+str(genmode)+'\npeakcachegenrs=300\nshowpeaks='+str(show)+'\n',encoding='utf-8')
    if plugin:
        (case/'UserPlugins').mkdir()
        shutil.copy2(INFO['plugin'],case/'UserPlugins'/pathlib.Path(INFO['plugin']).name)
    if action=='project':
        escaped=media.as_posix().replace('"','')
        (case/'input.rpp').write_text('<REAPER_PROJECT 0.1 7.79 1\n<TRACK\n<ITEM\nPOSITION 0\nLENGTH 2\nVOLPAN 1 0 1 -1\n<SOURCE WAVE\nFILE "'+escaped+'"\n>\n>\n>\n>\n')
    env=dict(os.environ,LRPK_CASE=str(case),LRPK_MEDIA=str(media),LRPK_ACTION=action,LRPK_EXPECT_PLUGIN=str(int(plugin)),LIBREAPEAKS_PLUGIN_LOG=str(case/'plugin.tsv'))
    if fail: env['LIBREAPEAKS_TEST_FAIL_GENERATE']='1'
    started=time.perf_counter()
    try:
        with (case/'console.txt').open('wb') as log:
            p=subprocess.run([INFO['reaper'],'-newinst','-cfgfile',str(cfg),'-new','-nosplash',str(SCRIPT)],env=env,stdout=log,stderr=subprocess.STDOUT,timeout=65)
        rc=p.returncode
    except subprocess.TimeoutExpired: rc=-999
    result=(case/'result.txt').read_text(errors='replace') if (case/'result.txt').exists() else ''
    trace=(case/'plugin.tsv').read_text(errors='replace') if (case/'plugin.tsv').exists() else ''
    after=cache.read_bytes() if cache.exists() else None
    row={'name':name,'exit':rc,'wall_s':time.perf_counter()-started,'action':action,'showpeaks':show,'genmode':genmode,'plugin':plugin,'result':result,'trace':trace,'errors':[]}
    def require(test,msg):
        if not test: row['errors'].append(msg)
    require(rc==0,'REAPER did not exit successfully')
    require('finished=true' in result,'test script did not finish')
    require('error=' not in result,'script error')
    if plugin:
        require('plugin=true' in result,'extension API missing')
        require('LOAD\t' in trace,'extension entrypoint not observed')
        require('final_status=-2' not in result,'source bypassed wrapper')
        if fail:
            require('TEST_GENERATOR_FAILURE' in trace,'generator failure hook not reached')
            require('final_status=-1' in result,'injected failure not surfaced')
            require(after==before,'native fallback or unexpected write after generator failure')
        else:
            require('final_status=2' in result,'plugin job did not become ready')
            if seed is None or stale or action in ('manual','selected','reverse','spectrogram'):
                require(any(x.startswith('DONE\t') and '\treuse=0\t' in x for x in trace.splitlines()),'no real plugin generation, only reuse/no job')
    if not fail:
        require(after is not None,'cache missing')
        if after is not None:
            try:
                end=standard_end(after);row.update(standard_bytes=end,standard_sha256=sha(after[:end]),file_bytes=len(after))
                if tail is not None:
                    require(after[end:]==tail,'RPKX or unrelated tail changed/lost')
                    row['tail_sha256']=sha(after[end:])
            except ValueError as e: require(False,str(e))
    row['passed']=not row['errors']
    (case/'summary.json').write_text(json.dumps(row,indent=2)+'\n')
    print(json.dumps({k:row[k] for k in ('name','passed','errors','wall_s')}),flush=True)
    return row,after

def main():
    rows=[]
    native_row,native=run_case('native-wave',plugin=False)
    rows.append(native_row)
    if native is None: raise RuntimeError('Native control failed; refusing to call host integration tested')
    native=native[:standard_end(native)]
    for name,kw in [
        ('plugin-auto',{}),
        ('plugin-manual',{'action':'manual','seed':native,'tail_mib':1}),
        ('plugin-selected',{'action':'selected','seed':native,'tail_mib':1}),
        ('plugin-project-stale',{'action':'project','seed':native,'tail_mib':1,'stale':True}),
        ('plugin-import-stale',{'seed':native,'tail_mib':1,'stale':True}),
        ('plugin-spectrogram',{'action':'spectrogram','seed':native,'tail_mib':1}),
        ('plugin-reverse',{'action':'reverse','seed':native,'tail_mib':1}),
        ('plugin-online',{'action':'online','seed':native,'tail_mib':1}),
        ('plugin-genmode-0',{'genmode':0}),
        ('plugin-genmode-1',{'genmode':1}),
        ('plugin-genmode-2',{'genmode':2}),
        ('plugin-float32',{'fmt':'float32'}),
    ]:
        row,data=run_case(name,**kw)
        if name=='plugin-auto' and data is not None:
            if data[:standard_end(data)]!=native: row['errors'].append('standard differs from native waveform control');row['passed']=False
        rows.append(row)
    report={'environment':INFO,'cases':rows,'passed':all(r['passed'] for r in rows),'scope':'Real host 7.79; no direct build API; recording/render/sink interception and long-source streaming are not yet covered.'}
    (OUT/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    text=['# REAPER host acceptance',f"Commit: {INFO['commit']}",'','| Case | Result | Error |','|---|---|---|']
    for r in rows: text.append('| '+r['name']+' | '+('PASS' if r['passed'] else 'FAIL')+' | '+'; '.join(r['errors'])+' |')
    text.append('\nNo test failure is silently skipped. See case logs for actual generator/reuse evidence.')
    (OUT/'SUMMARY.md').write_text('\n'.join(text)+'\n')
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f: f.write('\n'.join(text)+'\n')
    if not report['passed']: sys.exit(1)
if __name__=='__main__': main()
