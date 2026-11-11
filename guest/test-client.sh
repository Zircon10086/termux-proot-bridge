#!/bin/bash
# Client-side tests for termux-run, run inside the PRoot container.
#
# By default this starts a private bridge instance on the host from the
# bridge.py under test (through the bridge that is already running) and
# drives CLIENT against it, so neither side has to be installed first.
# --live tests against the running bridge instead. --root adds the cases that
# go through su.
#
# Run it from a directory the running bridge shares with the host. CLIENT and
# BRIDGE_PY are paths in the container and default to the files next to this
# script.
#
# Usage: test-client.sh [--live] [--root] [CLIENT [BRIDGE_PY]]
set -u
LIVE=0
ROOT=0
while :; do
  case "${1:-}" in
    --live) LIVE=1; shift;;
    --root) ROOT=1; shift;;
    *) break;;
  esac
done
here=$(cd "$(dirname "$0")" && pwd)
CLIENT=$(readlink -f "${1:-$here/termux-run}")
BRIDGE=$(readlink -f "${2:-$here/../host/bridge.py}")
SHARED=$(pwd -P)
scratch=$(mktemp -d) || exit 1
name=
pass=0
fail=0

# A checkout on Android's shared storage has no executable bits.
if [ ! -x "$CLIENT" ]; then
  install -m 755 "$CLIENT" "$scratch/termux-run" || exit 1
  CLIENT=$scratch/termux-run
fi

# The bridge that is already running, by the directory the client finds it in.
outer=$("$CLIENT" --status | sed -n 's/.*run directory //p')
[ -n "$outer" ] || { echo 'no running bridge to test with' >&2; rm -rf "$scratch"; exit 1; }
boot() { TERMUX_RUN_DIR=$outer timeout -k 2 40 "$CLIENT" "$@"; }

cleanup() {
  if [ -n "$name" ]; then
    boot -n -c '
dir="$TMPDIR/$1"
kill -TERM "$(cat "$dir/run/pid")" 2>/dev/null
for i in $(seq 150); do [ -e "$dir/run/pid" ] || break; sleep 0.1; done
[ -s "$dir/service.log" ] && { echo "--- bridge log ---"; tail -n 20 "$dir/service.log"; }
rm -rf "$dir"' _ "$name"
  fi
  rm -rf "$scratch"
}
trap cleanup EXIT

boot -n true || { echo "run this from a directory the bridge shares with the host, not $SHARED" >&2; exit 1; }

if [ "$LIVE" = 1 ]; then
  export TERMUX_RUN_DIR=$outer
else
  # The name is short for the sake of the socket path (107 bytes).
  name=$(boot -c '
set -e
dir=$(mktemp -d "$TMPDIR/ct-XXXXXX")
cat > "$dir/bridge.py"
printf "%s\n" "$1" > "$dir/shared-dirs"
cd "$dir"
setsid python3 bridge.py >service.log 2>&1 </dev/null &
for i in $(seq 100); do [ -s run/pid ] && break; sleep 0.05; done
[ -s run/pid ] && basename "$dir"' _ "$SHARED" < "$BRIDGE")
  case "$name" in
    ct-??????) ;;
    *) echo "cannot start the test bridge: $name" >&2; name=; exit 1;;
  esac
  export TERMUX_RUN_DIR=$outer/tmp/$name/run
fi

ok() { pass=$((pass + 1)); echo "PASS $1"; }
no() { fail=$((fail + 1)); echo "FAIL $1 :: $2"; }
expect() {
  if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "got [$2], want [$3]"; fi
}
T() { timeout -k 2 20 "$CLIENT" "$@"; }
ms() { echo $(( ($(date +%s%N) - $1) / 1000000 )); }
hex() { od -An -tx1 | tr -d ' \n'; }
# Two characters of three bytes each, for arguments that are not ASCII.
wide=$'\xe4\xb8\xad\xe6\x96\x87'

# --- exit status
T true; expect 'exit 0' $? 0
T -c 'exit 42'; expect 'exit status passes through' $? 42
T -c 'kill -TERM $$'; expect 'death by signal -> 128+N' $? 143
err=$(T definitely-not-a-command 2>&1)
expect 'command not found -> 127' "$?|$err" '127|termux-run: Command not found: definitely-not-a-command'
T /system/etc/hosts 2>/dev/null; expect 'not executable -> 126' $? 126
T -t 1 sleep 10 2>/dev/null; expect 'timeout -> 124' $? 124
T --timeout=1 sleep 10 2>/dev/null; expect '--timeout=N form' $? 124

# --- options
T --bogus true 2>/dev/null; expect 'unknown option -> 2' $? 2
T 2>/dev/null; expect 'missing command -> 2' $? 2
T -M true 2>/dev/null; expect '-M without --root -> 2' $? 2
T --help | grep -q '^Usage:'; expect '--help' $? 0
out=$(T -- echo --not-an-option); expect '-- ends the options' "$out" '--not-an-option'

