#!/bin/sh
# Prints one version's section of the changelog, for its GitHub release:
#   scripts/release-notes.sh 0.2.0 CHANGELOG.md
#
# It fails on a version that's missing, empty, or still marked unreleased, so a release can't go out
# with notes nobody finished. Only a heading with a date counts.
set -eu

[ $# -eq 2 ] || {
	echo "Usage: scripts/release-notes.sh VERSION CHANGELOG" >&2
	exit 2
}
version=$1
changelog=$2

notes=$(awk -v heading="## $version (" '
	index($0, heading) == 1 && $0 ~ /\([0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]\)$/ { found = 1; next }
	found && /^## / { exit }
	found { print }
' "$changelog")

if [ -z "$(printf '%s' "$notes" | tr -d '[:space:]')" ]; then
	echo "$changelog has no dated section with notes for $version." >&2
	exit 1
fi
printf '%s\n' "$notes"
