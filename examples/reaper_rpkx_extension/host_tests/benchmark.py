#!/usr/bin/env python3
"""Real-host benchmark with separate peak-ready and durable-ready clocks."""
from __future__ import annotations
import json,math,os,pathlib,random,shutil,statistics,time
from host_process import launch
from host_acceptance import ROOT,OUT,INFO,FIXED_MTIME,fixture,rpkx_tail,standard_end,sha
SCRIPT=pathlib.Path(__file__).with_name('benchmark.lua')
DURABLE_ABS_BUDGET_S=1.000
RPKX_SIZE_OVERHEAD_BUDGET_S=0.500
RPKX_SIZE_MULTIPLIER_BUDGET=8.0

def one(name,profile,plugin,seed=None,mib=None):
    case=OUT/'benchmark'/name;case.mkdir(parents=True,exist_ok=False)
    media=case/'audio.wav';fixture(media);cache=pathlib.Path(str(media)+'.reapeaks');tail=b'';seed_sync_s=0.0
    if seed is not None:
        tail=rpkx_tail(seed,mib) if mib is not None else b''
        seed_started=time.perf_counter()
        # This cache models data that already existed before the REAPER action.
        # Make the setup durable before starting the timer so the plugin's
        # sync_all() is not charged for flushing 16/64 MiB of benchmark-created
        # dirty RPKX pages that it never modified.
        with cache.open('wb') as seeded:
            seeded.write(seed+tail);seeded.flush();os.fsync(seeded.fileno())
        seed_sync_s=time.perf_counter()-seed_started
        os.utime(media,(FIXED_MTIME+120,FIXED_MTIME+120))
    cfg=case/'reaper.ini';cfg.write_text(f'[REAPER]\npeakcachegenmode=3\npeakcachegenrs=300\nshowpeaks={profile}\n[audioconfig]\nmode=5\ndummy_srate=48000\ndummy_blocksize=512\n',encoding='utf-8')
    if plugin:
        (case/'UserPlugins').mkdir();p=pathlib.Path(INFO['plugin']);shutil.copy2(p,case/'UserPlugins'/p.name)
    env=dict(os.environ,LRPK_CASE=str(case),LRPK_MEDIA=str(media),LIBREAPEAKS_PLUGIN_LOG=str(case/'plugin.tsv'));env.pop('LIBREAPEAKS_TEST_FAIL_AFTER_GENERATE',None)
    started=time.perf_counter();rc=launch([INFO['reaper'],'-newinst','-cfgfile',str(cfg),'-new','-nosplash',str(SCRIPT)],env,case,timeout=150)
    result=(case/'result.txt').read_text(errors='replace') if (case/'result.txt').exists() else ''
    kv=dict(line.split('=',1) for line in result.splitlines() if '=' in line);trace=(case/'plugin.tsv').read_text(errors='replace') if (case/'plugin.tsv').exists() else ''
    paths=[pathlib.Path(kv[k]) for k in ('peak_write','peak_read') if kv.get(k)]+[cache];actual=next((p for p in paths if p.is_file()),cache);data=actual.read_bytes() if actual.exists() else None
    errors=[]
    if rc!=0 or kv.get('finished')!='true' or 'error' in kv:errors.append('host/build driver failed')
    if kv.get('plugin')!=str(plugin).lower():errors.append('wrong plugin presence')
    if kv.get('begin')=='0':errors.append('cache was reused, not built')
    if plugin and (kv.get('status')!='2' or 'GENERATED\t' not in trace or 'DONE\t' not in trace):errors.append('plugin did not reach durable successful generation')
    components={}
    for line in trace.splitlines():
        if line.startswith('DONE\t'):components=dict(x.split('=',1) for x in line.split('\t')[1:] if '=' in x)
    if plugin and components.get('syncs')!='3':errors.append('same-size rebuild did not use three-sync redo fast path')
    if plugin and profile in (1,1345):
        if components.get('raw_pcm16')!='1':errors.append('canonical PCM16 case did not use raw fast path')
        if profile==1 and components.get('async_commit')!='1':errors.append('canonical PCM16 waveform did not release peak-ready build before durable commit')
        if profile==1345 and components.get('async_commit')!='0':errors.append('spectrogram unexpectedly used waveform async handoff')
    image=None
    if data is None:errors.append('cache missing')
    else:
        try:
            end=standard_end(data);image=data[:end]
            if plugin and data[end:]!=tail:errors.append('RPKX not preserved')
            if plugin and components.get('tail_moved')!='0':errors.append('same-size rebuild moved RPKX')
            if seed is not None and len(image)!=len(seed):errors.append('unexpected standard-size change')
        except ValueError as e:errors.append(str(e))
    build_s=float(kv.get('build_s','nan'));settle_s=float(kv.get('settle_s','0' if not plugin else 'nan'))
    if not math.isfinite(build_s) or build_s<0:errors.append('invalid peak-ready build timing')
    if not math.isfinite(settle_s) or settle_s<0:errors.append('invalid durable-settle timing')
    durable_s=build_s+settle_s if math.isfinite(build_s) and math.isfinite(settle_s) else float('nan')
    row={'name':name,'profile':profile,'plugin':plugin,'rpkx_mib':mib,'rc':rc,'build_s':build_s,'settle_s':settle_s,'durable_s':durable_s,'seed_sync_s':seed_sync_s,'process_wall_s':time.perf_counter()-started,'standard_sha256':sha(image) if image else None,'tail_sha256':sha(tail),'components':components,'errors':errors}
    (case/'summary.json').write_text(json.dumps(row,indent=2)+'\n');print('BENCHMARK',json.dumps(row),flush=True)
    if not errors:
        if (case/'UserPlugins').exists():shutil.rmtree(case/'UserPlugins')
        media.unlink(missing_ok=True);actual.unlink(missing_ok=True)
    return row,image

