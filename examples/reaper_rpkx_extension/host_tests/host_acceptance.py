#!/usr/bin/env python3
"""Exercise ordinary REAPER actions. No direct peak-build API calls.
Positive cases use the distributable build; only negative controls use the
separately hashed diagnostic build. Workspaces are isolated and disposable.
"""
from __future__ import annotations
import hashlib,json,math,os,pathlib,shutil,struct,sys,time,wave
from host_process import launch
ROOT=pathlib.Path(__file__).resolve().parents[3]
OUT=ROOT/'host-results'; INFO=json.loads((OUT/'environment.json').read_text())
SCRIPT=pathlib.Path(__file__).with_name('host_actions.lua')
FIXED_MTIME=1700000000

def sha(b): return hashlib.sha256(b).hexdigest()
def standard_end(b):
    if len(b)<18 or b[:4] not in (b'RPKN',b'RPKL'): raise ValueError('missing/invalid standard cache')
    ch,n=b[4],b[5];pos=18+8*n
    if len(b)<pos: raise ValueError('truncated layer table')
    for i in range(n):
        div,count=struct.unpack_from('<iI',b,18+i*8)
        if div>0: width=4
        elif div in (-115,-103,-114): width=4
        else: raise ValueError(f'unsupported layer {div}')
        pos+=ch*count*width
    if pos>len(b): raise ValueError('truncated standard')
    return pos

