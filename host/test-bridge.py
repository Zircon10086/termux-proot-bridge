#!/usr/bin/env python3
"""Protocol tests for the host bridge, run in Termux (outside the container).

Default: copies bridge.py into a throwaway directory, starts a private
instance there and tests it. --live tests a bridge that is already running
instead: the one in this directory if there is one, else the installed one,
or the one in DIR with --live=DIR. Run it from a directory that bridge
shares. --root adds the tests that need a working su.

The client side is covered by guest/test-client.sh, run inside the container.
"""
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
INSTALLED = Path.home() / '.local/lib/termux-proot-bridge'
SLOTS = 16
CWD = ''
RESULTS = []


class Bridge:
    def __init__(self, run):
        self.run = run
        self.token = (run / 'token').read_text()

    def open(self):
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(30)
        conn.connect(str(self.run / 'bridge.sock'))
        return conn

    def connect(self, argv, **kwargs):
        conn = self.open()
        request = dict(version=1, token=self.token, cwd=CWD, argv=argv, timeout=5)
        request.update(kwargs)
        conn.sendall((json.dumps(request) + '\n').encode())
        return conn

    def pid(self):
        return int((self.run / 'pid').read_text())


class Instance:
    """A private bridge, run from a copy of bridge.py in a throwaway directory."""

    def __init__(self, replace=None):
        # The name is short for the sake of the socket path (107 bytes).
        self.dir = Path(tempfile.mkdtemp(prefix='bt-'))
        source = (BASE / 'bridge.py').read_text()
        if replace:
            # A copy that differs in one place, to reach what this device
            # would not run by itself.
            if source.count(replace[0]) != 1:
                raise SystemExit(f'bridge.py no longer has exactly one {replace[0]!r}')
            source = source.replace(*replace)
        (self.dir / 'bridge.py').write_text(source)
        (self.dir / 'shared-dirs').write_text(f'{self.dir}\n')
        self.log = open(self.dir / 'service.log', 'ab')
        self.start()

    def start(self):
        # What the bridge is started with must not reach its commands.
        env = dict(os.environ, ANTHROPIC_API_KEY='planted', PLANTED_TOKEN='planted')
        self.proc = subprocess.Popen([sys.executable, str(self.dir / 'bridge.py')], env=env,
                                     stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                                     start_new_session=True)
        for _ in range(200):
            try:
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.settimeout(0.2)
                    probe.connect(str(self.dir / 'run' / 'bridge.sock'))
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise SystemExit('isolated bridge did not start; see ' + str(self.dir / 'service.log'))
        self.bridge = Bridge(self.dir / 'run')

    def close(self):
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        service = (self.dir / 'service.log').read_text()
        if service.strip():
            print('--- bridge log ---')
            print(service[-2000:])
        shutil.rmtree(self.dir, ignore_errors=True)


def check(name, condition, detail=''):
    RESULTS.append((name, bool(condition)))
    print(('PASS ' if condition else 'FAIL ') + name + ('' if condition else f' :: {detail}'))


def receive(conn):
    result = {'stdout': b'', 'stderr': b'', 'hello': None}
    with conn, conn.makefile('rb') as stream:
        for line in stream:
            message = json.loads(line)
            if 'hello' in message:
                result['hello'] = message['hello']
                continue
            for name in ('stdout', 'stderr'):
                if name in message:
                    result[name] += base64.b64decode(message[name])
            if 'exit' in message or 'error' in message:
                result.update({k: v for k, v in message.items() if k not in ('stdout', 'stderr')})
                return result
    result['error'] = 'connection closed without a result'
    return result


def attached(bridge, argv, fd, **kwargs):
    """Run argv while fd stands in as this process's fd 0."""
    saved = os.dup(0)
    os.dup2(fd, 0)
    try:
        return receive(bridge.connect(argv, attach_stdin=True, **kwargs))
    finally:
        os.dup2(saved, 0)
        os.close(saved)


def first_pid(stream):
    while True:
        line = stream.readline()
        if not line:
            return None
        message = json.loads(line)
        if 'error' in message:
            return None
        if 'stdout' in message:
            for text in base64.b64decode(message['stdout']).decode().split():
                if text.isdigit():
                    return int(text)


