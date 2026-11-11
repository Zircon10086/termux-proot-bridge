# termux-proot-bridge

`termux-run command [args...]`, run inside a Termux PRoot container, hands the command to the Termux host outside it and brings back the output and the exit status. What users need to know is in `README.md`.

## State

- Client `termux-run 1.0.0`, bridge revision 4 (`PROTOCOL` in `bridge.py`). 1.0.0 is the first public release; `CHANGELOG.md` has what changed.
- The version is `VERSION` in `guest/termux-run` and the top entry of `CHANGELOG.md`. A release is tagged `vX.Y.Z`.
- The implementation was not written from scratch: it was imported from a setup that was in daily use (then 0.2.1 / revision 3).
- MIT license. The code uses the standard libraries of Python and Node only.
- Code, messages and documentation are in English.

## Layout

- `guest/termux-run`: the client in the container (Node). Parses the options, sends the request, writes out the result, forwards SIGINT/SIGTERM.
- `host/bridge.py`: the bridge on the host. Unix socket, newline-delimited JSON, one request per connection; the protocol is described at the top of the file.
- `host/bridge-control.py`: `start` / `stop` / `status` for the bridge; installed as the command `termux-proot-bridge`.
- `install.sh`: installs, upgrades, removes.
- `host/test-bridge.py`: protocol tests, run on the host.
- `guest/test-client.sh`: client tests, run in the container.

## Tests

Both suites start a bridge instance of their own by default and leave the running one alone. `--live` tests the running bridge instead; `--root` adds the cases that go through `su`.

- Protocol tests: `python3 host/test-bridge.py [--live[=DIR]] [--root]`
- Client tests: `bash guest/test-client.sh [--live] [--root] [CLIENT [BRIDGE_PY]]`, run in the container from inside a shared directory. They need a bridge that is already running to start the instance under test.

After a change to the bridge or the client, both suites have to pass in isolated mode with `--root`. To fix a defect, first add a case that fails on the old code.

When a change alters what a client can observe of the bridge, raise `PROTOCOL` and say in the protocol description at the top of the file what the new revision guarantees. The client decides by the revision what it may rely on, and a new client has to work with an old bridge.

## Constraints to know before changing anything

Each of these was settled after something went wrong. Do not delete the test cases that go with them.

- **The client does not read standard input.** It only asks for `attach_stdin` when fd 0 is a pipe or a regular file, and the bridge duplicates fd 0 out of the client process for the command. A client that reads and forwards hangs when standard input is a socket that never ends, which is what a child process of Node gets.
- **Regular files and pipes are attached differently.** A regular file has to be the caller's own open file description (`pidfd_getfd`), or the caller reads again what the command consumed. A pipe has to be reopened as a description of the bridge's own and set to blocking, or the command reads EAGAIN when the caller keeps its pipe non-blocking. Without `pidfd_getfd` in the kernel the bridge falls back to reopening through `/proc`, and the file position is no longer shared.
- **Sockets, terminals and character devices are not attached**; the command reads `/dev/null`.
- **The client sends its request only after the bridge's greeting.** At the connection limit the first line is an error and the bridge hangs up; a request written before that turns the error into a general connection failure.
- **Output piles up on neither side without bound.** When the bridge cannot send, it stops reading from the command (`outbox` in `handle`), and the command waits. The client holds at most 16 MiB per stream and then stops reading the socket. The bridge must not drop a client for not reading for a while.
- **An exited command is not reaped before its process group is cleaned up.** The command's pid is also the id of its group; once reaped, the number can go to an unrelated process, and the cleanup kills the group by that number, through `su` if need be. While a command runs, use `exit_status` (which looks without reaping), not `child.poll()`.
- **Every step of the cleanup stands alone.** A failure in one must not skip the rest, or the slot, the descriptors and the connection leak.
- **Reclaiming root leftovers.** `su -c 'kill -9 -- -PGID'` runs once, only after an ordinary `killpg` was refused with EPERM, and only for a process group the bridge started. Reap your own child before probing: a zombie group leader keeps `killpg(pgid, 0)` succeeding. Only ENOENT means a process is gone; the `/proc` entry of a root process answers EACCES to an ordinary user.
- **The watcher process.** At start the bridge forks a process that learns through a pipe of every command started and cleaned up. When the pipe ends, however the bridge went, it kills the groups not yet cleaned up. It has to close the lock file it inherited, or a dead bridge looks alive.
- **Only the process group is reclaimed.** A process that detached with `setsid` has to outlive the call. That is intended.
- **No waiting for background processes that hold the output pipes.** 1.5 seconds after the command exited, its real exit status is returned with `background` set. Time during which the client was behind does not count towards the 1.5 seconds.
- **`--root` does not imply `-M`.**
- **Arguments travel as bytes.** Bytes that are not UTF-8 go as lone surrogates U+DC80..U+DCFF (Python's surrogateescape). With `--root` they become octal escapes inside `$'...'`, because some su implementations refuse a command string that is not UTF-8.
- **No deployment paths in the code.** The host's HOME and PREFIX come from the environment, interpreters from `#!/usr/bin/env`, and the allowed working directories from `shared-dirs` or its default.
- **A Unix socket created inside the container needs a short path.** PRoot translates the container path into a host path below the rootfs. When the result is too long, the socket lands somewhere private to that PRoot instance: other instances cannot connect and it does not show in the directory. Measured: 100 bytes after translation work, 111 do not; the limit for a Unix socket path is 107 bytes. The bridge's own socket is bound in from the host and not affected, but the directory names of test instances are short for this reason.