# --- the bridge itself
out=$(T --status); code=$?
case "$code|$out" in
  "0|"*" $TERMUX_RUN_DIR") ok '--status names the bridge it found';;
  *) no '--status names the bridge it found' "$code|$out";;
esac
T --status true 2>/dev/null; expect '--status takes no command' $? 2
err=$(TERMUX_RUN_DIR=$scratch/none T true 2>&1); code=$?
case "$code|$err" in
  '125|'*'termux-proot-bridge start'*) ok 'no bridge -> 125 and how to start one';;
  *) no 'no bridge -> 125 and how to start one' "$code|$err";;
esac
mkdir "$scratch/stale"; echo stale > "$scratch/stale/token"
err=$(TERMUX_RUN_DIR=$scratch/stale T --status 2>&1); code=$?
case "$code|$err" in
  '125|termux-run: The bridge is not running'*) ok 'files of a bridge that is gone -> 125, not running';;
  *) no 'files of a bridge that is gone -> 125, not running' "$code|$err";;
esac

# --- working directory
out=$(T pwd); expect 'default cwd is the caller'"'"'s' "$out" "$SHARED"
ln -s "$SHARED" "$scratch/link"
out=$(T -C "$scratch/link" pwd); expect 'cwd through a container-only symlink resolves' "$out" "$SHARED"
out=$(T -C . pwd); expect 'relative cwd' "$out" "$SHARED"
err=$(T -C "$scratch" true 2>&1); code=$?
case "$code|$err" in
  '125|termux-run: Working directory'*'-C'*) ok 'container-only cwd -> 125 with a reason and a way out';;
  *) no 'container-only cwd -> 125 with a reason and a way out' "$code|$err";;
esac
T -C /no/such/dir true 2>/dev/null; expect 'missing cwd -> 125' $? 125
T -C /system true 2>/dev/null; expect 'cwd outside the shared directories -> 125' $? 125

# --- arguments
out=$(T printf '[%s]' 'a b' "it's" '$HOME' '' "$wide" '*')
expect 'arguments arrive verbatim' "$out" "[a b][it's][\$HOME][][$wide][*]"
meta='[;id][$(id)][`id`][&& id][a\b]["q"][>x]'
out=$(T printf '[%s]' ';id' '$(id)' '`id`' '&& id' 'a\b' '"q"' '>x' $'tab\there' $'two\nlines')
expect 'shell metacharacters stay plain text' "$out" "$meta"$'[tab\there][two\nlines]'
out=$(T -c 'echo "$1|$2"' _ 'a b' c); expect '-c with positional parameters' "$out" 'a b|c'
out=$(T printf '%s|' $'caf\xe9' $'\xff\xfeA' $'\xe4\xb8\xad\xff\xe6\x96\x87' '' | hex)
expect 'argument bytes that are not UTF-8 arrive unchanged' "$out" '636166e97cfffe417ce4b8adffe696877c7c'
big=$(head -c 100000 /dev/zero | tr '\0' x)
out=$(T -c 'printf %s "$1" | wc -c' _ "$big"); expect '100 kB argument' "$out" 100000
err=$(T true "$big" "$big" "$big" "$big" "$big" "$big" "$big" "$big" "$big" "$big" "$big" 2>&1)
expect 'oversized request -> 125' "$?|$err" '125|termux-run: Request too large (limit 1 MiB)'

# --- standard input
out=$(printf 'a\nb\n' | T cat); expect 'pipe reaches the command' "$out" $'a\nb'
printf 'from file\n' > "$scratch/in"
out=$(T cat < "$scratch/in"); expect 'container-only file reaches the command' "$out" 'from file'
out=$(T cat <<EOF
here $((1 + 1))
EOF
)
expect 'here-document reaches the command' "$out" 'here 2'
out=$(echo dropped | T -n cat); expect '-n detaches standard input' "$out" ''
out=$(printf '1\n2\n3\n' | while read -r l; do T echo "got $l"; done | tr '\n' ' ')
expect 'while-read loop over a pipe keeps its input' "$out" 'got 1 got 2 got 3 '
printf '1\n2\n3\n' > "$scratch/lines"
out=$(while read -r l; do T echo "got $l"; done < "$scratch/lines" | tr '\n' ' ')
expect 'while-read loop over a file keeps its input' "$out" 'got 1 got 2 got 3 '
out=$(printf 'l1\nl2\n' | { T -c 'read -r x; echo "$x"'; cat; } | tr '\n' ' ')
expect 'command and caller share one pipe' "$out" 'l1 l2 '
# A regular file has an offset: what the command reads has to move it for the
# caller too, as it would for a command started right here.
out=$({ T -c 'read -r x; echo "$x"'; cat; } < "$scratch/lines" | tr '\n' ' ')
expect 'command and caller share one file offset' "$out" '1 2 3 '
out=$({ T -c 'read -r a; echo "a=$a"'; T -c 'read -r b; echo "b=$b"'; } < "$scratch/lines" | tr '\n' ' ')
expect 'two commands read one file in turn' "$out" 'a=1 b=2 '
out=$(while read -r l; do echo "loop $l"; T -c 'cat >/dev/null'; done < "$scratch/lines" | tr '\n' ' ')
expect 'a command that drains the file ends the loop over it' "$out" 'loop 1 '

