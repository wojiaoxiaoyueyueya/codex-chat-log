"""Only the fresh test-window process is targeted; no Codex process is queried."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
import win_process
exe = Path(__file__).with_name('GracefulWindow.exe')
results = {}
with tempfile.TemporaryDirectory(prefix='zhiru-close-probe-') as temp:
    for method in ('close_windows', 'graceful_shutdown'):
        marker = Path(temp) / method
        p = subprocess.Popen([str(exe), str(marker)], creationflags=0x08000000)
        deadline=time.monotonic()+10
        while not marker.with_name(marker.name+'.ready').exists():
            if p.poll() is not None or time.monotonic()>deadline: raise RuntimeError('Test window did not start')
            time.sleep(0.05)
        started=time.monotonic()
        getattr(win_process, method)([{'pid':p.pid,'kind':'desktop'}])
        p.wait(timeout=45)
        events=marker.with_name(marker.name+'.messages')
        normal_exit=marker.with_name(marker.name+'.normal-exit')
        elapsed=time.monotonic()-started
        if not normal_exit.exists() or elapsed>30: raise RuntimeError(json.dumps({'method':method,'exit':p.returncode,'seconds':round(elapsed,2),'files':[f.name for f in Path(temp).iterdir()]}))
        results[method]={'exit':p.returncode,'normal_exit_handler':normal_exit.read_text(),
                         'windows_messages':events.read_text().strip() if events.exists() else '', 'seconds':round(elapsed,2)}
print(json.dumps(results))