def wait_gone(pid, seconds=5):
    # /proc/<pid> of a root process exists but stat() raises EACCES for us;
    # a dead pid raises FileNotFoundError.
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            os.stat(f'/proc/{pid}')
        except FileNotFoundError:
            return True
        except PermissionError:
            pass
        time.sleep(0.05)
    return False


def process_state(pid):
    """State letter of a process from /proc ('Z' is a zombie); None once it is gone."""
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def resident(pid):
    """Resident memory of a process in MiB."""
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
        if line.startswith('VmRSS:'):
            return int(line.split()[1]) / 1024
    return 0


def test_basics(bridge):
    r = receive(bridge.connect([
        'bash', '-c', 'printf %s "$1"; printf err >&2; exit 7', '_',
        'space "quote" \'single\' \u4e2d\u6587']))
    check('hello + argv + Unicode + streams + exit code',
          r['hello'] == 4 and r['stdout'] == 'space "quote" \'single\' \u4e2d\u6587'.encode()
          and r['stderr'] == b'err' and r['exit'] == 7, r)

    r = receive(bridge.connect(['printf', '[%s]', '', 'x', '']))
    check('empty arguments survive', r.get('exit') == 0 and r['stdout'] == b'[][x][]', r)

    r = receive(bridge.connect(['printf', '%s', '\udcff\udcfeA caf\udce9']))
    check('argument bytes that are not UTF-8 survive',
          r.get('exit') == 0 and r['stdout'] == b'\xff\xfeA caf\xe9', r)

    for name, kwargs in (
            ('cwd outside the shared directories', {'cwd': '/'}),
            ('relative cwd', {'cwd': 'relative'}),
            ('missing cwd', {'cwd': CWD + '/does-not-exist'}),
            ('boolean timeout', {'timeout': True}),
            ('zero timeout', {'timeout': 0})):
        r = receive(bridge.connect(['true'], **kwargs))
        check(f'validation: {name}',
              bool(r.get('error')) and r['exit'] == 125 and r['hello'] == 4, r)

    r = receive(bridge.connect(['true'], token='incorrect'))
    check('wrong token -> authentication rejected',
          r.get('error') == 'Authentication rejected' and r['exit'] == 125, r)
    r = receive(bridge.connect(['true'], version=2))
    check('unknown request version is named as such',
          r.get('error') == 'Unsupported request version' and r['exit'] == 125, r)

    # Strings that are no byte strings must be refused by name, not fail
    # somewhere inside and land in the log.
    r = receive(bridge.connect(['printf', '%s', 'a\ud800b']))
    check('an argument that cannot be encoded is refused',
          r.get('error') == 'Invalid argv' and r['exit'] == 125, r)
    refused = [receive(bridge.connect(['true'], cwd=CWD + bad)).get('error') for bad in ('/\ud800', '/a\0b')]
    check('a working directory that cannot be encoded is refused',
          refused == ['Invalid working directory'] * 2, refused)

    r = receive(bridge.connect(['bridge-test-no-such-command']))
    check('command not found -> 127',
          r['exit'] == 127 and 'Command not found' in r.get('error', ''), r)

    target = bridge.run / 'tmp' / 'not-executable'
    target.write_text('#!/bin/sh\ntrue\n')
    target.chmod(0o600)
    r = receive(bridge.connect([str(target)]))
    check('not executable -> 126',
          r['exit'] == 126 and 'Permission denied' in r.get('error', ''), r)
    target.unlink()


def test_timeout(bridge):
    start = time.monotonic()
    r = receive(bridge.connect(['sleep', '10'], timeout=1))
    elapsed = time.monotonic() - start
    check('timeout -> 124',
          r['exit'] == 124 and r.get('error') == 'Command timed out' and elapsed < 6,
          (r, elapsed))

    start = time.monotonic()
    r = receive(bridge.connect(['bash', '-c', 'trap "" TERM; echo partial; sleep 30'], timeout=1))
    elapsed = time.monotonic() - start
    check('timeout escalates TERM -> KILL and keeps partial output',
          r['exit'] == 124 and 2.0 <= elapsed < 6 and r['stdout'] == b'partial\n', (r, elapsed))