mkfifo "$scratch/fifo"
sleep 30 > "$scratch/fifo" &
holder=$!
start=$(date +%s%N); T true < "$scratch/fifo"; code=$?; took=$(ms "$start")
kill "$holder" 2>/dev/null
if [ "$code" = 0 ] && [ "$took" -lt 3000 ]; then ok 'input that never ends does not hold the client'
else no 'input that never ends does not hold the client' "exit $code after ${took}ms"; fi

# Node hands its children a socket for stdin; so do tools built on it.
start=$(date +%s%N)
out=$(timeout -k 2 20 node -e '
const child = require("child_process").spawn(process.argv[1], ["cat"], {stdio: ["pipe", "inherit", "inherit"]});
child.on("exit", code => process.exit(code));' "$CLIENT"); code=$?; took=$(ms "$start")
if [ "$code|$out" = '0|' ] && [ "$took" -lt 3000 ]; then ok 'socket input is left alone'
else no 'socket input is left alone' "exit $code, output [$out], ${took}ms"; fi

# --- output
T -c 'echo o1; echo e1 >&2; echo o2' > "$scratch/merged" 2>&1
expect 'shared stdout/stderr keeps the order' "$(tr '\n' ' ' < "$scratch/merged")" 'o1 e1 o2 '
out=$(T -c 'echo o1; echo e1 >&2; echo o2' 2> "$scratch/err")
expect 'separate stdout/stderr stay separate' "$out|$(cat "$scratch/err")" $'o1\no2|e1'
T seq 1 300000 2> "$scratch/err" | head -n 1 > "$scratch/out"; code=${PIPESTATUS[0]}
expect 'closed downstream -> quiet 141' "$code|$(cat "$scratch/out")|$(cat "$scratch/err")" '141|1|'
want=$(node -e 'process.stdout.write(Buffer.from(Array.from({length: 2048000}, (_, i) => (i * 31) & 255)))' | sha256sum)
got=$(T python3 -c 'import sys; sys.stdout.buffer.write(bytes((i * 31) & 255 for i in range(2048000)))' | sha256sum)
expect 'binary output survives the trip' "$got" "$want"

# A reader that stalls for longer than any send timeout: nothing may be lost,
# and what it has not taken must not pile up in the client.
mkfifo "$scratch/slow"
"$CLIENT" -t 60 head -c 150000000 /dev/zero > "$scratch/slow" & pid=$!
exec 9< "$scratch/slow"
sleep 6.5
rss=$(awk '/^VmRSS:/ {print int($2 / 1024)}' "/proc/$pid/status" 2>/dev/null)
got=$(wc -c <&9)
exec 9<&-
wait "$pid"
expect 'a reader that stalls gets everything once it reads again' "$?|$got" '0|150000000'
if [ "${rss:-999}" -lt 110 ]; then ok 'output for a stalled reader is not piled up in the client'
else no 'output for a stalled reader is not piled up in the client' "${rss:-?} MiB resident"; fi
echo "INFO client memory with a stalled reader: ${rss:-?} MiB"

start=$(date +%s%N); out=$(T -c 'sleep 30 & echo started' 2> "$scratch/err"); code=$?; took=$(ms "$start")
if [ "$code|$out" = '0|started' ] && [ "$took" -lt 4000 ] && grep -q 'background processes' "$scratch/err"; then
  ok 'background job: real exit status, a notice, no wait'
else no 'background job: real exit status, a notice, no wait' "exit $code, [$out], ${took}ms, $(cat "$scratch/err")"; fi

# --- signals and disconnects
"$CLIENT" -t 20 sleep 30 & pid=$!; sleep 1; kill -TERM "$pid"; wait "$pid"
expect 'SIGTERM is forwarded' $? 143
"$CLIENT" -t 20 sleep 30 & pid=$!; sleep 1; kill -INT "$pid"; wait "$pid"
expect 'SIGINT is forwarded' $? 130
"$CLIENT" -t 20 -c 'trap "" INT; sleep 30' 2>/dev/null & pid=$!; sleep 1; kill -INT "$pid"; wait "$pid"
expect 'ignored SIGINT -> killed, 130' $? 130

"$CLIENT" -t 30 -c 'echo $$; exec sleep 30' > "$scratch/pid" & pid=$!
for i in $(seq 50); do [ -s "$scratch/pid" ] && break; sleep 0.1; done
kill -KILL "$pid"; wait "$pid" 2>/dev/null
sleep 1
T -c 'kill -0 "$1" 2>/dev/null' _ "$(cat "$scratch/pid")"
expect 'killed client leaves nothing behind' $? 1

# Daemons that detach with setsid must outlive the call. The command has to
# stay until the child reached setsid(): one that returns at once loses the
# child to the group kill.
pid=$(T -c 'setsid sleep 30 >/dev/null 2>&1 </dev/null & echo $!; sleep 0.3')
sleep 1
T -c 'kill -0 "$1" 2>/dev/null && kill "$1"' _ "${pid:-0}"
expect 'setsid-detached process outlives the command' $? 0

# --- connection limit (would refuse the running bridge's real callers)
if [ "$LIVE" = 0 ]; then
  pids=()
  for i in $(seq 20); do T sleep 5 2> "$scratch/limit.$i" & pids+=($!); done
  ran=0; refused=0; other=
  for i in "${!pids[@]}"; do
    wait "${pids[$i]}"; code=$?
    case "$code|$(cat "$scratch/limit.$((i + 1))")" in
      '0|') ran=$((ran + 1));;
      '125|termux-run: Too many concurrent commands') refused=$((refused + 1));;
      *) other="$other [$code|$(cat "$scratch/limit.$((i + 1))")]";;
    esac
  done
  expect 'past the limit a call is refused with the reason' "$ran ran, $refused refused$other" '16 ran, 4 refused'
