#!/usr/bin/env python3
"""Host agent of termux-proot-bridge.

Runs in Termux, outside the PRoot container, and executes the commands that
termux-run sends from inside it.

Newline-delimited JSON over a Unix socket, one request per connection.

  hello   : {"hello":4}                                  sent first, once
  request : {"version":1,"token":str,"cwd":str,"argv":[str],"timeout":number,
             "attach_stdin":bool,"merge":bool}
  control : {"signal":"SIGINT"|"SIGTERM"}
  output  : {"stdout":"<base64>"}  {"stderr":"<base64>"}
  result  : {"exit":int,"background":bool} | {"error":str,"exit":int}

hello is the revision of the bridge, by which a client tells what it can rely
on: 3 attaches standard input itself, 4 also waits for a client that is slow
to take output instead of dropping it. version is the format of the request.
A client reads the first line before it sends its request: at the connection
limit that line is an error and the bridge hangs up.

Arguments are strings. Bytes that are not UTF-8 travel as lone surrogates
U+DC80..U+DCFF (Python's surrogateescape) and reach the command unchanged.
exit is negative when a signal ended the command.

attach_stdin hands the client's own fd 0 to the command: the bridge duplicates
it out of the client process. Nothing is copied, input the command does not
read stays with the caller, and input it does read is gone for the caller too
(a regular file keeps one offset between them). Only pipes and regular files
qualify.

Errors carry shell-conventional exit codes: 124 timeout, 125 bridge/tool
error, 126 not executable, 127 not found, 128+N did not stop on signal N.
"""
import base64
import ctypes
import errno
import fcntl
import hmac
import json
import os
import secrets
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

PREFIX = os.environ.get('PREFIX') or '/data/data/com.termux/files/usr'
HOME = Path(os.environ.get('HOME') or '/data/data/com.termux/files/home')
BASE = Path(__file__).resolve().parent
RUN = BASE / 'run'
ROOTS = []
ACTIVE = set()
GUARD = threading.Lock()
STOP = threading.Event()
SLOTS = threading.BoundedSemaphore(16)
PROTOCOL = 4
MAX_REQUEST = 1 << 20
CHUNK = 65536
DRAIN = 1.5
SIGNALS = {'SIGINT': signal.SIGINT, 'SIGTERM': signal.SIGTERM}
# Commands get a Termux environment built here, not the one the bridge was
# started in, which may hold credentials.
ENV = {k: v for k, v in os.environ.items()
       if k.startswith(('ANDROID_', 'TERMUX_'))
       or k in ('BOOTCLASSPATH', 'DEX2OATBOOTCLASSPATH', 'LD_PRELOAD')}
ENV.update(HOME=str(HOME), PREFIX=PREFIX, PATH=PREFIX + '/bin:/system/bin',
           SHELL=PREFIX + '/bin/bash',
           TMPDIR=str(RUN / 'tmp'), XDG_CACHE_HOME=str(RUN / 'cache'),
           TERM='xterm-256color', LANG='C.UTF-8')
# termux-exec is what makes #!/usr/bin/env and the like work on Android.
for library in ('libtermux-exec-ld-preload.so', 'libtermux-exec.so'):
    if 'LD_PRELOAD' not in ENV and os.path.exists(f'{PREFIX}/lib/{library}'):
        ENV['LD_PRELOAD'] = f'{PREFIX}/lib/{library}'
TOKEN = ''
REAPER = None
# Termux builds Python without os.pidfd_open and nothing wraps pidfd_getfd.
# Both carry the same number on every architecture.
LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.syscall.restype = ctypes.c_long
SYS_PIDFD_OPEN = 434
SYS_PIDFD_GETFD = 438


def kernel():
    try:
        return tuple(int(part) for part in os.uname().release.split('.')[:2])
    except ValueError:
        return (0, 0)


# pidfd_getfd came with Linux 5.6. Older kernels are not asked: Android may
# kill a process for a system call it does not know instead of refusing it.
PIDFD_GETFD = kernel() >= (5, 6)


class ClientGone(Exception):
    pass


class RequestError(Exception):
    def __init__(self, message, code=125):
        super().__init__(message)
        self.code = code


def log(*parts):
    try:
        print(time.strftime('%F %T'), *parts, file=sys.stderr, flush=True)
    except Exception:
        pass


def encode(message):
    return (json.dumps(message) + '\n').encode()


def send(conn, message):
    conn.sendall(encode(message))


