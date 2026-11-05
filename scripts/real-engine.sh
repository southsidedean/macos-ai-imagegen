#!/bin/sh
# Real generation, start to finish, on Apple Silicon. CI never runs a real model, so this runs by hand
# before each release and for any change to an engine:
#   scripts/real-engine.sh
#
# It uses the real models folder (or MIA_MODELS), and downloads the default model if it's missing, about
# 4.6 GB. Images, history, and the test's own server go in a throwaway folder and on a spare port, so your
# own history and any mia serve you have running are left alone, though it can't share the GPU with one.
# It also installs and removes a real launchd agent, under a label of its own. It takes about seven minutes. Every line it prints is safe to paste into a pull request: it shows no paths.
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
server=""
agent=""
cleanup() {
	if [ -n "$server" ]; then
		kill -TERM "$server" 2>/dev/null || true
		wait "$server" 2>/dev/null || true
	fi
	if [ -n "$agent" ]; then
		"$mia" service uninstall >/dev/null 2>&1 || true
	fi
	rm -rf "$work"
}
trap cleanup EXIT
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

# The API, with a real model in a real worker: what 0.3 promises.
port=18765
token=$("$mia" token create --for real-engine 2>/dev/null)
"$mia" serve --port "$port" 2>"$work/serve.log" &
server=$!
base="http://127.0.0.1:$port"
call() { # METHOD PATH [BODY]: prints the answer's body.
	if [ $# -eq 3 ]; then
		curl -fsS -X "$1" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -d "$3" "$base$2"
	else
		curl -fsS -X "$1" -H "Authorization: Bearer $token" "$base$2"
	fi
}
field() { # NAME: reads one field of the JSON on standard input.
	.venv/bin/python -c 'import json, sys; print(json.load(sys.stdin).get(sys.argv[1]))' "$1"
}
state_of() { call GET "/v1/jobs/$1" | field state; }
wait_until_finished() { # JOB: prints its final state, or "timeout" after five minutes.
	tries=0
	while [ "$tries" -lt 600 ]; do
		now=$(state_of "$1" || echo unknown)
		case "$now" in done | failed | cancelled)
			echo "$now"
			return
			;;
		esac
		tries=$((tries + 1))
		sleep 0.5
	done
	echo timeout
}
tries=0
until curl -fsS "$base/v1/health" >/dev/null 2>&1; do
	tries=$((tries + 1))
	if [ "$tries" -gt 60 ]; then
		fail "6. the server didn't start"
		echo "$failures check(s) failed."
		exit 1
	fi
	sleep 0.5
done

body=$(.venv/bin/python -c 'import json, sys; print(json.dumps({"prompt": sys.argv[1], "seed": 42}))' "$prompt")
job=$(call POST /v1/jobs "$body" | field id)
result=$(wait_until_finished "$job")
image=$(call GET "/v1/jobs/$job" | field image)
pixels=$(call GET "/v1/images/$image" | field pixels)
call GET "/v1/images/$image/file" >"$work/from-api.png"
case "$result $pixels" in
"done $expected"*) pass "6. a job through the API, in a worker, gave the known pixels" ;;
*) fail "6. a job through the API: $result, pixels $(printf '%s' "$pixels" | cut -c1-12)" ;;
esac
if [ -s "$work/from-api.png" ]; then pass "7. the image downloaded through the API"; else fail "7. no image from the API"; fi
call GET "/v1/images/$image/preview" >"$work/preview.jpg"
# The real engine's preview, which the fake's can't stand in for: a JPEG, small, and with no metadata.
if .venv/bin/python - "$work/preview.jpg" <<'CHECK'
import sys
from PIL import Image

with Image.open(sys.argv[1]) as preview:
    assert preview.format == "JPEG" and max(preview.size) == 512
    assert not preview.getexif() and not {"exif", "xmp", "comment"} & set(preview.info)
CHECK
then pass "7b. the preview is a small JPEG with no metadata ($(wc -c <"$work/preview.jpg" | tr -d ' ') bytes)"; else fail "7b. the preview is wrong"; fi

# Kill the worker while it holds a loaded model and is partway through a job.
long=$(call POST /v1/jobs "$body" | field id)
sleep 12
victim=$(cat "$MIA_HOME/gpu.lock")
kill -KILL "$victim"
result=$(wait_until_finished "$long")
reason=$(call GET "/v1/jobs/$long" | field error)
if [ "$result" = failed ] && [ "$reason" = "the worker stopped while this job was running" ] && curl -fsS "$base/v1/health" >/dev/null; then
	pass "8. killing the worker mid-job left the API up, and recorded why the job failed"
else
	fail "8. after killing the worker: $result"
fi
next=$(call POST /v1/jobs '{"prompt": "a small test", "seed": 1, "width": 512, "height": 512, "steps": 2}' | field id)
result=$(wait_until_finished "$next")
replacement=$(cat "$MIA_HOME/gpu.lock")
if [ "$result" = "done" ] && [ "$replacement" != "$victim" ]; then
	pass "9. a new worker took the next job"
else
	fail "9. the next job: $result"
fi

