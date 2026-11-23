"""Track local conversion processes so shutdown can stop them promptly."""
import subprocess
import time
from threading import Lock

_lock = Lock()
_active = set()
_stopping = False


def _start(args, **kwargs):
    with _lock:
        if _stopping:
            raise RuntimeError('Media processing is shutting down')
        proc = subprocess.Popen(args, **kwargs)
        _active.add(proc)
    return proc


def run(args, **kwargs):
    timeout = kwargs.pop('timeout', None)
    capture = kwargs.pop('capture_output', False)
    check = kwargs.pop('check', False)
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc = _start(args, **kwargs)
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


def lines(args, *, timeout):
    """Yield stdout lines without holding a large probe report in memory."""
    proc = _start(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1 << 16)
    deadline = time.monotonic() + timeout
    try:
        for line in proc.stdout:
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(args, timeout)
            yield line
        proc.wait(timeout=max(1, deadline - time.monotonic()))
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, args)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
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
