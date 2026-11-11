#!/usr/bin/env python3
"""Start, stop and query the host bridge.

Installed as the command termux-proot-bridge: start|stop|status.
"""
import fcntl
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
RUN = BASE / 'run'

def running():
    with open(RUN / 'lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True

def main():
    os.umask(0o077)
    RUN.mkdir(mode=0o700, exist_ok=True)
    action = sys.argv[1] if len(sys.argv) == 2 else ''
    if action not in ('start', 'stop', 'status'):
        raise SystemExit(f'Usage: {Path(sys.argv[0]).name} start|stop|status')
    if action == 'status':
        print('running' if running() else 'stopped')
        return
    if action == 'stop':
        if running():
            pid = int((RUN / 'pid').read_text())
            cmd = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            if str(BASE / 'bridge.py').encode() not in cmd:
                raise SystemExit('Refusing to signal an unrelated process')
            os.kill(pid, signal.SIGTERM)
            for _ in range(150):
                if not running():
                    print('Bridge stopped')
                    return
                time.sleep(0.1)
            raise SystemExit('Bridge shutdown did not finish')
        print('Bridge already stopped')
        return
    started = not running()
    if started:
        # The bridge gets the Termux part of this environment and nothing
        # else: the rest may hold credentials it has no use for.
        keep = ('BOOTCLASSPATH', 'DEX2OATBOOTCLASSPATH', 'LD_PRELOAD', 'HOME', 'PREFIX')
        env = {k: v for k, v in os.environ.items() if k.startswith(('ANDROID_', 'TERMUX_')) or k in keep}
        prefix = env.setdefault('PREFIX', '/data/data/com.termux/files/usr')
        env.setdefault('HOME', '/data/data/com.termux/files/home')
        env['PATH'] = prefix + '/bin:/system/bin'
        with open(RUN / 'service.log', 'ab') as log:
            subprocess.Popen([sys.executable, str(BASE / 'bridge.py')], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(100):
        try:
            with socket.socket(socket.AF_UNIX) as conn:
                conn.settimeout(0.2)
                conn.connect(str(RUN / 'bridge.sock'))
            if running():
                print('Bridge started' if started else 'Bridge already running')
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise SystemExit(f"Bridge did not become ready; see {RUN / 'service.log'}")

if __name__ == '__main__':
    main()