fi

# --- root
if [ "$ROOT" = 1 ]; then
  prefix=$(T -c 'echo "$PREFIX"')
  out=$(T --root id -u); expect '--root runs as root' "$out" 0
  out=$(T --root printf '[%s]' 'a b' "it's" '$HOME' '' "$wide")
  expect '--root keeps arguments verbatim' "$out" "[a b][it's][\$HOME][][$wide]"
  out=$(T --root printf '[%s]' ';id' '$(id)' '`id`' '&& id' 'a\b' '"q"' '>x')
  expect '--root: shell metacharacters stay plain text' "$out" "$meta"
  out=$(T --root printf '%s' $'\xff\xfeA' | hex)
  expect '--root keeps argument bytes that are not UTF-8' "$out" 'fffe41'
  out=$(T --root -c 'command -v am; command -v pkg')
  expect '--root: system tools first, Termux tools reachable' "$out" $'/system/bin/am\n'"$prefix/bin/pkg"
  if T --root -M true 2>/dev/null; then
    a=$(T --root readlink /proc/self/ns/mnt); b=$(T --root -M readlink /proc/self/ns/mnt)
    if [ -n "$a" ] && [ -n "$b" ] && [ "$a" != "$b" ]; then ok '-M switches the mount namespace'
    else no '-M switches the mount namespace' "$a / $b"; fi
  else
    echo 'SKIP -M switches the mount namespace :: this su does not take -M'
  fi
  out=$(echo piped | T --root cat); expect '--root command reads the pipe' "$out" piped
  out=$({ T --root -c 'read -r x; echo "$x"'; cat; } < "$scratch/lines" | tr '\n' ' ')
  expect '--root command and caller share one file offset' "$out" '1 2 3 '
  T --root -c 'exit 9'; expect '--root exit status passes through' $? 9
  start=$(date +%s%N); out=$(T --root -t 1 -c 'echo $$; exec sleep 30' 2>/dev/null); code=$?; took=$(ms "$start")
  if [ "$code" = 124 ] && [ "$took" -lt 3500 ]; then ok '--root timeout -> 124'
  else no '--root timeout -> 124' "exit $code after ${took}ms"; fi
  sleep 1
  T --root -c 'kill -0 "$1" 2>/dev/null' _ "${out:-0}"
  expect '--root timeout leaves no root process behind' $? 1
  pid=$(T --root -c 'setsid sleep 30 >/dev/null 2>&1 </dev/null & echo $!; sleep 0.3')
  sleep 1
  T --root -c 'kill -0 "$1" 2>/dev/null && kill "$1"' _ "${pid:-0}"
  expect '--root setsid-detached process outlives the command' $? 0
fi

start=$(date +%s%N)
for i in 1 2 3 4 5 6 7 8 9 10; do T true; done
echo "INFO per call: $(( $(ms "$start") / 10 )) ms"
start=$(date +%s%N); got=$(T head -c 50000000 /dev/zero | wc -c)
echo "INFO 50 MB of output: $(ms "$start") ms ($got bytes)"

echo
echo "$pass/$((pass + fail)) passed"
[ "$fail" = 0 ]