def shared_dirs():
    """Directories a command may start in.

    A working directory means the same thing to both sides only where the
    container sees the host's directory under the host's path. The file
    `shared-dirs` next to this one lists those, one per line; without it they
    are the Termux home and the shared storage, which a proot-distro login
    binds that way unless told otherwise.
    """
    try:
        lines = (BASE / 'shared-dirs').read_text().splitlines()
    except FileNotFoundError:
        lines = [str(HOME), '/storage/emulated/0']
    roots = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        path = Path(line).expanduser()
        if path.is_absolute():
            roots.append(path.resolve())
        else:
            log(f'shared-dirs: ignoring {line!r}, not an absolute path')
    return roots


def signal_group(pgid, sig):
    # killpg serves every member we may signal and raises EPERM only when
    # none is left that we own, i.e. the survivors are root.
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def group_state(pgid):
    """'gone', 'ours' (a member still takes our signals) or 'blocked'."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return 'gone'
    except PermissionError:
        return 'blocked'
    return 'ours'


def reclaim(pgid, reap=lambda: None):
    """SIGKILL a command's process group, root members included.

    Members that became root through su refuse our signals. Killing those is
    the single exception to "the bridge never runs su on its own": it only
    ever targets the group of a command this bridge started, and only after
    an ordinary kill was refused with EPERM. Returns whether it came to that.
    """
    end = time.monotonic() + 2
    while True:
        signal_group(pgid, signal.SIGKILL)
        # Reap before probing: our own zombie leader keeps answering for the
        # group and would hide the members we are not allowed to signal.
        reap()
        state = group_state(pgid)
        if state != 'ours' or time.monotonic() >= end:
            break
        time.sleep(0.02)
    if state != 'blocked':
        return False
    log(f'group {pgid} has members we cannot signal; killing it through su')
    try:
        subprocess.run(['su', '-c', f'kill -9 -- -{pgid}'], env=ENV,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, start_new_session=True)
    except Exception as error:
        log(f'su kill failed for group {pgid}: {error!r}')
    return True


def exit_status(child):
    """Exit status of a command that has ended, None while it runs.

    Looks without reaping. The command's pid is also the id of its process
    group; reaped, and with the group empty, the number is free for an
    unrelated process, and the group kill of the cleanup would hit that one,
    through su if it is root. So the command stays a zombie until kill_group
    has signalled the group.
    """
    if child.returncode is not None:
        return child.returncode
    try:
        info = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return child.poll()
    if info is None:
        return None
    return info.si_status if info.si_code == os.CLD_EXITED else -info.si_status


def kill_group(child):
    """Reclaim the process group of one of our commands and reap the command."""
    pgid = child.pid
    blocked = reclaim(pgid, child.poll)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log(f'child {pgid} did not exit after cleanup')
    if blocked:
        end = time.monotonic() + 1
        while group_state(pgid) == 'blocked' and time.monotonic() < end:
            time.sleep(0.05)
        if group_state(pgid) == 'blocked':
            log(f'group {pgid} survived the su kill')


def announce(pgid, running):
    """Tell the reaper that a command started or has been cleaned up."""
    if REAPER is not None:
        try:
            os.write(REAPER, f'{"+" if running else "-"}{pgid}\n'.encode())
        except OSError:
            pass


def reaper(source):
    """Take down the commands of a bridge that died without cleaning up.

    Runs as a process of its own. The bridge announces on a pipe each command
    it starts and each it has cleaned up; the pipe ends when the bridge is
    gone, however it went, and what is still announced then gets killed.

    Nothing holds the group ids from then on: a command that had exited is
    reaped by init, and its number is free once its group is empty. The kill
    follows the end of the pipe at once, which is all that keeps it from
    meeting another group under that number.
    """
    groups = set()
    with os.fdopen(source) as lines:
        for line in lines:
            (groups.add if line[0] == '+' else groups.discard)(int(line[1:]))
    for pgid in groups:
        log(f'the bridge is gone; killing group {pgid}, which it left behind')
        reclaim(pgid)


def start_reaper(lock):
    """Fork the reaper and keep the writing end of its pipe."""
    global REAPER
    source, REAPER = os.pipe()
    if os.fork():
        os.close(source)
        return
    status = 1
    try:
        os.close(REAPER)
        # Ours is a copy: the bridge keeps the lock, and a reaper that is
        # still at work must not make a dead bridge look alive.
        lock.close()
        os.setsid()
        reaper(source)
        status = 0
    except BaseException as error:
        log('reaper failed:', repr(error))
    finally:
        os._exit(status)


def syscall(number, *args):
    result = LIBC.syscall(ctypes.c_long(number), *(ctypes.c_long(arg) for arg in args))
    if result < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return result


def peer_descriptor(pid):
    """Duplicate fd 0 out of a client process; None where the kernel cannot."""
    if not PIDFD_GETFD:
        return None
    try:
        pidfd = syscall(SYS_PIDFD_OPEN, pid, 0)
        try:
            return syscall(SYS_PIDFD_GETFD, pidfd, 0, 0)
        finally:
            os.close(pidfd)
    except OSError as error:
        if error.errno == errno.ENOSYS:
            return None
        raise


def reopen_peer_stdin(pid):
    """Open a client's fd 0 anew through /proc, for kernels without pidfd_getfd.

    For a pipe that comes to the same. A regular file starts where the caller
    is, but what the command reads does not move the caller along.
    """
    path = f'/proc/{pid}/fd/0'
    mode = os.stat(path).st_mode
    if not stat.S_ISREG(mode) and not stat.S_ISFIFO(mode):
        raise RequestError('Standard input is neither a pipe nor a regular file; use -n to run without it')
    # O_NONBLOCK keeps a named FIFO without writers from blocking the open.
    own = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
    try:
        if stat.S_ISREG(mode):
            with open(f'/proc/{pid}/fdinfo/0') as info:
                position = next(int(line.split()[1]) for line in info if line.startswith('pos:'))
            os.lseek(own, position, os.SEEK_SET)
        os.set_blocking(own, True)
    except BaseException:
        os.close(own)
        raise
    return own


def open_peer_stdin(conn):
    """Take the connecting client's fd 0 for the command to read directly.

    A regular file has to be the caller's own open file description: one
    reopened through /proc carries an offset of its own, and the caller would
    read again what the command consumed. A pipe has no offset to share, so it
    gets a description of ours, which blocks whatever mode the caller keeps
    its own in.
    """
    size = struct.calcsize('3i')
    pid, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size))
    if uid != os.getuid():
        raise RequestError('Peer credentials rejected')
    try:
        fd = peer_descriptor(pid)
        if fd is None:
            return reopen_peer_stdin(pid)
    except (OSError, StopIteration) as error:
        reason = getattr(error, 'strerror', None) or 'no access to the client process'
        raise RequestError(f'Cannot attach standard input ({reason}); use -n to run without it')
    mode = os.fstat(fd).st_mode
    if stat.S_ISREG(mode):
        return fd
    try:
        if not stat.S_ISFIFO(mode):
            raise RequestError('Standard input is neither a pipe nor a regular file; use -n to run without it')
        try:
            # O_NONBLOCK keeps a named FIFO without writers from blocking the open.
            own = os.open(f'/proc/self/fd/{fd}', os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        except OSError as error:
            raise RequestError(f'Cannot attach standard input ({error.strerror}); use -n to run without it')
    finally:
        os.close(fd)
    os.set_blocking(own, True)
    return own


def cwd_error(problem, path):
    shared = ', '.join(map(str, ROOTS)) or 'none'
    return RequestError(f'Working directory {problem}: {path} (use -C to run in a shared one: {shared})')


def spawn_error(error, command, cwd):
    if error.filename == cwd and command != cwd:
        return RequestError(f'Cannot enter working directory {cwd}: {error.strerror}')
    if isinstance(error, FileNotFoundError):
        return RequestError(f'Command not found: {command}', 127)
    return RequestError(f'Cannot execute {command}: {error.strerror or error}', 126)


def usable(text):
    """Whether a string can go to the kernel as a path or an argument."""
    if '\0' in text:
        return False
    try:
        os.fsencode(text)
    except UnicodeEncodeError:
        return False
    return True


def read_request(conn):
    data = b''
    while b'\n' not in data:
        part = conn.recv(CHUNK)
        if not part:
            return None
        data += part
        if len(data) > MAX_REQUEST:
            raise RequestError('Request too large (limit 1 MiB)')
    initial, control = data.split(b'\n', 1)
    try:
        req = json.loads(initial)
    except ValueError:
        raise RequestError('Invalid request encoding')
    if not isinstance(req, dict):
        raise RequestError('Invalid request')
    if req.get('version') != 1:
        raise RequestError('Unsupported request version')
    if not isinstance(req.get('token'), str) or not hmac.compare_digest(req['token'], TOKEN):
        raise RequestError('Authentication rejected')
    args = req.get('argv')
    if not isinstance(args, list) or not args \
            or not all(isinstance(a, str) and usable(a) for a in args) or not args[0]:
        raise RequestError('Invalid argv')
    raw_cwd = req.get('cwd')
    if not isinstance(raw_cwd, str) or not raw_cwd or not usable(raw_cwd):
        raise RequestError('Invalid working directory')
    path = Path(raw_cwd)
    if not path.is_absolute():
        raise RequestError(f'Working directory must be absolute: {raw_cwd}')
    try:
        cwd = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise cwd_error('does not exist on the host', raw_cwd)
    if not any(cwd == root or root in cwd.parents for root in ROOTS):
        raise cwd_error('is not shared with the host', cwd)
    timeout = req.get('timeout', 600)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 3600:
        raise RequestError('Invalid timeout')
    return {
        'argv': list(args),
        'cwd': str(cwd),
        'timeout': float(timeout),
        'attach_stdin': req.get('attach_stdin') is True,
        'merge': req.get('merge') is True,
        'control': control,
    }


def drain_output(sel, conn, seconds):
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0 or all(key.data == 'control' for key in sel.get_map().values()):
            return
        for key, _ in sel.select(min(left, 0.1)):
            if key.data == 'control':
                try:
                    if not conn.recv(CHUNK):
                        return
                except OSError:
                    return
                continue
            chunk = os.read(key.fileobj.fileno(), CHUNK)
            if not chunk:
                sel.unregister(key.fileobj)
                continue
            send(conn, {key.data: base64.b64encode(chunk).decode()})


def handle(conn):
    child = None
    sel = None
    slow = None
    stdin_fd = None
    failure = None
    outbox = b''
    try:
        conn.settimeout(15)
        send(conn, {'hello': PROTOCOL})
        request = read_request(conn)
        if request is None:
            return
        if request['attach_stdin']:
            stdin_fd = open_peer_stdin(conn)
        merge = request['merge']
        with GUARD:
            if STOP.is_set():
                raise RequestError('Bridge is stopping')
            try:
                child = subprocess.Popen(
                    request['argv'], cwd=request['cwd'], env=ENV,
                    stdin=subprocess.DEVNULL if stdin_fd is None else stdin_fd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                    start_new_session=True)
            except OSError as error:
                raise spawn_error(error, request['argv'][0], request['cwd']) from None
            ACTIVE.add(child)
            announce(child.pid, True)
        if stdin_fd is not None:
            os.close(stdin_fd)
            stdin_fd = None

        control = request['control']
        deadline = time.monotonic() + request['timeout']
        conn.setblocking(False)
        sel = selectors.DefaultSelector()
        sel.register(conn, selectors.EVENT_READ, 'control')
        sel.register(child.stdout, selectors.EVENT_READ, 'stdout')
        if not merge:
            sel.register(child.stderr, selectors.EVENT_READ, 'stderr')
        # Watched in place of the above while the client is behind.
        slow = selectors.DefaultSelector()
        slow.register(conn, selectors.EVENT_READ | selectors.EVENT_WRITE)
        open_streams = 1 if merge else 2
        drain_deadline = None
        interrupted = None
        timed_out = False
        result = None
        while True:
            if STOP.is_set():
                raise RequestError('Bridge is stopping')
            if len(control) > MAX_REQUEST:
                raise RequestError('Control message too large')
            while b'\n' in control:
                line, control = control.split(b'\n', 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    raise RequestError('Invalid control message')
                if not isinstance(message, dict):
                    raise RequestError('Invalid control message')
                sig = SIGNALS.get(message.get('signal'))
                if sig is not None and exit_status(child) is None:
                    signal_group(child.pid, sig)
                    interrupted = sig
                    deadline = min(deadline, time.monotonic() + 3)
            if outbox:
                # The client has not taken what it was sent. Nothing more is
                # read from the command until it has, so the command waits as
                # well.
                waited = time.monotonic()
                for _, events in slow.select(0.2):
                    if events & selectors.EVENT_WRITE:
                        try:
                            outbox = outbox[conn.send(outbox):]
                        except BlockingIOError:
                            pass
                    if events & selectors.EVENT_READ:
                        try:
                            chunk = conn.recv(CHUNK)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            raise ClientGone()
                        control += chunk
                if drain_deadline is not None:
                    # Pipes nobody read from in the meantime say nothing
                    # about background jobs holding them open.
                    drain_deadline += time.monotonic() - waited
                if outbox and time.monotonic() >= deadline:
                    timed_out = True
                    break
                continue
            if result is not None:
                break
            now = time.monotonic()
            code = exit_status(child)
            if code is not None:
                if open_streams == 0:
                    result = {'exit': code}
                elif drain_deadline is None:
                    drain_deadline = now + DRAIN
                elif now >= drain_deadline:
                    # The command exited but something it started still holds the
                    # output pipes; report the real exit status instead of waiting.
                    result = {'exit': code, 'background': True}
                if result is not None:
                    outbox = encode(result)
                    continue
            elif now >= deadline:
                timed_out = True
                break
            for key, _ in sel.select(0.2):
                if key.data == 'control':
                    try:
                        chunk = conn.recv(CHUNK)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        raise ClientGone()
                    control += chunk
                else:
                    chunk = os.read(key.fileobj.fileno(), CHUNK)
                    if chunk:
                        outbox += encode({key.data: base64.b64encode(chunk).decode()})
                    else:
                        sel.unregister(key.fileobj)
                        open_streams -= 1

        if timed_out:
            stopped = exit_status(child) is not None
            if not stopped:
                signal_group(child.pid, signal.SIGTERM)
                limit = time.monotonic() + 2
                # A group we cannot signal will not react; leave it to the reclaim.
                while exit_status(child) is None and time.monotonic() < limit and group_state(child.pid) == 'ours':
                    time.sleep(0.05)
                if exit_status(child) is None:
                    signal_group(child.pid, signal.SIGKILL)
            # From here on the client gets a few seconds for each message.
            conn.settimeout(5)
            conn.sendall(outbox)
            outbox = b''
            if result is None:
                drain_output(sel, conn, 0.5)
                if stopped:
                    raise RequestError('Output was not taken before the timeout', 124)
                if interrupted is not None:
                    raise RequestError(f'Command did not stop on {interrupted.name}', 128 + interrupted)
                raise RequestError('Command timed out', 124)
    except RequestError as error:
        failure = {'error': str(error), 'exit': error.code}
    except (ClientGone, OSError):
        pass
    except Exception as error:
        log('request failed:', repr(error))
        failure = {'error': 'Bridge request or command failed', 'exit': 125}
    finally:
        # Every step stands alone: a failure in one must not skip the rest,
        # or the slot, the descriptors and the connection leak.
        if failure is not None:
            try:
                conn.settimeout(5)
                # A line the client has in part comes first.
                conn.sendall(outbox + encode(failure))
            except OSError:
                pass
        if stdin_fd is not None:
            try:
                os.close(stdin_fd)
            except OSError:
                pass
        if child is not None:
            # Out of ACTIVE first: once kill_group has reaped the command its
            # pid is no longer ours to signal, and a stopping bridge signals
            # what it finds in there.
            with GUARD:
                ACTIVE.discard(child)
            try:
                kill_group(child)
            except Exception as error:
                log('cleanup error:', repr(error))
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            announce(child.pid, False)
        for selector in (sel, slow):
            if selector is not None:
                try:
                    selector.close()
                except Exception:
                    pass
        try:
            conn.close()
        except Exception:
            pass
        SLOTS.release()


def main():
    global TOKEN
    os.umask(0o077)
    RUN.mkdir(mode=0o700, exist_ok=True)
    for name in ('tmp', 'cache'):
        (RUN / name).mkdir(mode=0o700, exist_ok=True)
    lock = open(RUN / 'lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    ROOTS[:] = shared_dirs()
    log('commands may start in:', ', '.join(map(str, ROOTS)) or 'nowhere')
    start_reaper(lock)
    sock = RUN / 'bridge.sock'
    sock.unlink(missing_ok=True)
    TOKEN = secrets.token_hex(32)
    (RUN / 'token').write_text(TOKEN)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(sock))
    sock.chmod(0o600)
    server.listen(32)
    server.settimeout(0.5)
    (RUN / 'pid').write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    workers = []
    try:
        while not STOP.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            if not SLOTS.acquire(blocking=False):
                try:
                    conn.sendall(b'{"error":"Too many concurrent commands","exit":125}\n')
                except OSError:
                    pass
                conn.close()
                continue
            worker = threading.Thread(target=handle, args=(conn,))
            workers = [t for t in workers if t.is_alive()]
            workers.append(worker)
            worker.start()
    finally:
        STOP.set()
        try:
            server.close()
        except Exception:
            pass
        with GUARD:
            children = list(ACTIVE)
        # Each worker finishes the cleanup of its own command, su included.
        for child in children:
            signal_group(child.pid, signal.SIGKILL)
        for worker in workers:
            try:
                worker.join()
            except Exception:
                pass
        for name in ('bridge.sock', 'token', 'pid'):
            try:
                (RUN / name).unlink(missing_ok=True)
            except Exception:
                pass


if __name__ == '__main__':
    main()
