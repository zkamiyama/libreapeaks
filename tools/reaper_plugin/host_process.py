"""Disposable host-test process control, NOT included in the plugin.
Only the test process is inspected. Initial evaluation/audio dialogs are driven
as ordinary UI; source code, registration state and host binaries are untouched.
Unknown dialogs are logged and cause a timeout, never blindly dismissed.
"""
from __future__ import annotations
import json,os,pathlib,platform,subprocess,time

def windows_startup(pid: int, case: pathlib.Path, age: float) -> None:
    import ctypes
    from ctypes import wintypes as w
    u=ctypes.WinDLL('user32',use_last_error=True)
    cb=ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
    u.EnumWindows.argtypes=[cb,w.LPARAM]
    u.EnumChildWindows.argtypes=[w.HWND,cb,w.LPARAM]
    u.GetWindowThreadProcessId.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)]
    u.GetWindowTextLengthW.argtypes=[w.HWND];u.GetWindowTextLengthW.restype=ctypes.c_int
    u.GetWindowTextW.argtypes=[w.HWND,w.LPWSTR,ctypes.c_int]
    u.GetClassNameW.argtypes=[w.HWND,w.LPWSTR,ctypes.c_int]
    u.IsWindowVisible.argtypes=[w.HWND];u.IsWindowEnabled.argtypes=[w.HWND]
    u.GetDlgCtrlID.argtypes=[w.HWND];u.GetDlgCtrlID.restype=ctypes.c_int
    u.PostMessageW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM]
    def item(hwnd):
        b=ctypes.create_unicode_buffer(u.GetWindowTextLengthW(hwnd)+1);u.GetWindowTextW(hwnd,b,len(b))
        cl=ctypes.create_unicode_buffer(256);u.GetClassNameW(hwnd,cl,len(cl))
        return {'handle':int(hwnd),'text':b.value,'class':cl.value,'enabled':bool(u.IsWindowEnabled(hwnd)),'id':u.GetDlgCtrlID(hwnd)}
    tops=[]
    @cb
    def top(hwnd,_):
        owner=w.DWORD();u.GetWindowThreadProcessId(hwnd,ctypes.byref(owner))
        if owner.value==pid and u.IsWindowVisible(hwnd): tops.append(hwnd)
        return True
    u.EnumWindows(top,0)
    observed=[]
    for hwnd in tops:
        controls=[]
        @cb
        def child(ch,_):
            if u.IsWindowVisible(ch):controls.append(item(ch))
            return True
        u.EnumChildWindows(hwnd,child,0)
        parent=item(hwnd);parent['children']=controls;observed.append(parent)
        text=' '.join([parent['text']]+[x['text'] for x in controls]).lower()
        for c in controls:
            label=c['text'].replace('&','').strip().lower()
            choose=False
            if c['class'].lower()=='button' and c['enabled']:
                if 'still evaluating' in label: choose=True
                elif 'audio device' in text and label=='no': choose=True
            if choose:
                with (case/'startup-actions.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps({'age':age,'dialog':parent['text'],'clicked':c['text']})+'\n')
                u.PostMessageW(c['handle'],0x00f5,0,0)
    if observed:
        (case/'startup-windows.json').write_text(json.dumps(observed,ensure_ascii=False,indent=2),encoding='utf-8')
        if int(age)%10==0: print('STARTUP_WINDOWS',json.dumps(observed,ensure_ascii=True),flush=True)

def macos_startup(pid: int,case: pathlib.Path,age: float) -> None:
    script=pathlib.Path(__file__).with_name('macos_startup.applescript')
    try:
        result=subprocess.run(['osascript',str(script),str(pid)],capture_output=True,text=True,timeout=6)
        text=result.stdout+result.stderr
        (case/'startup-macos.txt').write_text(text,encoding='utf-8')
        print('STARTUP_MACOS',case.name,result.returncode,text,flush=True)
    except subprocess.TimeoutExpired:
        print('STARTUP_MACOS_TIMEOUT',case.name,flush=True)
    if age>=16 and not (case/'startup.png').exists():
        subprocess.run(['/usr/sbin/screencapture','-x',str(case/'startup.png')],timeout=5,check=False)

def launch(command: list[str], env: dict, case: pathlib.Path, timeout: float=65) -> int:
    with (case/'console.txt').open('wb') as out:
        p=subprocess.Popen(command,env=env,stdout=out,stderr=subprocess.STDOUT)
        begin=time.monotonic();last_poll=-1
        while p.poll() is None:
            age=time.monotonic()-begin
            if int(age)!=last_poll:
                last_poll=int(age)
                script_started=(case/'result.txt').exists()
                if not script_started and age>=1:
                    try:
                        if os.name=='nt':windows_startup(p.pid,case,age)
                        elif platform.system()=='Darwin' and int(age) in (8,16,32,48):macos_startup(p.pid,case,age)
                    except Exception as e:print('STARTUP_DIAGNOSTIC_ERROR',repr(e),flush=True)
                if age>=timeout:
                    p.kill();p.wait(timeout=10)
                    return -999
            time.sleep(0.05)
        return p.returncode
