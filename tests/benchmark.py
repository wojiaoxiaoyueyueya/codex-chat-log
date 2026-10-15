"""Compare export implementations using an isolated, deterministic test profile."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

project = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(project / 'src'), str(project / 'lib')]
import offline as o
from test_chat_select import make_home, A, B, C, SEG
with tempfile.TemporaryDirectory(prefix='zhiru-benchmark-') as name:
    root = Path(name)
    text = ('This is a realistic local conversation about robotics and migration.\n' * 8000)
    home = make_home(root / 'profile', {sid: [text + str(n) for n in range(8)] for sid in (A, B, C, SEG)})
    with patch.object(o, 'CACHE', root / 'cache'), patch.object(o, 'owners', return_value=[]), \
         patch.object(o.m, 'running_codex', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
        start = time.perf_counter(); result = o.export_bundle(home, root / 'test.czr'); elapsed = time.perf_counter()-start
        o.verify_bundle(root / 'test.czr')
    print(json.dumps({'version':o.VERSION,'export_seconds':round(elapsed,3),'archive_bytes':result['size'],
                      'transcript_bytes':sum(p.stat().st_size for p in (home/'sessions').glob('*.jsonl'))}))
