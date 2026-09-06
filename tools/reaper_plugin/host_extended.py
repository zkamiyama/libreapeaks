#!/usr/bin/env python3
"""Extended real-host acceptance for ordinary REAPER workflows that create or
reprofile media. This suite never calls RPKX_ForceBuild or PCM_Source_BuildPeaks.
It runs after host_acceptance.py and reuses its proven native controls.
"""
from __future__ import annotations
import hashlib,json,math,os,pathlib,shutil,struct,sys,time,wave
from host_process import launch
from host_acceptance import ROOT,OUT,INFO,FIXED_MTIME,standard_end,rpkx_tail,run_case as base_run_case
SCRIPT=ROOT/'tools/reaper_plugin/host_extended.lua'

def sha(b:bytes)->str:return hashlib.sha256(b).hexdigest()
def standalone(data:bytes|None)->bytes|None:return data[:standard_end(data)] if data is not None else None

def repeated_pcm16(path:pathlib.Path,seconds:int)->None:
    rate=48000;ch=2
    one=bytearray(rate*ch*2)
    for i in range(rate):
        x=int(16000*math.sin(2*math.pi*997*i/rate))
        struct.pack_into('<hh',one,i*4,x,-x)
    with wave.open(str(path),'wb') as w:
        w.setnchannels(ch);w.setsampwidth(2);w.setframerate(rate)
        for _ in range(seconds):w.writeframesraw(one)
    os.utime(path,(FIXED_MTIME,FIXED_MTIME))

def norm(p:str|pathlib.Path)->str:
    try:return os.path.normcase(str(pathlib.Path(p).resolve(strict=False)))
    except Exception:return os.path.normcase(os.path.abspath(str(p)))

def trace_jobs(trace:str):
    begins={};generated=set();done={}
    for line in trace.splitlines():
        parts=line.split('\t');tag=parts[0] if parts else ''
        fields=dict(x.split('=',1) for x in parts[1:] if '=' in x)
        jid=fields.get('id')
        if not jid:continue
        if tag=='BEGIN':begins[jid]=fields
        elif tag=='GENERATED':generated.add(jid)
        elif tag=='DONE':done[jid]=fields
    return begins,generated,done

def cleanup_payload(case:pathlib.Path):
    for pat in ('*.wav','*.reapeaks','*.guard'):
        for p in case.rglob(pat):
            try:p.unlink()
            except OSError:pass

