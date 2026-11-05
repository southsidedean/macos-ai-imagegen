#!/bin/sh
# Every check CI runs: the shell scripts, the Python's lint, security, and type checks, the
# workflows, Renovate's settings, the lockfile, known vulnerabilities, and the tests. The pre-push
# hook runs it; run it yourself any time:
#   scripts/check.sh
#
# Needs uv, plus shellcheck, actionlint, and npx. A tool that isn't installed here is skipped with
# a note; CI=true turns a missing tool into a failure instead. CI itself runs the same checks as
# separate steps, in .github/workflows/tests.yml.
set -eu
cd "$(dirname "$0")/.."

failures=0
pass() { echo "PASS  $1"; }
fail() {
	echo "FAIL  $1"
	failures=$((failures + 1))
}
missing() {
	if [ "${CI:-}" = true ]; then
		fail "$1 (not installed)"
	else
		echo "SKIP  $1: not installed here; CI runs it"
	fi
}

if ! command -v uv >/dev/null 2>&1; then
	echo "FAIL  uv isn't installed, and the other checks need it: brew install uv" >&2
	exit 1
fi

# 1. The lockfile matches pyproject.toml, and the environment matches the lockfile.
if uv lock --check -q && uv sync --locked --extra dev --extra engine -q; then
	pass "1. uv.lock is current"
else
	fail "1. uv.lock (above); uv lock updates it"
	echo "$failures check(s) failed."
	exit 1
fi

# 2. The shell scripts are POSIX sh and shellcheck clean.
if command -v shellcheck >/dev/null 2>&1; then
	if shellcheck -s sh scripts/*.sh .githooks/*; then
		pass "2. the shell scripts are shellcheck clean"
	else
		fail "2. shellcheck (above)"
	fi
else
	missing "2. shell scripts (shellcheck)"
fi

# 3. ruff: bugs and risky code, with the rules in pyproject.toml.
if uv run --no-sync ruff check src tests; then pass "3. ruff"; else fail "3. ruff (above)"; fi

# 4. bandit: security problems in the package's own code.
if uv run --no-sync bandit -q -c pyproject.toml -r src; then pass "4. bandit"; else fail "4. bandit (above)"; fi

# 5. mypy, strict: types, for the package and the tests.
if uv run --no-sync mypy; then pass "5. mypy"; else fail "5. mypy (above)"; fi

# 6. zizmor: the workflows' security (pinned actions, permissions, injection).
if uv run --no-sync zizmor --offline --min-severity medium .github/workflows; then
	pass "6. zizmor"
else
	fail "6. zizmor (above)"
fi

# 7. actionlint: mistakes in the workflows that zizmor doesn't look for.
if command -v actionlint >/dev/null 2>&1; then
	if actionlint; then pass "7. actionlint"; else fail "7. actionlint (above)"; fi
else
	missing "7. actionlint"
fi

# 8. Renovate's settings are valid. The version is pinned, so the check doesn't change under us;
# Renovate itself proposes moving it.
if command -v npx >/dev/null 2>&1; then
	if npx --yes --package renovate@44.155.1 -- renovate-config-validator --strict >/dev/null 2>&1; then
		pass "8. renovate.json5 is valid"
	else
		fail "8. renovate.json5: npx --package renovate@44.155.1 -- renovate-config-validator --strict"
	fi
else
	missing "8. Renovate's settings (npx)"
fi

# 9. No locked dependency has a known vulnerability. The export includes the hashes, so pip-audit
# checks exactly what uv.lock pins.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if uv export --locked --all-extras --no-emit-project --format requirements-txt -q -o "$work/requirements.txt" &&
	uv run --no-sync pip-audit --disable-pip --require-hashes --progress-spinner off -r "$work/requirements.txt"; then
	pass "9. pip-audit"
else
	fail "9. pip-audit (above)"
fi

# 10. The tests: privacy, the repository's rules, the git hooks, and every part's own tests.
if uv run --no-sync pytest -q; then pass "10. the tests"; else fail "10. the tests (above)"; fi

if [ "$failures" -gt 0 ]; then
	echo "$failures check(s) failed."
	exit 1
fi
echo "All checks passed."
