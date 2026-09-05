#!/usr/bin/env python3
"""Small reproducible per-host benchmark; correctness is checked before timing
results are summarized. No cold-cache or device-level physical-I/O claim.
See host_actions.lua for independent normal-operation acceptance coverage.
"""
from __future__ import annotations
import json,math,os,pathlib,random,shutil,statistics,time
from host_process import launch
from host_acceptance import ROOT,OUT,INFO,FIXED_MTIME,fixture,rpkx_tail,standard_end,sha
SCRIPT=ROOT/'tools/reaper_plugin/benchmark.lua'
# This is deliberately a regression budget, not a parity claim. The plugin does
# extra durable journal/sync work that native REAPER does not. Median timing must
# nevertheless stay bounded, and a large untouched RPKX payload must not cause a
# size-proportional same-size rebuild penalty.
ABS_OVERHEAD_BUDGET_S=0.250
NATIVE_MULTIPLIER_BUDGET=12.0
RPKX_SIZE_OVERHEAD_BUDGET_S=0.250
RPKX_SIZE_MULTIPLIER_BUDGET=8.0

def one(name,profile,plugin,seed=None,mib=None):
    case=OUT/'benchmark'/name;case.mkdir(parents=True,exist_ok=False)
    media=case/'audio.wav';fixture(media)
    cache=pathlib.Path(str(media)+'.reapeaks')
    tail=b''
    if seed is not None:
        tail=rpkx_tail(seed,mib) if mib is not None else b''
        cache.write_bytes(seed+tail)
        os.utime(media,(FIXED_MTIME+120,FIXED_MTIME+120))
    cfg=case/'reaper.ini'
    cfg.write_text(
        f'[REAPER]\npeakcachegenmode=3\npeakcachegenrs=300\nshowpeaks={profile}\n'
        '[audioconfig]\nmode=5\ndummy_srate=48000\ndummy_blocksize=512\n',encoding='utf-8')
    if plugin:
        (case/'UserPlugins').mkdir();p=pathlib.Path(INFO['plugin']);shutil.copy2(p,case/'UserPlugins'/p.name)
    env=dict(os.environ,LRPK_CASE=str(case),LRPK_MEDIA=str(media),LIBREAPEAKS_PLUGIN_LOG=str(case/'plugin.tsv'))
    env.pop('LIBREAPEAKS_TEST_FAIL_AFTER_GENERATE',None)
    started=time.perf_counter()
    rc=launch([INFO['reaper'],'-newinst','-cfgfile',str(cfg),'-new','-nosplash',str(SCRIPT)],env,case,timeout=150)
    result=(case/'result.txt').read_text(errors='replace') if (case/'result.txt').exists() else ''
    kv=dict(line.split('=',1) for line in result.splitlines() if '=' in line)
    trace=(case/'plugin.tsv').read_text(errors='replace') if (case/'plugin.tsv').exists() else ''
    paths=[pathlib.Path(kv[k]) for k in ('peak_write','peak_read') if kv.get(k)]+[cache]
    actual=next((p for p in paths if p.is_file()),cache)
    data=actual.read_bytes() if actual.exists() else None
    errors=[]
    if rc!=0 or kv.get('finished')!='true' or 'error' in kv: errors.append('host/build driver failed')
    if kv.get('plugin')!=str(plugin).lower():errors.append('wrong plugin presence')
    if kv.get('begin')=='0':errors.append('cache was reused, not built')
    if plugin and (kv.get('status')!='2' or 'GENERATED\t' not in trace):errors.append('no successful real plugin generation')
    components={}
    for line in trace.splitlines():
        if line.startswith('DONE\t'):components=dict(x.split('=',1) for x in line.split('\t')[1:] if '=' in x)
    image=None
    if data is None: errors.append('cache missing')
    else:
        try:
            end=standard_end(data);image=data[:end]
            if plugin and data[end:]!=tail:errors.append('RPKX not preserved')
            if plugin and components.get('tail_moved')!='0':errors.append('same-size rebuild moved RPKX')
            if seed is not None and len(image)!=len(seed):errors.append('unexpected standard-size change')
        except ValueError as e:errors.append(str(e))
    build_s=float(kv.get('build_s','nan'))
    if not math.isfinite(build_s) or build_s<0:errors.append('invalid build timing')
    row={'name':name,'profile':profile,'plugin':plugin,'rpkx_mib':mib,'rc':rc,'build_s':build_s,'process_wall_s':time.perf_counter()-started,'standard_sha256':sha(image) if image else None,'tail_sha256':sha(tail),'components':components,'errors':errors}
    (case/'summary.json').write_text(json.dumps(row,indent=2)+'\n')
    print('BENCHMARK',json.dumps(row),flush=True)
    # Retain logs/checksums, not duplicate binaries or huge deterministic data
    # in the CI artifact. Cleanup is only inside this disposable case workspace.
    if not errors:
        if (case/'UserPlugins').exists():shutil.rmtree(case/'UserPlugins')
        media.unlink(missing_ok=True)
        actual.unlink(missing_ok=True)
    return row,image

