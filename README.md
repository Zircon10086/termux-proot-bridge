# termux-proot-bridge

Run commands on the Termux host from inside a PRoot container. `termux-run command [args...]` hands the command to Termux, outside the container, and brings back its standard output, standard error and exit status.

```console
$ uname -o                      # in the container
GNU/Linux
$ termux-run uname -o           # on the host
Android
$ printf 'a\nb\n' | termux-run wc -l
2
$ termux-run -c 'exit 37'; echo $?
37
```

What the container cannot install or use (`pkg`, `am`, the `termux-*` tools, the host's compilers and SDKs, `su`) becomes reachable without leaving it.

> **Read [Security](#security) first.** While the bridge runs, every process in the container can run any command on the host as the Termux user, and as root if the device is rooted and Termux has been granted su.

## How it works

- `termux-proot-bridge`, the bridge: a Python process in Termux. It listens on a Unix socket, starts the commands it is asked for and streams their output back.
- `termux-run`, the client: a Node.js script in the container. It parses the options, sends the request, writes out the result and forwards Ctrl-C.

Between them goes newline-delimited JSON, one request per connection; the format is described at the top of `host/bridge.py`. Arguments travel as an array and never pass through a shell. Both sides use their standard library only.

This is not file sharing: `termux-run` runs commands and nothing else. A command sees the host's file system, where the container's own files (its `/tmp`, `/home`, `/usr`) do not exist.

## Requirements

| Where | What |
|---|---|
| Host (Termux) | `python`, `bash`; `proot-distro` if `install.sh` is to install the client |
| Container | Node.js on the `PATH` |

It has been verified on one setup only: Android 16, kernel 6.1, aarch64; Termux with Python 3.13 and proot-distro 5.3.0; a Debian 13 container with Node.js 24. Other combinations are untested.

## Installation

In Termux, outside the container:

```sh
pkg install python git
git clone https://github.com/Zircon10086/termux-proot-bridge
cd termux-proot-bridge
bash install.sh CONTAINER       # the container's name in proot-distro; several may be given
```

The script does three things:

1. It puts the bridge into `~/.local/lib/termux-proot-bridge/` and the command `termux-proot-bridge` into `$PREFIX/bin/`.
2. It puts the client into the container as `/usr/local/bin/termux-run`.
3. It starts the bridge.

Without a container name it installs the host part only and lists the containers it found. Running it again upgrades; that restarts the bridge and ends the commands running through it. A file of the same name that is not this project's stops the script before it changes anything.

For a container that proot-distro does not manage, copy `guest/termux-run` into it and make it executable.

### Removal

```sh
bash install.sh --uninstall CONTAINER
```

This removes the files the installer put there and the bridge's own run files. Your `shared-dirs` and whatever commands left in `run/tmp` and `run/cache` are kept and listed; delete them by hand once you are sure you do not need them.

## Usage

### Starting the bridge

The bridge does not start with Termux. After each restart of Termux, run this in Termux before entering the container:

```sh
termux-proot-bridge start      # also: stop, status
```

It goes well together with the login: `termux-proot-bridge start && proot-distro login CONTAINER`.

In the container, `termux-run --status` tells whether the bridge can be reached.

### Entering the container

**A default login** (`proot-distro login CONTAINER`) needs nothing more: proot-distro binds the Termux home into the container under its own path, and the client finds the bridge there.

**An isolated login** (`--isolated`) sees none of the host's directories, so two things have to be bound by hand:

```sh
proot-distro login --isolated \
  --bind "$HOME/.local/lib/termux-proot-bridge/run:/run/termux-bridge" \
  --bind /storage/emulated/0/projects:/storage/emulated/0/projects \
  CONTAINER
```

The first lets the client find the bridge. The second is a directory to run commands in, and it **has to be bound to the same path as on the host** (see below).

The client looks in `/run/termux-bridge` and then in `/data/data/com.termux/files/home/.local/lib/termux-proot-bridge/run`. Where neither is right (a Termux fork with another package name, for one), set the environment variable `TERMUX_RUN_DIR` to the path of the bridge's run directory as the container sees it.

### Working directory

A command runs in the caller's current directory, or in the one given with `-C`. That only means something where container and host see the **same directory** under the **same path**, so the bridge accepts a working directory only inside a "shared directory". By default these are the Termux home and `/storage/emulated/0`, the two that a default proot-distro login binds under their own paths.

In a directory that belongs to the container alone (its `/tmp` or `/root`, say) the command does not run, and the exit status is 125:

```
termux-run: Working directory is not shared with the host: /tmp (use -C to run in a shared one: ...)
```

Where the host has no such path at all, the message is `Working directory does not exist on the host`. Either way, name a shared directory with `-C`, or `cd` into one first.

To change the shared directories, create `~/.local/lib/termux-proot-bridge/shared-dirs` on the host with one absolute path per line (lines starting with `#` are comments), then `termux-proot-bridge stop && termux-proot-bridge start`. Once the file exists the defaults no longer apply. It limits where commands start, **not what they can reach**.

### Options

```
termux-run [options] command [args...]
termux-run [options] -c 'string' [args...]
termux-run --status

  -C, --cwd PATH        working directory (default: current directory)
  -t, --timeout SECS    time limit, 1..3600 (default: 600)
  -n, --no-stdin        do not hand standard input to the command
      --root            run as root (su -c)
  -M, --mount-master    pass -M to su; only with --root
  -c STRING             run STRING with bash -c on the host
      --status          check that the bridge can be reached
  -h, --help            show the help
      --version         show the version
```

Everything after `--` is the command, for one whose name starts with `-`.

```sh
termux-run pkg list-installed
termux-run -C /data/data/com.termux/files/home/src/app make -j4
termux-run -t 30 -c 'for f in *.log; do wc -l "$f"; done'
termux-run --root id -u
```

### Standard input

- **A pipe or a regular file** on standard input is read by the host command itself. No data is copied: what the command does not read stays with the caller, and what it does read is gone for the caller too. So `termux-run` can be called inside a `while read` loop without eating the loop's input.
- **A terminal, `/dev/null`, a socket or another device** is not handed over, and the command reads `/dev/null`. A child process of Node.js gets a socket for standard input; write `cat | termux-run ...` there to turn it into a pipe.
- `-n` never hands it over.

For a regular file, caller and command share one read position. That takes `pidfd_getfd`, which came with Linux 5.6. On an older kernel the bridge opens the file anew: the command starts reading where the caller is, but what it reads does not move the caller along. This fallback has only been tested with `pidfd_getfd` switched off by force, never on a kernel that old.

### Output

Standard output and standard error come back separately and byte for byte. Where both go to the same file or pipe (`2>&1`), the host merges them and the order is the one the command wrote them in.

When the reader downstream is slow, the command is made to wait instead of the output piling up in memory (the client holds at most 16 MiB per stream).

### Exit status

The command's own exit status is passed through, 128+N if signal N ended it. Apart from that:

| Status | Meaning |
|---|---|
| 2 | wrong use of `termux-run`'s options |
| 124 | timed out (SIGTERM, then SIGKILL two seconds later) |
| 125 | the bridge cannot be reached, refused the request, or the working directory is not valid |
| 126 | the command cannot be executed |
| 127 | the command was not found |
| 141 | the reader downstream closed the pipe early |

## Behaviour and limits

- **No terminal.** Host commands get no TTY, so interactive programs (editors, `top`, password prompts) do not work, and a command that asks questions needs its `-y` or the like.
- **The environment is not passed on.** A command gets a Termux environment that the bridge builds (`HOME`, `PREFIX`, `PATH`, `LD_PRELOAD`, `ANDROID_*`, `TERMUX_*` and a few more); the container's variables do not reach the host. Write `termux-run env NAME=value command` where one is needed, or assign it in the string given to `-c`. `TMPDIR` and `XDG_CACHE_HOME` point to `tmp` and `cache` in the bridge's run directory.
- **Signals.** SIGINT (Ctrl-C) and SIGTERM sent to the client are forwarded to the whole process group of the host command. If it has not exited three seconds later it gets SIGTERM, and SIGKILL two seconds after that; the exit status is 128+N. A client that is killed or disconnects has the process group of its command cleaned up as well.
- **Background processes.** When a command exits, the background processes it left in its process group are killed. If something still holds the output pipes 1.5 seconds after the command exited, the client stops waiting, returns the command's real exit status and says so. A process that is meant to stay on the host has to detach with `setsid`, and the command has to wait until it has:

  ```sh
  termux-run -c 'setsid my-daemon >/dev/null 2>&1 </dev/null & sleep 0.3'
  ```

- **Arguments** arrive unchanged: spaces, quotes, shell metacharacters and bytes that are not UTF-8.
- **Limits.** 1 MiB for one request (the command with all its arguments); 16 connections at a time, and a call beyond that fails at once with 125; 3600 seconds for one command. A connection that sends no request holds its place until it is dropped after 15 seconds.
- **Several containers.** There is one bridge. All containers and sessions share it, and it does not tell them apart.
- **A bridge that dies.** If the bridge is killed, SIGKILL included, a separate watcher process takes down the commands that were running, and waiting clients return at once with 125. Run `termux-proot-bridge start` again; nothing has to be cleaned up by hand. Android may kill Termux in the background: for long sessions run `termux-wake-lock` in Termux and lift the battery restrictions on Termux in the system settings.
- **Termux:API** commands (`termux-battery-status` and so on) are untested.

## Security

The bridge exists to let programs in the container run commands outside it. It is not a sandbox and must not be taken for a part of one.

- **Who can use it.** The socket and the token file have mode 600 and their directory 700, so every process with Termux's Android UID can connect, and root. The processes in a PRoot container have that UID whatever user they appear as inside, so **any program in the container can use the bridge**, including the third-party software and scripts you run there.
- **What it can do.** Run any command as the Termux user; there is no allowlist. The shared directories restrict only where a command starts. The command can reach everything Termux can: the Termux home, the keys and configuration in it, the shared storage.
- **The token is not authentication.** It is random and new at every start, and it keeps a client from talking to something else and keeps out processes that cannot read the token file. Every process with the same UID that can read the file passes.
- **`--root`.** On a rooted device where Termux has been granted su, `termux-run --root` lets programs in the container run commands on the host as root. The bridge asks for no confirmation of its own; whether it goes through is up to what the root manager grants Termux.
- **The one time the bridge calls su by itself.** When it cleans up the process group of a command it started and an ordinary `kill` is refused (the group has members that became root through `su`), it runs `su -c 'kill -9 -- -PGID'` once and records that in `~/.local/lib/termux-proot-bridge/run/service.log`. It uses su for nothing else.
- **The log.** `service.log` records the shared directories at start and the cleanups above. In normal operation it records no commands, arguments, output or tokens. An internal error is logged with its exception.
- **PRoot is no security boundary.** Without this project the processes in the container have Termux's UID just the same. What the project removes is the inconvenience that programs in the container do not reach the host environment by default. To keep a container session from using the bridge, run `termux-proot-bridge stop` first.

## Troubleshooting

| What you see | Why, and what to do |
|---|---|
| `No bridge found (no readable token in ...)` | The bridge is not started, or the container does not see its run directory. Run `termux-proot-bridge start` in Termux; for an isolated login add the `--bind` above or set `TERMUX_RUN_DIR` |
| `The bridge is not running` | The run directory holds files from last time and the bridge is gone, most likely because Android killed Termux. Start it again |
| `Working directory is not shared with the host` / `does not exist on the host` | The current directory belongs to the container alone or is outside the shared directories. Use `-C`, or adjust `shared-dirs` |
| `Too many concurrent commands` | More than 16 calls at a time |
| `Command not found`, though the host has it | The `PATH` of host commands is `$PREFIX/bin:/system/bin`; name programs elsewhere by their full path |
| `Cannot attach standard input` | The bridge cannot get at the caller's standard input. Add `-n`, or use `cat file \| termux-run ...` |
| The command seems to hang | Most likely it waits for input from a terminal that is not there. Give it its non-interactive option, or a time limit with `-t` |
| `Bridge did not become ready` | See `~/.local/lib/termux-proot-bridge/run/service.log` |

## Development and tests

Both test suites start a bridge instance of their own by default and leave the running one alone.

```sh
# protocol tests, run in Termux
python3 host/test-bridge.py [--live[=DIR]] [--root]

# client tests, run in the container from inside a shared directory
# (they start the instance under test through a bridge that is already running)
bash guest/test-client.sh [--live] [--root] [CLIENT [BRIDGE_PY]]
```

`--live` tests the running bridge instead, and `--root` adds the cases that go through `su` (on a rooted device).

## License

MIT, see [LICENSE](LICENSE).