# Cancel partway, through the API.
stopping=$(call POST /v1/jobs "$body" | field id)
sleep 8
requested=$(date +%s)
call DELETE "/v1/jobs/$stopping" >/dev/null
result=$(wait_until_finished "$stopping")
took=$(($(date +%s) - requested))
kept=$(call GET "/v1/jobs/$stopping" | field image)
leftovers=$(find "$MIA_HOME/images" -name '.*partial*' | wc -l | tr -d ' ')
if [ "$result" = cancelled ] && [ "$kept" = None ] && [ "$leftovers" = 0 ]; then
	pass "10. cancelled ${took} seconds after asking, and kept nothing"
else
	fail "10. cancelling: $result, image $kept, $leftovers partial files"
fi

# Stopping mid-job, with another waiting. mflux's step loop catches the interrupt itself, and treats it as the
# end of the job, so this is the one place only the real engine can prove the worker still stops.
running=$(call POST /v1/jobs "$body" | field id)
waiting=$(call POST /v1/jobs '{"prompt": "a small test", "seed": 1, "width": 512, "height": 512, "steps": 2}' | field id)
sleep 12
worker=$(cat "$MIA_HOME/gpu.lock")
requested=$(date +%s)
kill -TERM "$server"
wait "$server" 2>/dev/null || true
server=""
took=$(($(date +%s) - requested))
states=$(.venv/bin/python -c 'import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); print(*[c.execute("SELECT state FROM jobs WHERE id = ?", (i,)).fetchone()[0] for i in sys.argv[2:]])' "$MIA_HOME/history.sqlite3" "$running" "$waiting")
if ! kill -0 "$worker" 2>/dev/null && [ "$states" = "failed queued" ]; then
	pass "11. stopped mid-job in ${took} seconds: that job failed, and the next stayed queued"
else
	fail "11. stopping mid-job: after ${took} seconds, states $states"
fi

# And the queue survives: a new server's worker runs what was waiting.
"$mia" serve --port "$port" 2>>"$work/serve.log" &
server=$!
tries=0
until curl -fsS "$base/v1/health" >/dev/null 2>&1 || [ "$tries" -gt 60 ]; do
	tries=$((tries + 1))
	sleep 0.5
done
result=$(wait_until_finished "$waiting")
if [ "$result" = "done" ]; then pass "12. a new server ran the job left in the queue"; else fail "12. the waiting job: $result"; fi
kill -TERM "$server"
wait "$server" 2>/dev/null || true
server=""

# The real launchd agent, under a label of its own, so one you've installed is left alone.
export MIA_SERVICE_LABEL=local.mia.real-engine-test
agent_file="$HOME/Library/LaunchAgents/$MIA_SERVICE_LABEL.plist"
answering() {
	tries=0
	until curl -fsS "$base/v1/health" >/dev/null 2>&1; do
		tries=$((tries + 1))
		if [ "$tries" -gt 90 ]; then return 1; fi
		sleep 0.5
	done
}
agent=yes
"$mia" service install --port "$port" >/dev/null
if answering; then pass "13. launchd started the service"; else fail "13. the service didn't start"; fi

# A job in that agent gives the same pixels, which proves it has the GPU, and it keeps the Mac awake only for the job.
job=$(call POST /v1/jobs "$body" | field id)
tries=0
until [ "$(state_of "$job")" = "running" ] || [ "$tries" -gt 240 ]; do
	tries=$((tries + 1))
	sleep 0.5
done
held=no
if pmset -g assertions | grep -q 'caffeinate'; then held=yes; fi
result=$(wait_until_finished "$job")
pixels=$(call GET "/v1/images/$(call GET "/v1/jobs/$job" | field image)" | field pixels)
case "$result $pixels" in
"done $expected"*) pass "14. a job under launchd gave the known pixels" ;;
*) fail "14. a job under launchd: $result, pixels $(printf '%s' "$pixels" | cut -c1-12)" ;;
esac
sleep 1
if [ "$held" = yes ] && ! pgrep -f "caffeinate -i -w $(cat "$MIA_HOME/gpu.lock")" >/dev/null; then
	pass "15. the Mac was kept awake during the job, and not after it"
else
	fail "15. keeping the Mac awake: held during the job: $held"
fi

# launchd brings back a server that dies, whose worker then picks up the queue.
before=$("$mia" service status | sed -n 's/.*process \([0-9]*\).*/\1/p')
kill -KILL "$before"
sleep 1
tries=0
now=""
until { now=$("$mia" service status | sed -n 's/.*process \([0-9]*\), answering/\1/p') && [ -n "$now" ] && [ "$now" != "$before" ]; } || [ "$tries" -gt 60 ]; do
	tries=$((tries + 1))
	sleep 0.5
	now=""
done
job=$(call POST /v1/jobs "$body" | field id)
result=$(wait_until_finished "$job")
if [ -n "$now" ] && [ "$result" = "done" ]; then
	pass "16. launchd brought back a killed server, and it ran a job"
else
	fail "16. after killing the server: process '$now', job $result"
fi

"$mia" service uninstall >/dev/null
agent=""
sleep 1
if [ ! -e "$agent_file" ] && ! curl -fsS "$base/v1/health" >/dev/null 2>&1 && ! pgrep -f "mia serve --port $port" >/dev/null; then
	pass "17. uninstalling stopped it and left nothing behind"
else
	fail "17. something is left after uninstalling"
fi

if [ "$failures" -gt 0 ]; then
	echo "$failures check(s) failed."
	exit 1
fi
echo "All real-engine checks passed."