def main():
    rows=[];summaries=[];performance_errors=[]
    for label,profile in [('waveform',1),('spectrogram',1345)]:
        base,seed=one(label+'-seed',profile,False);rows.append(base)
        if base['errors'] or seed is None:continue
        plan=[(False,None,i) for i in range(3)]+[(True,mib,i) for mib in (0,16,64) for i in range(3)]
        random.Random(779).shuffle(plan)
        collected=[]
        for plugin,mib,i in plan:
            name=f'{label}-'+(f'plugin-{mib}MiB' if plugin else 'native')+f'-{i}'
            row,image=one(name,profile,plugin,seed,mib);rows.append(row);collected.append(row)
        controls={r['standard_sha256'] for r in collected if not r['plugin'] and not r['errors']}
        for row in collected:
            if row['plugin'] and row['standard_sha256'] not in controls:row['errors'].append('standard differs from same-platform native control')
        native_times=[r['build_s'] for r in collected if not r['plugin'] and not r['errors']]
        native_median=statistics.median(native_times) if len(native_times)==3 else None
        profile_summaries=[]
        for plugin,mib in [(False,None),(True,0),(True,16),(True,64)]:
            selected=[r for r in collected if r['plugin']==plugin and r['rpkx_mib']==mib]
            valid=len(selected)==3 and all(not r['errors'] for r in selected)
            times=[r['build_s'] for r in selected]
            summary={'profile':label,'plugin':plugin,'rpkx_mib':mib,'valid':valid,'n':len(times),'median_s':statistics.median(times) if valid else None,'min_s':min(times) if valid else None,'max_s':max(times) if valid else None,'ratio_to_native':statistics.median(times)/native_median if valid and native_median else None}
            if plugin and valid and native_median:
                budget=max(native_median*NATIVE_MULTIPLIER_BUDGET,native_median+ABS_OVERHEAD_BUDGET_S)
                summary['native_regression_budget_s']=budget
                summary['within_native_budget']=summary['median_s']<=budget
                if not summary['within_native_budget']:
                    performance_errors.append(f"{label} {mib}MiB median {summary['median_s']:.6f}s exceeds native regression budget {budget:.6f}s")
            summaries.append(summary);profile_summaries.append(summary)
        p0=next((s for s in profile_summaries if s['plugin'] and s['rpkx_mib']==0 and s['valid']),None)
        p64=next((s for s in profile_summaries if s['plugin'] and s['rpkx_mib']==64 and s['valid']),None)
        if p0 and p64:
            size_budget=max(p0['median_s']*RPKX_SIZE_MULTIPLIER_BUDGET,p0['median_s']+RPKX_SIZE_OVERHEAD_BUDGET_S)
            p64['rpkx_size_regression_budget_s']=size_budget
            p64['within_rpkx_size_budget']=p64['median_s']<=size_budget
            if not p64['within_rpkx_size_budget']:
                performance_errors.append(f"{label} 64MiB median {p64['median_s']:.6f}s exceeds 0MiB size-regression budget {size_budget:.6f}s")
    correctness=bool(rows) and all(not r['errors'] for r in rows)
    report={'environment':INFO,'method':'Fresh REAPER process per case; 10 s 48 kHz stereo PCM16; timed direct build API, excluding startup. Shuffled order, 3 repeats; median gates. Warm/recently written files; no cache eviction. No claim of equal durability work or cold-device performance. Native baseline has no RPKX because native rebuild deletes it.','performance_policy':{'native_multiplier_budget':NATIVE_MULTIPLIER_BUDGET,'absolute_overhead_budget_s':ABS_OVERHEAD_BUDGET_S,'rpkx_size_multiplier_budget':RPKX_SIZE_MULTIPLIER_BUDGET,'rpkx_size_overhead_budget_s':RPKX_SIZE_OVERHEAD_BUDGET_S,'purpose':'Catch runaway regressions while allowing the plugin extra durable journal/sync work and hosted-runner timing noise.'},'rows':rows,'summaries':summaries,'performance_errors':performance_errors,'correctness_passed':correctness,'passed':correctness and not performance_errors}
    (OUT/'benchmark.json').write_text(json.dumps(report,indent=2)+'\n')
    lines=['# Host API benchmark',report['method'],'','| Profile | Writer | RPKX MiB | n | Median s | Min s | Max s | Ratio |','|---|---|---:|---:|---:|---:|---:|---:|']
    for s in summaries:
        values=[s['profile'],'plugin' if s['plugin'] else 'native',str(s['rpkx_mib']),str(s['n'])]
        values += [f'{s[k]:.6f}' if s[k] is not None else 'INVALID' for k in ('median_s','min_s','max_s','ratio_to_native')]
        lines.append('| '+' | '.join(values)+' |')
    if performance_errors:
        lines += ['','Performance gate failures:']+[f'- {e}' for e in performance_errors]
    else:
        lines += ['','Performance regression gates: PASS']
    (OUT/'BENCHMARK.md').write_text('\n'.join(lines)+'\n')
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:f.write('\n'.join(lines)+'\n')
    if not report['passed']:raise SystemExit(1)
if __name__=='__main__':main()
