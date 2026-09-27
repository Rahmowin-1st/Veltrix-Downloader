"""Track local conversion processes so shutdown can stop them promptly."""
import subprocess
from threading import Lock

_lock = Lock()
_active = set()
_stopping = False


def run(args, **kwargs):
    timeout = kwargs.pop('timeout', None)
    capture = kwargs.pop('capture_output', False)
    check = kwargs.pop('check', False)
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with _lock:
        if _stopping:
            raise RuntimeError('Media processing is shutting down')
        proc = subprocess.Popen(args, **kwargs)
        _active.add(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except BaseException:
            proc.kill()
            proc.communicate()
            raise
        result = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
    finally:
        with _lock:
            _active.discard(proc)


def stop_all():
    global _stopping
    with _lock:
        _stopping = True
        active = tuple(_active)
    for proc in active:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