def main():
    rows=[];summaries=[];performance_errors=[]
    for label,profile in [('waveform',1),('spectrogram',1345)]:
        base,seed=one(label+'-seed',profile,False);rows.append(base)
        if base['errors'] or seed is None:continue
        plan=[(False,None,i) for i in range(3)]+[(True,m,i) for m in (0,16,64) for i in range(3)];random.Random(779).shuffle(plan);collected=[]
        for plugin,mib,i in plan:
            row,_=one(f'{label}-'+(f'plugin-{mib}MiB' if plugin else 'native')+f'-{i}',profile,plugin,seed,mib);rows.append(row);collected.append(row)
        controls={r['standard_sha256'] for r in collected if not r['plugin'] and not r['errors']}
        for r in collected:
            if r['plugin'] and r['standard_sha256'] not in controls:r['errors'].append('standard differs from same-platform native control')
        native=[r['build_s'] for r in collected if not r['plugin'] and not r['errors']];native_median=statistics.median(native) if len(native)==3 else None;profile_summaries=[]
        for plugin,mib in [(False,None),(True,0),(True,16),(True,64)]:
            selected=[r for r in collected if r['plugin']==plugin and r['rpkx_mib']==mib];valid=len(selected)==3 and all(not r['errors'] for r in selected);times=[r['build_s'] for r in selected];durable=[r['durable_s'] for r in selected]
            s={'profile':label,'plugin':plugin,'rpkx_mib':mib,'valid':valid,'n':len(times),'median_s':statistics.median(times) if valid else None,'min_s':min(times) if valid else None,'max_s':max(times) if valid else None,'durable_median_s':statistics.median(durable) if valid else None,'ratio_to_native':statistics.median(times)/native_median if valid and native_median else None}
            if plugin and valid and native_median:
                s['native_regression_budget_s']=native_median;s['beats_native']=s['median_s']<native_median;s['within_native_budget']=s['beats_native']
                if not s['beats_native']:performance_errors.append(f"{label} {mib}MiB median {s['median_s']:.6f}s did not beat native {native_median:.6f}s")
                if label=='waveform':
                    s['within_durable_budget']=s['durable_median_s']<=DURABLE_ABS_BUDGET_S
                    if not s['within_durable_budget']:performance_errors.append(f"waveform {mib}MiB durable median {s['durable_median_s']:.6f}s exceeds {DURABLE_ABS_BUDGET_S:.3f}s background durability budget")
            summaries.append(s);profile_summaries.append(s)
        p0=next((s for s in profile_summaries if s['plugin'] and s['rpkx_mib']==0 and s['valid']),None);p64=next((s for s in profile_summaries if s['plugin'] and s['rpkx_mib']==64 and s['valid']),None)
        if p0 and p64:
            budget=max(p0['durable_median_s']*RPKX_SIZE_MULTIPLIER_BUDGET,p0['durable_median_s']+RPKX_SIZE_OVERHEAD_BUDGET_S);p64['rpkx_size_regression_budget_s']=budget;p64['within_rpkx_size_budget']=p64['durable_median_s']<=budget
            if not p64['within_rpkx_size_budget']:performance_errors.append(f"{label} 64MiB durable median {p64['durable_median_s']:.6f}s exceeds 0MiB size-regression budget {budget:.6f}s")
    correctness=bool(rows) and all(not r['errors'] for r in rows)
    report={'environment':INFO,'method':'Fresh REAPER process per case; 10 s 48 kHz stereo PCM16. Every pre-existing seeded cache, including its RPKX tail, is fsync-d before the timer so plugin durability is charged only for writes caused by the measured rebuild. build_s ends when PCM_Source_BuildPeaks completes; waveform settle_s independently waits for the stronger WAL/fsync durability status. Shuffled 3-run medians. Plugin cases prove raw PCM16, exact native standard bytes, three-sync WAL completion, and untouched RPKX.','performance_policy':{'waveform_peak_ready':'Every 0/16/64 MiB plugin median must be strictly faster than same-host native.','spectrogram_peak_ready':'Every 0/16/64 MiB plugin median must be strictly faster than same-host native.','durable_absolute_budget_s':DURABLE_ABS_BUDGET_S,'rpkx_size_multiplier_budget':RPKX_SIZE_MULTIPLIER_BUDGET,'rpkx_size_overhead_budget_s':RPKX_SIZE_OVERHEAD_BUDGET_S},'rows':rows,'summaries':summaries,'performance_errors':performance_errors,'correctness_passed':correctness,'passed':correctness and not performance_errors}
    (OUT/'benchmark.json').write_text(json.dumps(report,indent=2)+'\n')
    lines=['# Host API benchmark',report['method'],'','| Profile | Writer | RPKX MiB | n | Peak-ready median s | Durable median s | Ratio | Native win |','|---|---|---:|---:|---:|---:|---:|---:|']
    for s in summaries:
        vals=[s['profile'],'plugin' if s['plugin'] else 'native',str(s['rpkx_mib']),str(s['n'])]+[f"{s[k]:.6f}" if s[k] is not None else 'INVALID' for k in ('median_s','durable_median_s','ratio_to_native')]+[str(s.get('beats_native','-'))];lines.append('| '+' | '.join(vals)+' |')
    lines+=['']+((['Performance gate failures:']+[f'- {e}' for e in performance_errors]) if performance_errors else ['Performance gates: PASS'])
    (OUT/'BENCHMARK.md').write_text('\n'.join(lines)+'\n')
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:f.write('\n'.join(lines)+'\n')
    if not report['passed']:raise SystemExit(1)
if __name__=='__main__':main()