def test_signals(bridge):
    conn = bridge.connect(['bash', '-c', 'echo $$; exec sleep 100'])
    stream = conn.makefile('rb')
    first_pid(stream)
    conn.sendall(b'{"signal":"SIGINT"}\n')
    terminal = None
    while terminal is None:
        message = json.loads(stream.readline())
        if 'exit' in message or 'error' in message:
            terminal = message
    check('SIGINT forwarded', terminal.get('exit') == -signal.SIGINT, terminal)
    stream.close()
    conn.close()

    conn = bridge.connect(['bash', '-c', 'trap "" INT; echo $$; sleep 30'], timeout=20)
    stream = conn.makefile('rb')
    first_pid(stream)
    start = time.monotonic()
    conn.sendall(b'{"signal":"SIGINT"}\n')
    terminal = None
    while terminal is None:
        message = json.loads(stream.readline())
        if 'exit' in message or 'error' in message:
            terminal = message
    elapsed = time.monotonic() - start
    check('ignored SIGINT -> killed, 130',
          terminal.get('exit') == 128 + signal.SIGINT and 'SIGINT' in terminal.get('error', '')
          and elapsed < 8, (terminal, elapsed))
    stream.close()
    conn.close()


def test_disconnect(bridge):
    conn = bridge.connect(['bash', '-c', 'echo $$; exec sleep 100'])
    stream = conn.makefile('rb')
    pid = first_pid(stream)
    stream.close()
    conn.close()
    check('disconnect reaps the process group', wait_gone(pid), f'pid {pid} survived')


def test_background(bridge):
    start = time.monotonic()
    r = receive(bridge.connect(['bash', '-c', 'sleep 100 & echo $!']))
    elapsed = time.monotonic() - start
    pid = int(r['stdout'])
    check('exited command reports its status and the background cleanup',
          r['exit'] == 0 and r.get('background') is True and wait_gone(pid) and elapsed < 4,
          (r, pid, elapsed))


def test_unreaped(bridge):
    # The command's pid is the id of its process group. Reaped, it can be
    # handed to an unrelated process, which the group kill of the cleanup
    # would then hit, through su if need be. So an exited command has to stay
    # a zombie until the cleanup.
    conn = bridge.connect(['bash', '-c', 'sleep 100 & echo $$'])
    stream = conn.makefile('rb')
    pid = first_pid(stream)
    time.sleep(0.7)
    state = process_state(pid)
    terminal = None
    while terminal is None:
        message = json.loads(stream.readline() or b'{"error": "closed"}')
        if 'exit' in message or 'error' in message:
            terminal = message
    stream.close()
    conn.close()
    check('an exited command stays unreaped until its group is cleaned up',
          state == 'Z' and terminal.get('exit') == 0 and wait_gone(pid), (state, terminal))


def test_stdin(bridge):
    r, w = os.pipe()
    os.write(w, '\u7b2c\u4e00\u884c\nsecond line\n'.encode())
    os.close(w)
    result = attached(bridge, ['cat'], r)
    os.close(r)
    check('attached pipe reaches the command',
          result['exit'] == 0 and result['stdout'] == '\u7b2c\u4e00\u884c\nsecond line\n'.encode(), result)

    r, w = os.pipe()
    os.write(w, b'keep\n')
    result = attached(bridge, ['true'], r)
    os.close(w)
    left = os.read(r, 100)
    os.close(r)
    check('unread pipe input stays with the caller',
          result['exit'] == 0 and left == b'keep\n', (result, left))

    with tempfile.NamedTemporaryFile() as handle:
        handle.write(b'skip\nrest\n')
        handle.flush()
        fd = os.open(handle.name, os.O_RDONLY)
        os.lseek(fd, 5, os.SEEK_SET)
        result = attached(bridge, ['cat'], fd)
        os.close(fd)
    check('attached file resumes at the caller offset',
          result['exit'] == 0 and result['stdout'] == b'rest\n', result)

    with tempfile.NamedTemporaryFile() as handle:
        handle.write(b'one\ntwo\nthree\n')
        handle.flush()
        fd = os.open(handle.name, os.O_RDONLY)
        result = attached(bridge, ['head', '-c', '4'], fd)
        left = os.read(fd, 100)
        os.close(fd)
    check('attached file shares its offset with the caller',
          result['exit'] == 0 and result['stdout'] == b'one\n' and left == b'two\nthree\n',
          (result, left))

    r, w = os.pipe()
    os.set_blocking(r, False)
    late = threading.Timer(0.5, lambda: (os.write(w, b'late\n'), os.close(w)))
    late.start()
    result = attached(bridge, ['cat'], r)
    late.join()
    untouched = not os.get_blocking(r)
    os.close(r)
    check('a non-blocking caller pipe blocks for the command and stays non-blocking',
          result['exit'] == 0 and result['stdout'] == b'late\n' and untouched, (result, untouched))

    fd = os.open('/dev/null', os.O_RDONLY)
    result = attached(bridge, ['cat'], fd)
    os.close(fd)
    check('attach refuses what is neither pipe nor file',
          result.get('exit') == 125 and 'error' in result, result)

    a, b = socket.socketpair()
    result = attached(bridge, ['cat'], a.fileno())
    a.close()
    b.close()
    check('attach refuses a socket', result.get('exit') == 125 and 'error' in result, result)

    result = receive(bridge.connect(['cat']))
    check('stdin is /dev/null unless attached',
          result['exit'] == 0 and result['stdout'] == b'', result)


