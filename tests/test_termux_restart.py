"""Exercise launcher shutdown with an old supervisor that waits indefinitely."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROCESS_HELPER = Path(__file__).resolve().parents[1] / 'termux_process.sh'


class TermuxRestartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Some container executors mount a different PID namespace at /proc;
        # ps then describes a different process for a valid child PID.
        probe = subprocess.Popen(['sleep', '2'])
        try:
            observed = subprocess.run(['ps', '-p', str(probe.pid), '-o', 'args='],
                                      capture_output=True, text=True).stdout
            if 'sleep 2' not in observed:
                raise unittest.SkipTest('ps cannot inspect its own PID namespace here')
        finally:
            probe.terminate()
            probe.wait()

    def stop_old(self, pid):
        return subprocess.run(
            ['bash', '-c', '. "$1"; veltrix_stop_previous "$2"', 'bash', str(PROCESS_HELPER), str(pid)],
            env={**os.environ, 'VELTRIX_STOP_GRACE_SECONDS': '1',
                 'VELTRIX_STOP_CHILD_SECONDS': '1', 'VELTRIX_STOP_FINAL_SECONDS': '3'},
            capture_output=True, text=True, timeout=10,
        )

    def test_unresponsive_old_worker_is_stopped_before_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'bot.py').write_text('import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(1)\n')
            (root / 'termux_supervisor.sh').write_text(
                '#!/bin/bash\ncd "$(dirname "$0")"\n'
                f'"{sys.executable}" bot.py &\n'
                'child=$!\necho "$child" > child.pid\n'
                'trap \'wait "$child"\' TERM\nwait "$child"\n'
            )
            supervisor = subprocess.Popen(['bash', './termux_supervisor.sh'], cwd=root)
            child = None
            try:
                for _ in range(100):
                    if (root / 'child.pid').exists():
                        child = int((root / 'child.pid').read_text().strip())
                        break
                    time.sleep(.02)
                self.assertIsNotNone(child)
                result = self.stop_old(supervisor.pid)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIsNotNone(supervisor.wait(timeout=2))
                child_state = subprocess.run(['ps', '-p', str(child), '-o', 'stat='],
                                             capture_output=True, text=True).stdout.strip()
                self.assertTrue(not child_state or child_state.startswith(('Z', 'X')), child_state)
            finally:
                if supervisor.poll() is None:
                    supervisor.kill()
                    supervisor.wait()
                if child is not None:
                    try:
                        os.kill(child, 9)
                    except ProcessLookupError:
                        pass

    def test_other_pid_is_never_stopped(self):
        unrelated = subprocess.Popen(['sleep', '30'])
        try:
            result = self.stop_old(unrelated.pid)
            self.assertEqual(result.returncode, 1)
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=2)

    def test_exited_unreaped_supervisor_does_not_block_restart(self):
        zombie = subprocess.Popen(['bash', '-c', 'exit 0'])
        try:
            time.sleep(.05)
            self.assertEqual(self.stop_old(zombie.pid).returncode, 0)
        finally:
            zombie.wait(timeout=2)
