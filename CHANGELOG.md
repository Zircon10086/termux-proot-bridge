# Changelog

Versions are those that `termux-run --version` reports. The bridge has a revision number of its own, which it announces when a connection opens and by which the client tells what it can rely on.

## 1.0.0 (2026-11-12)

The first public release, with bridge revision 4. The versions before it were used in one private setup and never released; they count as 0.x here. 0.2.1 was the last of them in regular use, and 0.2.2 was the development version this release was made from. What follows is the difference from 0.2.1 (bridge revision 3).

### Added

- `install.sh` installs, upgrades and removes the bridge and puts the client into the proot-distro containers it is given.
- The host command `termux-proot-bridge start|stop|status`.
- `termux-run --status` checks that the bridge can be reached and shows which one was found.
- The directories that may serve as working directories can be set in the file `shared-dirs` on the host. By default they are the Termux home and `/storage/emulated/0`.
- The client finds the bridge's run directory by itself, bound to `/run/termux-bridge` or under the host's own path, and `TERMUX_RUN_DIR` overrides that.
- On a kernel without `pidfd_getfd` (before Linux 5.6), standard input is opened anew through `/proc` instead of being refused.

### Fixed

- Output for a slow reader no longer piles up in the client: it holds at most 16 MiB per stream, then the bridge stops reading from the command, and the command waits. Before, 150 MB of output made the client take more than 200 MB of memory, and the bridge dropped a client that read nothing for five seconds; now it waits.
- Past the connection limit the client shows the bridge's reason, `Too many concurrent commands`, instead of a general connection failure. The client now sends its request only after the bridge's greeting.
- A request format the bridge does not accept is answered with `Unsupported request version`, no longer as a failed authentication.
- When the bridge is killed outright, the commands it started are no longer orphaned: a separate watcher process takes them down, including those that became root through `su`.
- A command that has exited is no longer reaped before its process group is cleaned up. Until then its process id could be given to another process, and since the cleanup kills the group by that number, through `su` if need be, it could hit an unrelated process.
- Arguments that are not UTF-8 reach the host command unchanged, with `--root` as well. Before, such bytes were replaced by U+FFFD.
- An argument or working directory that cannot be encoded as bytes is refused by name (`Invalid argv`, `Invalid working directory`) instead of ending in an internal error.
- When the run directory holds only what an earlier bridge left behind, the client says that the bridge is not running and how to start it.

### Changed

- All messages of the client and the installer are in English.
- No deployment paths are built in any more: interpreters are found through `#!/usr/bin/env`, the host's `HOME` and `PREFIX` come from the environment, and no message refers to the former setup.
- The environment of host commands keeps the `TERMUX_*` variables, and `LD_PRELOAD` is the one Termux itself sets.
- A refused working directory is reported together with the shared directories that would do.
- The tests no longer depend on particular directories or install locations, and the protocol tests no longer need Node.js on the host.
