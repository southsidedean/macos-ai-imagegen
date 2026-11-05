#!/bin/sh
# Real generation, start to finish, on Apple Silicon. CI never runs a real model, so this runs by hand
# before each release and for any change to an engine:
#   scripts/real-engine.sh
#
# It uses the real models folder (or MIA_MODELS), and downloads the default model if it's missing, about
# 4.6 GB. Images and history go in a throwaway folder, so your own history is left alone. It takes
# about three minutes. Every line it prints is safe to paste into a pull request: it shows no paths.
set -eu
cd "$(dirname "$0")/.."

# FLUX.2 klein 4B at its pinned commit, on mflux 0.24.0 and mlx 0.32.4, with this prompt and seed, at 1024
# by 1024 in 4 steps. A different start to the SHA-256 of the pixels means the output has changed. Moving
# either changes it (mlx 0.31.2 on mflux 0.18.1 gave 4eb1d7f9497d), so look at the new image before updating.
prompt="a red fox in a snowy forest at sunrise, photograph"
expected=9e30a7c54dc2

failures=0
pass() { echo "PASS  $1"; }
fail() {
	echo "FAIL  $1"
	failures=$((failures + 1))
}

uv sync --locked --extra dev --extra engine -q
mia=.venv/bin/mia

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export MIA_MODELS="${MIA_MODELS:-$HOME/Library/Application Support/mia/models}"
export MIA_HOME="$work/mia"

"$mia" models pull --yes >/dev/null
pass "the default model is ready"

# 1. The real thing: an image, at the settings every earlier test used.
first=$("$mia" generate "$prompt" --seed 42 2>"$work/first.log")
if [ -s "$first" ]; then pass "1. made an image ($(grep -o 'done in [0-9]* seconds' "$work/first.log"))"; else fail "1. no image"; fi
id=$(basename "$first" .png)
pixels=$(.venv/bin/python -c 'import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT pixels FROM images WHERE id = ?", (sys.argv[2],)).fetchone()[0])' "$MIA_HOME/history.sqlite3" "$id")
case "$pixels" in
"$expected"*) pass "2. the pixels are the known ones" ;;
*) fail "2. the pixels changed: expected $expected, got $(printf '%s' "$pixels" | cut -c1-12)" ;;
esac

# 3. No metadata: only the chunks a picture needs, and neither the prompt nor the home folder.
if .venv/bin/python - "$first" "$prompt" "$HOME" <<'EOF'
import struct, sys
data = open(sys.argv[1], "rb").read()
chunks, position = set(), 8
while position < len(data):
    length = struct.unpack(">I", data[position:position + 4])[0]
    chunks.add(data[position + 4:position + 8])
    position += 12 + length
sys.exit(bool(chunks - {b"IHDR", b"IDAT", b"IEND"}) or sys.argv[2].encode() in data or sys.argv[3].encode() in data)
EOF
then pass "3. the image holds no metadata"; else fail "3. the image holds metadata"; fi

# 4. The same settings again, in a new process.
if "$mia" generate --again "$id" >/dev/null 2>&1; then pass "4. made again, with identical pixels"; else fail "4. the remake's pixels differ"; fi

# 5. Stopping: a job told to stop keeps no image and records nothing. The delay lands in the steps.
before=$(.venv/bin/python -c 'import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT count(*) FROM images").fetchone()[0])' "$MIA_HOME/history.sqlite3")
"$mia" generate "$prompt" --seed 42 --output "$work/stopped.png" >/dev/null 2>"$work/stop.log" &
job=$!
sleep 15
requested=$(date +%s)
kill -TERM "$job"
status=0
wait "$job" || status=$?
took=$(($(date +%s) - requested))
after=$(.venv/bin/python -c 'import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT count(*) FROM images").fetchone()[0])' "$MIA_HOME/history.sqlite3")
if [ "$status" -eq 130 ] && [ ! -e "$work/stopped.png" ] && [ "$before" = "$after" ]; then
	pass "5. stopped ${took} seconds after asking, kept nothing"
else
	fail "5. stopping: exit $status, recorded $before then $after"
fi

if [ "$failures" -gt 0 ]; then
	echo "$failures check(s) failed."
	exit 1
fi
echo "All real-engine checks passed."