def run_ext(name:str,*,plugin:bool=True,action:str='import',seed:bytes|None=None,tail_mib:int|None=None,
            show:int=1,genmode:int=3,seconds:int=10,media_from:pathlib.Path|None=None,hardlink:bool=False,
            expect_new:bool=False,expect_tokens=(),forbid_tokens=(),expected_standard:bytes|None=None,
            expect_stream:bool|None=None,expect_tail_move:str|None=None,cleanup:bool=False,
            require_generation:bool=True,allow_idle:bool=False):
    case=OUT/('extended-'+name);case.mkdir(parents=True,exist_ok=False)
    media=case/'audio.wav'
    if media_from is None:repeated_pcm16(media,seconds)
    elif hardlink:
        try:os.link(media_from,media)
        except OSError:shutil.copy2(media_from,media)
    else:shutil.copy2(media_from,media)
    cache=pathlib.Path(str(media)+'.reapeaks');tail=None
    if seed is not None:
        tail=rpkx_tail(seed,tail_mib) if tail_mib is not None else b''
        cache.write_bytes(seed+tail)
    before=cache.read_bytes() if cache.exists() else None
    cfg=case/'reaper.ini'
    cfg.write_text(
        f'[REAPER]\npeakcachegenmode={genmode}\npeakcachegenrs=300\nshowpeaks={show}\n'
        '[audioconfig]\nmode=5\ndummy_srate=48000\ndummy_blocksize=512\n',
        encoding='utf-8')
    if plugin:
        (case/'UserPlugins').mkdir();src=pathlib.Path(INFO['plugin']);shutil.copy2(src,case/'UserPlugins'/src.name)
    env=dict(os.environ,LRPK_CASE=str(case),LRPK_MEDIA=str(media),LRPK_ACTION=action,LRPK_EXPECT_PLUGIN=str(int(plugin)),LIBREAPEAKS_PLUGIN_LOG=str(case/'plugin.tsv'))
    env.pop('LIBREAPEAKS_TEST_FAIL_AFTER_GENERATE',None)
    started=time.perf_counter();cmd=[INFO['reaper'],'-newinst','-cfgfile',str(cfg),'-new','-nosplash',str(SCRIPT)]
    rc=launch(cmd,env,case,timeout=110)
    result=(case/'result.txt').read_text(errors='replace') if (case/'result.txt').exists() else ''
    trace=(case/'plugin.tsv').read_text(errors='replace') if (case/'plugin.tsv').exists() else ''
    kv=dict(line.split('=',1) for line in result.splitlines() if '=' in line)
    source_file=kv.get('source_file','')
    paths=[pathlib.Path(kv[k]) for k in ('peak_write','peak_read') if kv.get(k)]+[cache]
    actual=next((p for p in paths if p.is_file()),cache)
    after=actual.read_bytes() if actual.exists() else None
    row={'name':name,'action':action,'plugin':plugin,'exit':rc,'wall_s':time.perf_counter()-started,'media':str(media),'source_file':source_file,'cache_path':str(actual),'result':result,'trace':trace,'errors':[]}
    def require(test,msg):
        if not test:row['errors'].append(msg)
    require(rc==0,'REAPER did not exit successfully');require('finished=true' in result,'test script did not finish');require('error=' not in result,'script error')
    require(int(kv.get('peak_count','0') or 0)>0,'REAPER could not read the resulting cache')
    target=pathlib.Path(source_file) if source_file else media
    if expect_new:require(bool(source_file) and norm(source_file)!=norm(media),'normal action did not produce a distinct media file')
    begins,generated,done=trace_jobs(trace);real_ids=set();target_ids=set()
    if plugin:
        require(kv.get('plugin')=='true','extension API missing');require('LOAD\t' in trace,'extension entrypoint not observed')
        final=kv.get('final_status')
        if allow_idle:require(final in ('0','2'),'created media was neither idle-wrapped nor ready-wrapped')
        else:require(final=='2','plugin job did not become ready')
        require(final!='-2','result source bypassed wrapper')
        target_ids={jid for jid,b in begins.items() if b.get('file') and norm(b['file'])==norm(target)}
        real_ids={jid for jid in target_ids if jid in generated and done.get(jid,{}).get('reuse')=='0'}
        if require_generation:
            require(bool(target_ids),'target media never entered plugin source/job path');require(bool(real_ids),'target media never completed a real plugin generation')
        if expect_stream is not None:
            require(bool(real_ids) and any((begins[j].get('stream')=='1')==expect_stream for j in real_ids),'target generation used unexpected streaming mode')
        row.update(target_job_count=len(target_ids),real_generation_count=len(real_ids))
    else:require(kv.get('plugin')=='false','native control unexpectedly loaded plugin')
    if after is None:require(False,'cache missing')
    else:
        try:
            end=standard_end(after);std=after[:end]
            layers=[struct.unpack_from('<i',after,18+i*8)[0] for i in range(after[5])]
            row.update(standard_bytes=end,standard_sha256=sha(std),file_bytes=len(after),layers=layers)
            for token in expect_tokens:require(token in layers,f'expected layer {token} missing')
            for token in forbid_tokens:require(token not in layers,f'forbidden layer {token} still present')
            if expected_standard is not None:require(std==expected_standard,'standard differs from same-platform native/seed control')
            if tail is not None:require(after[end:]==tail,'RPKX tail changed/lost');row['tail_sha256']=sha(after[end:])
            if expect_tail_move and plugin and real_ids:
                moved=[int(done[j].get('tail_moved','-1')) for j in real_ids if j in done]
                if expect_tail_move=='zero':require(bool(moved) and all(x==0 for x in moved),'same-size rebuild moved RPKX payload')
                elif expect_tail_move=='positive':require(any(x>0 for x in moved),'size-changing rebuild did not exercise tail relocation')
        except (ValueError,struct.error) as e:require(False,str(e))
    if seed is not None and action in ('manual','online') and row['errors'] and after is not None and before is not None:
        row['before_sha256']=sha(before);row['after_sha256']=sha(after)
    row['passed']=not row['errors'];(case/'summary.json').write_text(json.dumps(row,indent=2)+'\n')
    print('EXTENDED',json.dumps(row),flush=True)
    if row['errors']:
        for filename in ('console.txt','actions.txt','startup-windows.json','startup-macos.txt','host-process.json'):
            p=case/filename
            if p.exists():print('EXTENDED_DIAGNOSTIC',name,filename,p.read_text(errors='replace')[-16000:],flush=True)
        print('EXTENDED_CACHE_FILES',list(map(str,case.rglob('*.reapeaks'))),flush=True)
    if cleanup:cleanup_payload(case)
    return row,after,target

def read_base_standard(case_name:str)->bytes:
    summary=json.loads((OUT/case_name/'summary.json').read_text())
    data=pathlib.Path(summary['cache_path']).read_bytes()
    return data[:standard_end(data)]

