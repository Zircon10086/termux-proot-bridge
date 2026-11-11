#!/usr/bin/env bash
# Install, upgrade or remove termux-proot-bridge.
#
# Run it in Termux, outside the container:
#
#   bash install.sh [CONTAINER...]              install or upgrade
#   bash install.sh --uninstall [CONTAINER...]  remove
#
# The host bridge goes to ~/.local/lib/termux-proot-bridge and its command
# `termux-proot-bridge` to $PREFIX/bin. CONTAINER is a proot-distro container
# that gets (or loses) /usr/local/bin/termux-run. For a container of another
# kind, copy guest/termux-run into it by hand.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
uninstall=0
if [ "${1:-}" = --uninstall ]; then uninstall=1; shift; fi
case "${1:-}" in
  -*) sed -n '2,12s/^# \{0,1\}//p' "$0"; exit 2;;
esac

die() { echo "install.sh: $*" >&2; exit 1; }

[ -n "${PREFIX:-}" ] && [ -d "$PREFIX/bin" ] && [ -n "${HOME:-}" ] \
  || die 'run this in Termux, outside the container'
lib=$HOME/.local/lib/termux-proot-bridge
launcher=$PREFIX/bin/termux-proot-bridge
distro=$PREFIX/var/lib/proot-distro
mark='termux-proot-bridge launcher'

rootfs() {
  local dir
  for dir in "$distro/containers/$1/rootfs" "$distro/installed-rootfs/$1"; do
    if [ -d "$dir" ]; then printf '%s\n' "$dir"; return; fi
  done
  die "no proot-distro container named $1 under $distro"
}

# Only a file that is ours gets replaced or removed.
ours() { [ ! -e "$1" ] || grep -q "$2" "$1"; }

stop_bridge() {
  [ -e "$lib/bridge-control.py" ] || return 0
  python3 "$lib/bridge-control.py" stop || die 'the bridge did not stop; nothing changed'
}

# Everything is checked before anything is changed.
ours "$launcher" "$mark" \
  || die "$launcher exists and was not installed by this project; nothing changed"
for name; do
  target=$(rootfs "$name")/usr/local/bin/termux-run
  ours "$target" "VERSION = 'termux-run" \
    || die "$target exists and is not this project's client; nothing changed"
done

if [ "$uninstall" = 1 ]; then
  stop_bridge
  rm -f "$launcher" "$lib/bridge.py" "$lib/bridge-control.py"
  for name in bridge.sock token pid lock service.log; do rm -f "$lib/run/$name"; done
  # Directories go only when empty: commands may have left files in tmp and
  # cache, and shared-dirs is the user's.
  for dir in "$lib/run/tmp" "$lib/run/cache" "$lib/run" "$lib"; do
    rmdir "$dir" 2>/dev/null || true
  done
  echo "Removed the host part."
  if [ -d "$lib" ]; then
    echo "Kept what the installer did not put into $lib (your shared-dirs, files that commands left behind):"
    find "$lib" -mindepth 1 | head -n 20 | sed 's/^/  /'
    echo "Delete that directory by hand once you are sure you do not need them."
  fi
  for name; do
    rm -f "$(rootfs "$name")/usr/local/bin/termux-run"
    echo "Removed termux-run from container $name."
  done
  exit 0
fi

python=$(command -v python3) || die 'the host needs Python: pkg install python'

# An upgrade replaces a running bridge, which ends the commands it is running.
stop_bridge
mkdir -p "$lib"
install -m 644 "$here/host/bridge.py" "$lib/bridge.py"
install -m 644 "$here/host/bridge-control.py" "$lib/bridge-control.py"
# Interpreters are named in full: the launcher has to work where termux-exec
# is not loaded and /usr/bin/env does not exist.
cat > "$launcher.new" <<EOF
#!/system/bin/sh
# $mark, written by install.sh
exec '$python' '$lib/bridge-control.py' "\$@"
EOF
chmod 755 "$launcher.new"
mv -f "$launcher.new" "$launcher"
echo "Installed the host part in $lib, command: termux-proot-bridge"

for name; do
  root=$(rootfs "$name")
  install -D -m 755 "$here/guest/termux-run" "$root/usr/local/bin/termux-run"
  echo "Installed the client in container $name: /usr/local/bin/termux-run (it needs Node.js on the container's PATH)"
done
if [ "$#" = 0 ]; then
  names=$(ls -1 "$distro/containers" "$distro/installed-rootfs" 2>/dev/null | grep -v ':$' | grep . || true)
  echo "No container given, so the client was not installed. proot-distro containers here: ${names:-(none found)}" | tr '\n' ' '
  echo
  echo "To install the client: bash install.sh CONTAINER"
fi

"$launcher" start
echo "The bridge does not start with Termux: run 'termux-proot-bridge start' before entering the container."