def test_stdin_without_pidfd():
    """Standard input on a kernel without pidfd_getfd (before Linux 5.6)."""
    global CWD
    outer = CWD
    instance = Instance(replace=('PIDFD_GETFD = kernel() >= (5, 6)', 'PIDFD_GETFD = False'))
    CWD = str(instance.dir)
    bridge = instance.bridge
    try:
        r, w = os.pipe()
        os.write(w, b'one\ntwo\n')
        result = attached(bridge, ['head', '-c', '4'], r)
        os.close(w)
        left = os.read(r, 100)
        os.close(r)
        check('old kernel: a pipe is shared with the caller all the same',
              result.get('exit') == 0 and result['stdout'] == b'one\n' and left == b'two\n', (result, left))

        r, w = os.pipe()
        os.set_blocking(r, False)
        late = threading.Timer(0.5, lambda: (os.write(w, b'late\n'), os.close(w)))
        late.start()
        result = attached(bridge, ['cat'], r)
        late.join()
        os.close(r)
        check('old kernel: a non-blocking caller pipe blocks for the command',
              result.get('exit') == 0 and result['stdout'] == b'late\n', result)

        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b'skip\nrest\n')
            handle.flush()
            fd = os.open(handle.name, os.O_RDONLY)
            os.lseek(fd, 5, os.SEEK_SET)
            result = attached(bridge, ['cat'], fd)
            os.close(fd)
        check('old kernel: a file is read from the caller offset',
              result.get('exit') == 0 and result['stdout'] == b'rest\n', result)

        fd = os.open('/dev/null', os.O_RDONLY)
        result = attached(bridge, ['cat'], fd)
        os.close(fd)
        a, b = socket.socketpair()
        other = attached(bridge, ['cat'], a.fileno())
        a.close()
        b.close()
        check('old kernel: attach refuses what is neither pipe nor file',
              result.get('exit') == 125 and other.get('exit') == 125, (result, other))
    finally:
        CWD = outer
        instance.close()


def test_merge(bridge):
    r = receive(bridge.connect(
        ['bash', '-c', 'echo o1; echo e1 >&2; echo o2'], merge=True))
    check('merged output preserves order',
          r['stdout'] == b'o1\ne1\no2\n' and r['stderr'] == b'', r)


def test_binary(bridge):
    code = 'import sys; sys.stdout.buffer.write(bytes((i * 31) & 255 for i in range(1000000)))'
    r = receive(bridge.connect([sys.executable, '-c', code]))
    expected = bytes((i * 31) & 255 for i in range(1_000_000))
    check('1 MB binary round trip', r['stdout'] == expected, len(r['stdout']))