def rpkx_tail(std,mib):
    payload=hashlib.sha256(b'host-acceptance-payload').digest()*((mib*1024*1024)//32)
    head=b'RPKX'+struct.pack('<HHIIQ',1,32,0,1,80+len(payload))+std[10:18]
    return head+b'\x71'*16+b'TEST'+struct.pack('<IIIQQ',1,0,0,80,len(payload))+payload

def fixture(path,fmt='pcm16'):
    rate=48000;frames=rate*10;ch=2
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

def real_done_fields(trace):
    out=[]
    for line in trace.splitlines():
        if not line.startswith('DONE\t'): continue
        fields=dict(x.split('=',1) for x in line.split('\t')[1:] if '=' in x)
        if fields.get('reuse')=='0': out.append(fields)
    return out

def run_case(name,*,plugin=True,action='import',seed=None,tail_mib=None,fmt='pcm16',show=1,genmode=3,stale=False,fail=False):
    case=OUT/name;case.mkdir(parents=True,exist_ok=False)
    media=case/'audio.wav';fixture(media,fmt)
    cache=pathlib.Path(str(media)+'.reapeaks');tail=None
    if seed is not None:
        tail=rpkx_tail(seed,tail_mib) if tail_mib is not None else b''
        cache.write_bytes(seed+tail)
    before=cache.read_bytes() if cache.exists() else None
    if stale: os.utime(media,(FIXED_MTIME+120,FIXED_MTIME+120))
    cfg=case/'reaper.ini'
    cfg.write_text(
        '[REAPER]\npeakcachegenmode='+str(genmode)+'\npeakcachegenrs=300\nshowpeaks='+str(show)+'\n'
        '[audioconfig]\nmode=5\ndummy_srate=48000\ndummy_blocksize=512\n',encoding='utf-8')
    if plugin:
        (case/'UserPlugins').mkdir()
        source=pathlib.Path(INFO['diagnostic_plugin' if fail else 'plugin'])
        shutil.copy2(source,case/'UserPlugins'/source.name)
    if action=='project':
        escaped=media.as_posix().replace('"','')
        (case/'input.rpp').write_text('<REAPER_PROJECT 0.1 7.79 1\n<TRACK\n<ITEM\nPOSITION 0\nLENGTH 10\nVOLPAN 1 0 1 -1\n<SOURCE WAVE\nFILE "'+escaped+'"\n>\n>\n>\n>\n')
    env=dict(os.environ,LRPK_CASE=str(case),LRPK_MEDIA=str(media),LRPK_ACTION=action,LRPK_EXPECT_PLUGIN=str(int(plugin)),LIBREAPEAKS_PLUGIN_LOG=str(case/'plugin.tsv'))
    env.pop('LIBREAPEAKS_TEST_FAIL_AFTER_GENERATE',None)
    if fail: env['LIBREAPEAKS_TEST_FAIL_AFTER_GENERATE']='1'
    started=time.perf_counter()
    rc=launch([INFO['reaper'],'-newinst','-cfgfile',str(cfg),'-new','-nosplash',str(SCRIPT)],env,case)
    result=(case/'result.txt').read_text(errors='replace') if (case/'result.txt').exists() else ''
    trace=(case/'plugin.tsv').read_text(errors='replace') if (case/'plugin.tsv').exists() else ''
    kv=dict(line.split('=',1) for line in result.splitlines() if '=' in line)
    paths=[pathlib.Path(kv[k]) for k in ('peak_write','peak_read') if kv.get(k)] + [cache]
    actual=next((p for p in paths if p.is_file()),cache)
    after=actual.read_bytes() if actual.exists() else None
    row={'name':name,'exit':rc,'wall_s':time.perf_counter()-started,'action':action,'showpeaks':show,'genmode':genmode,'plugin':plugin,'diagnostic':fail,'cache_path':str(actual),'before_present':before is not None,'after_present':after is not None,'result':result,'trace':trace,'errors':[]}
    def require(test,msg):
        if not test: row['errors'].append(msg)
    require(rc==0,'REAPER did not exit successfully')
    require('finished=true' in result,'test script did not finish')
    require('error=' not in result,'script error')
    real_done=real_done_fields(trace)
    if plugin:
        require('plugin=true' in result,'extension API missing');require('LOAD\t' in trace,'extension entrypoint not observed')
        require('final_status=-2' not in result,'source bypassed wrapper')
        if fail:
            row['failure_no_write']=after==before
            row['failure_before_sha256']=sha(before) if before is not None else None
            row['failure_after_sha256']=sha(after) if after is not None else None
            require('TEST_GENERATOR_FAILURE_AFTER_GENERATE' in trace,'diagnostic hook not reached')
            require('GENERATED\t' in trace,'real generator did not return')
            require('failure_after_action=true' in result or 'final_status=-1' in result,'injected failure not surfaced')
            require(row['failure_no_write'],'native fallback or unexpected write after generator failure')
        else:
            require('DIAGNOSTIC_BUILD' not in trace,'positive test accidentally used diagnostic binary')
            require('final_status=2' in result,'plugin job did not become ready')
            if seed is None or stale or action in ('manual','selected','reverse','spectrogram'):
                require(bool(real_done),'no real plugin generation, only reuse/no job')
    if not fail:
        require(after is not None,'cache missing')
        if after is not None:
            try:
                end=standard_end(after);row.update(standard_bytes=end,standard_sha256=sha(after[:end]),file_bytes=len(after))
                if action=='spectrogram': require(any(struct.unpack_from('<i',after,18+i*8)[0]==-103 for i in range(after[5])),'spectrogram layer missing after toggle')
                if tail is not None:
                    require(after[end:]==tail,'RPKX or unrelated tail changed/lost');row['tail_sha256']=sha(after[end:])
                    if real_done:
                        moves=[int(x.get('tail_moved','-1')) for x in real_done]
                        if action=='spectrogram': require(any(x>0 for x in moves),'growing spectrogram rebuild did not relocate RPKX')
                        else: require(all(x==0 for x in moves),'same-size rebuild unnecessarily moved RPKX')
            except (ValueError,struct.error) as e: require(False,str(e))
    row['passed']=not row['errors']
    (case/'summary.json').write_text(json.dumps(row,indent=2)+'\n')
    print(json.dumps(row),flush=True)
    if row['errors']:
        for filename in ('console.txt','actions.txt','startup-windows.json','startup-macos.txt','host-process.json'):
            p=case/filename
            if p.exists(): print('DIAGNOSTIC',name,filename,p.read_text(errors='replace')[-12000:],flush=True)
        print('CACHE_FILES',list(map(str,case.rglob('*.reapeaks'))),flush=True)
    return row,after

def standalone_standard(data):
    return data[:standard_end(data)] if data is not None else None

def compare_standard(row,data,expected,label):
    if data is not None and expected is not None and standalone_standard(data)!=expected:
        row['errors'].append('standard differs from '+label);row['passed']=False

def main():
    rows=[]
    native_row,native_data=run_case('native-wave',plugin=False,action='manual');rows.append(native_row)
    native=standalone_standard(native_data)
    native_stale_row,native_stale_data=run_case('native-stale',plugin=False,action='manual',stale=True);rows.append(native_stale_row)
    native_stale=standalone_standard(native_stale_data)
    native_f32_row,native_f32_data=run_case('native-float32',plugin=False,action='manual',fmt='float32');rows.append(native_f32_row)
    native_f32=standalone_standard(native_f32_data)
    native_spec_row,native_spec_data=run_case('native-spectrogram',plugin=False,action='spectrogram');rows.append(native_spec_row)
    native_spec=standalone_standard(native_spec_data)
    for name,kw in [('plugin-auto',{}),('plugin-float32',{'fmt':'float32'}),('negative-auto',{'fail':True})]:
        row,data=run_case(name,**kw)
        if name=='plugin-auto': compare_standard(row,data,native,'native waveform control')
        if name=='plugin-float32': compare_standard(row,data,native_f32,'native float32 control')
        rows.append(row)
    if native is not None:
        for name,kw in [
            ('plugin-manual',{'action':'manual'}),('plugin-selected',{'action':'selected'}),
            ('plugin-project-stale',{'action':'project','stale':True}),('plugin-import-stale',{'stale':True}),
            ('plugin-spectrogram',{'action':'spectrogram'}),('plugin-reverse',{'action':'reverse'}),
            ('plugin-online',{'action':'online'}),('plugin-genmode-0',{'action':'manual','genmode':0}),
            ('plugin-genmode-1',{'action':'manual','genmode':1}),('plugin-genmode-2',{'action':'manual','genmode':2}),
            ('plugin-genmode-3',{'action':'manual','genmode':3}),('negative-manual',{'action':'manual','fail':True}),
        ]:
            row,data=run_case(name,seed=native,tail_mib=1,**kw)
            if not kw.get('fail'):
                expected=native_spec if name=='plugin-spectrogram' else native_stale if kw.get('stale') else native
                label='native spectrogram control' if name=='plugin-spectrogram' else 'native stale control' if kw.get('stale') else 'native waveform control'
                compare_standard(row,data,expected,label)
            rows.append(row)
    else:rows.append({'name':'seeded-cases','passed':False,'errors':['BLOCKED: native control produced no cache']})
    report={'environment':INFO,'cases':rows,'passed':all(r['passed'] for r in rows),'scope':'Real REAPER 7.79 ordinary import/project/rebuild/profile/offline-online paths with exact same-platform native controls, genmodes 0-3, RPKX byte preservation/relocation checks, float32 RPKL, unwrapped-source public API safety, and injected-failure no-write controls. Creation/long-source coverage is in extended-report.json.'}
    (OUT/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    text=['# REAPER host acceptance',f"Commit: {INFO['commit']}",'','| Case | Result | Error |','|---|---|---|']
    for r in rows:text.append('| '+r['name']+' | '+('PASS' if r['passed'] else 'FAIL')+' | '+'; '.join(r['errors'])+' |')
    text.append('\nPositive cases use the regular plugin; negative controls a separately hashed diagnostic build. Failures are not skips.')
    (OUT/'SUMMARY.md').write_text('\n'.join(text)+'\n')
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:f.write('\n'.join(text)+'\n')
    if not report['passed']:sys.exit(1)
if __name__=='__main__':main()