def main():
    rows=[];native=read_base_standard('native-wave');spectrogram=read_base_standard('native-spectrogram')

    native_spectral_row,native_spectral_data,_=run_ext('spectral-native',plugin=False,action='spectral',expect_tokens=(-115,-114));rows.append(native_spectral_row)
    native_spectral=standalone(native_spectral_data)
    native_loudness_row,native_loudness_data,_=run_ext('loudness-native',plugin=False,action='loudness',expect_tokens=(-114,));rows.append(native_loudness_row)
    native_loudness=standalone(native_loudness_data)

    for name,kw in [
        ('spectral',dict(action='spectral',seed=native,tail_mib=1,expect_tokens=(-115,-114),expected_standard=native_spectral,expect_tail_move='positive')),
        ('loudness',dict(action='loudness',seed=native,tail_mib=1,expect_tokens=(-114,),expected_standard=native_loudness,expect_tail_move='positive')),
        ('normal-shrink',dict(action='normal',seed=spectrogram,tail_mib=1,show=1345,forbid_tokens=(-115,-103,-114),expected_standard=native,expect_tail_move='positive')),
        ('online-regenerate',dict(action='online',seed=native,tail_mib=1,expected_standard=native,expect_tail_move='zero')),
    ]:
        row,_,_=run_ext(name,**kw);rows.append(row)
    neg,_=base_run_case('extended-negative-reverse',seed=native,tail_mib=1,action='reverse',fail=True);rows.append(neg)

    long_source=OUT/'extended-long-source.wav';repeated_pcm16(long_source,1500)
    try:
        native_long_row,native_long_data,_=run_ext('long-native',plugin=False,action='manual',media_from=long_source,hardlink=True,cleanup=True);rows.append(native_long_row)
        native_long=standalone(native_long_data)
        if native_long is None:
            rows.append({'name':'long-plugin-cases','passed':False,'errors':['BLOCKED: long native control produced no valid cache']})
        else:
            row,_,_=run_ext('long-import',action='import',media_from=long_source,hardlink=True,expected_standard=native_long,expect_stream=True,cleanup=True);rows.append(row)
            row,_,_=run_ext('long-rebuild-rpkx',action='manual',media_from=long_source,hardlink=True,seed=native_long,tail_mib=1,expected_standard=native_long,expect_stream=True,expect_tail_move='zero',cleanup=True);rows.append(row)
    finally:
        try:long_source.unlink()
        except OSError:pass

    for opname in ('glue','render'):
        # A media-creation sink may legitimately create the first standard cache
        # before the new source is wrapped. There is no pre-existing RPKX to
        # preserve at creation time. Instead prove that the newly-created source
        # is wrapped/readable, then append RPKX and rebuild it through an ordinary
        # REAPER peak action. Record is intentionally not a hardware/driver CI
        # gate: once a recorded file exists, its peak-preservation semantics are
        # the same PCM16/float32 source path already covered by the base and long
        # rebuild cases.
        create_row,created_data,created_media=run_ext(opname+'-create',action=opname,expect_new=True,require_generation=False,allow_idle=True);rows.append(create_row)
        created_std=standalone(created_data)
        if create_row['passed'] and created_std is not None and created_media.is_file():
            rebuild_row,_,_=run_ext(opname+'-rpkx-rebuild',action='manual',media_from=created_media,seed=created_std,tail_mib=1,expected_standard=created_std,expect_tail_move='zero');rows.append(rebuild_row)
        else:rows.append({'name':opname+'-rpkx-rebuild','passed':False,'errors':['BLOCKED: creation operation did not yield wrapped readable file media']})

    passed=all(r.get('passed',False) for r in rows)
    report={'environment':INFO,'cases':rows,'passed':passed,'record_policy':'Recording creates new media/cache and therefore has no pre-existing RPKX to preserve. Recorded-file regeneration is covered by the same PCM16/float32 ordinary rebuild preservation path; live audio-device/transport availability is not a plugin-correctness gate.','scope':'Real REAPER 7.79 ordinary actions with same-platform exact native controls: spectral/loudness/normal profile grow+shrink, offline/online regeneration, reverse failure control, 25-minute PCM16 streaming import+RPKX rebuild, Glue and Render-items-to-new-take creation, and RPKX-bearing rebuilds of newly-created media.'}
    (OUT/'extended-report.json').write_text(json.dumps(report,indent=2)+'\n')
    lines=['# Extended REAPER host acceptance',f"Commit: {INFO['commit']}",'','| Case | Result | Error |','|---|---|---|']
    for r in rows:lines.append('| '+r['name']+' | '+('PASS' if r.get('passed') else 'FAIL')+' | '+'; '.join(r.get('errors',[]))+' |')
    (OUT/'EXTENDED_SUMMARY.md').write_text('\n'.join(lines)+'\n')
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:f.write('\n'.join(lines)+'\n')
    if not passed:sys.exit(1)
if __name__=='__main__':main()