def test_slow_client(bridge):
    # Longer than any send timeout: the bridge has to hold the command back,
    # neither dropping the client nor piling the output up.
    size = 30_000_000
    before = resident(bridge.pid())
    conn = bridge.connect(['head', '-c', str(size), '/dev/zero'], timeout=30)
    time.sleep(6.5)
    grown = resident(bridge.pid()) - before
    r = receive(conn)
    check('a client that stalls gets everything once it reads again',
          r.get('exit') == 0 and len(r['stdout']) == size,
          (r.get('exit'), r.get('error'), len(r['stdout'])))
    check('output for a stalled client is not piled up in the bridge', grown < 8, f'{grown:.1f} MiB')

    start = time.monotonic()
    conn = bridge.connect(['head', '-c', str(size), '/dev/zero'], timeout=2)
    time.sleep(3)
    r = receive(conn)
    elapsed = time.monotonic() - start
    check('output nobody takes still ends at the timeout',
          r.get('exit') in (124, 0) and elapsed < 12 and len(r['stdout']) <= size,
          (r.get('exit'), r.get('error'), len(r['stdout']), elapsed))

    conn = bridge.connect(['bash', '-c', 'echo $$; exec head -c 30000000 /dev/zero'], timeout=30)
    stream = conn.makefile('rb')
    pid = first_pid(stream)
    time.sleep(0.5)
    stream.close()
    conn.close()
    check('a stalled client that leaves takes its command with it',
          pid is not None and wait_gone(pid), f'pid {pid} survived')


def test_concurrency(bridge):
    a = bridge.connect(['bash', '-c', 'sleep 0.15; echo a'])
    b = bridge.connect(['echo', 'b'])
    ra, rb = receive(a), receive(b)
    check('concurrent commands', ra['stdout'] == b'a\n' and rb['stdout'] == b'b\n', (ra, rb))


def test_limit(bridge):
    idle = []
    try:
        for _ in range(SLOTS):
            idle.append(bridge.open())
        for conn in idle:
            conn.recv(100)
        extra = bridge.open()
        with extra, extra.makefile('rb') as stream:
            first = json.loads(stream.readline() or b'{}')
        check('past the connection limit the first line says so',
              first.get('error') == 'Too many concurrent commands' and first.get('exit') == 125, first)
    finally:
        for conn in idle:
            conn.close()
    r = None
    for _ in range(50):
        try:
            r = receive(bridge.connect(['true']))
        except OSError as error:
            r = {'error': repr(error)}
        if r.get('exit') == 0:
            break
        time.sleep(0.1)
    check('the slots return when the connections close', r.get('exit') == 0, r)


def test_environment(bridge):
    r = receive(bridge.connect(['bash', '-c', 'env | grep -cE "^[^=]*(ANTHROPIC|API_KEY|TOKEN|PLANTED)"']))
    check('no inherited API credentials', r['stdout'] == b'0\n', r)

    r = receive(bridge.connect(['bash', '-c', 'echo "$HOME|$PREFIX|$PATH"']))
    home, prefix, path = (r['stdout'].decode().strip().split('|') + ['', ''])[:3]
    check('commands get the Termux environment',
          home == os.environ.get('HOME') and prefix == os.environ.get('PREFIX')
          and path.startswith(prefix + '/bin:'), r)


def test_shebang(bridge):
    # Android has no /usr/bin/env; termux-exec, loaded through LD_PRELOAD,
    # is what makes such a script start.
    script = bridge.run / 'tmp' / 'env-shebang'
    script.write_text('#!/usr/bin/env bash\necho ran\n')
    script.chmod(0o700)
    r = receive(bridge.connect([str(script)]))
    check('#!/usr/bin/env scripts run on the host',
          r.get('exit') == 0 and r['stdout'] == b'ran\n', r)
    script.unlink()


def test_permissions(bridge):
    modes = {}
    for name in ('token', 'bridge.sock', 'pid'):
        modes[name] = (bridge.run / name).stat().st_mode & 0o777
    check('private runtime permissions',
          all(mode == 0o600 for mode in modes.values()), modes)
    check('runtime directory is private',
          (bridge.run.stat().st_mode & 0o777) == 0o700,
          oct(bridge.run.stat().st_mode & 0o777))


def bridge_descriptors(bridge):
    """Descriptor count of the bridge once it stopped changing.

    A worker still closes its command's pipes for a moment after the client
    has the result, so a count taken right behind a request can be too high.
    """
    path = f'/proc/{bridge.pid()}/fd'
    count = len(os.listdir(path))
    for _ in range(40):
        time.sleep(0.05)
        now = len(os.listdir(path))
        if now == count:
            break
        count = now
    return count


def test_root_reclaim(bridge):
    before = bridge_descriptors(bridge)
    conn = bridge.connect(['su', '-c', 'echo $$; exec sleep 100'], timeout=10)
    stream = conn.makefile('rb')
    pid = first_pid(stream)
    stream.close()
    conn.close()
    if pid is None:
        check('root leftovers reclaimed after disconnect', False, 'su did not start')
        return
    check('root leftovers reclaimed after disconnect', wait_gone(pid, 8),
          f'root pid {pid} survived; kill manually with su')

    # The leak first seen in practice: our own shell leads the group and only
    # a descendant is root.
    conn = bridge.connect(['bash', '-c', 'su -c "echo \\$\\$; exec sleep 100"; true'], timeout=10)
    stream = conn.makefile('rb')
    pid = first_pid(stream)
    stream.close()
    conn.close()
    check('root descendant of an unprivileged leader reclaimed',
          pid is not None and wait_gone(pid, 8), f'root pid {pid} survived; kill manually with su')

    start = time.monotonic()
    conn = bridge.connect(['su', '-c', 'echo $$; exec sleep 100'], timeout=1)
    stream = conn.makefile('rb')
    pid = first_pid(stream)
    terminal = None
    while terminal is None:
        message = json.loads(stream.readline())
        if 'exit' in message or 'error' in message:
            terminal = message
    elapsed = time.monotonic() - start
    stream.close()
    conn.close()
    check('root command timeout -> 124 without waiting on an unsignalable group',
          terminal.get('exit') == 124 and elapsed < 3 and wait_gone(pid, 8),
          (terminal, elapsed, pid))

    time.sleep(0.5)
    after = bridge_descriptors(bridge)
    check('root cleanup leaks no descriptors', after == before, (before, after))


def test_crash(with_root):
    """A bridge that is killed outright. Always on an instance of its own."""
    global CWD
    outer = CWD
    instance = Instance()
    CWD = str(instance.dir)
    try:
        commands = [['bash', '-c', 'echo $$; exec sleep 100']]
        if with_root:
            commands.append(['su', '-c', 'echo $$; exec sleep 100'])
        held, pids = [], []
        for argv in commands:
            conn = instance.bridge.connect(argv, timeout=60)
            stream = conn.makefile('rb')
            pids.append(first_pid(stream))
            held += [stream, conn]
        instance.proc.kill()
        instance.proc.wait()
        gone = [pid is not None and wait_gone(pid, 8) for pid in pids]
        check('the commands of a killed bridge are taken down' + (', root ones too' if with_root else ''),
              all(gone), f'pids {pids}, gone {gone}; kill what is left by hand')
        for item in held:
            item.close()
        instance.start()
        r = receive(instance.bridge.connect(['true']))
        check('a killed bridge starts again in place', r.get('exit') == 0, r)
    finally:
        CWD = outer
        instance.close()


def main():
    global CWD
    options = sys.argv[1:]
    live = next((o for o in options if o == '--live' or o.startswith('--live=')), None)
    with_root = '--root' in options
    if any(o not in ('--root', live) for o in options):
        raise SystemExit('Usage: test-bridge.py [--live[=DIR]] [--root]')
    instance = None
    if live:
        directory = Path(live[7:]) if '=' in live else BASE if (BASE / 'run' / 'token').exists() else INSTALLED
        try:
            bridge = Bridge(directory / 'run')
        except OSError:
            raise SystemExit(f'no running bridge in {directory}')
        CWD = os.getcwd()
        r = receive(bridge.connect(['true']))
        if r.get('exit') != 0:
            raise SystemExit(f"cannot use the running bridge from here: {r.get('error')}")
    else:
        instance = Instance()
        bridge = instance.bridge
        CWD = str(instance.dir)

    try:
        test_basics(bridge)
        test_timeout(bridge)
        test_signals(bridge)
        test_disconnect(bridge)
        test_background(bridge)
        test_unreaped(bridge)
        test_stdin(bridge)
        test_merge(bridge)
        test_binary(bridge)
        test_slow_client(bridge)
        test_concurrency(bridge)
        test_environment(bridge)
        test_shebang(bridge)
        test_permissions(bridge)
        if not live:
            # Would refuse the running bridge's real callers for a moment.
            test_limit(bridge)
        if with_root:
            test_root_reclaim(bridge)
        test_crash(with_root)
        test_stdin_without_pidfd()
    finally:
        if instance is not None:
            instance.close()

    failed = [name for name, ok in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed')
    if failed:
        print('FAILED: ' + ', '.join(failed))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
