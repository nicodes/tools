#!/bin/sh
# scripts/release.sh - cut a cicd release with the helper checkouts pinned.
#
#   sh scripts/release.sh prepare v0.1.0 <full-current-main-sha>
#   sh scripts/release.sh prepare v0.1.0 <sha> --push
#
# Makes ONE deterministic commit on a release/vX.Y.Z branch and prints the PR
# command. It never pushes without --push, never tags, and never touches the
# working tree's current branch.
#
# --- why this exists ------------------------------------------------------
#
# This repository is consumed two ways at once, and until now neither had a
# release to name.
#
#   1. Products call the reusable workflows:
#        uses: nicodes/cicd/.github/workflows/backup.yml@<sha>
#   2. Products vendor helpers/ and tests/ into scripts/engineering, recorded
#      in SOURCE.json with a revision and a hash per file.
#
# And there is a third, which is the one this script is really about. Those
# reusable workflows check out cicd AGAIN, by a SHA written inside the file:
#
#        repository: nicodes/cicd
#        ref: b90d1ae8150cfb44be1e90dc251a68c80dc1bed2
#
# GitHub resolves the caller's ref for backup.yml ALONE. The helpers that
# workflow then runs come from whatever ref is written in it. So a consumer
# who carefully pinned backup.yml by SHA pinned one file and left the helpers
# floating on a different, older commit -- the pin looked complete and was
# not, which is worse than not pinning, because it is the difference between
# a risk you have accepted and one you think you have closed.
#
# On main today those inner pins are 24 and 34 commits behind, at two
# different revisions, while products vendor a third.
#
# --- why a release rather than the bump-along rule ------------------------
#
# The README documents the current answer: "The PR changing the helper cannot
# name its own merge SHA; the immediately following PR bumps both ref: pins to
# the previous merge commit." That is correct, and it is a chicken-and-egg
# worked around by hand, every time, forever. It has already failed in
# production once -- a stale pin ran an older portfolio identity check and
# rejected a new adopter at the Report-failure step.
#
# A release breaks the cycle by making the rewrite mechanical and reviewed:
# the candidate commit pins the helper checkouts to the SOURCE commit it was
# cut from, which is a real, already-merged, already-tested revision. Nobody
# has to remember a follow-up PR, and the pins cannot be 34 commits stale
# without somebody having cut a release that says so.
#
# --- why an immutable tag -------------------------------------------------
#
# So consumers have something to name. A 40-hex SHA off main is not a
# release: there is no changelog, no review boundary, and no way to say which
# version a product is on -- which is how eight products ended up spread
# across five revisions of this repository with nothing reporting it.
#
# Each release is its own tag and no tag ever moves. A SHA pin still works and
# is still stronger; the tag is what makes the SHA legible.
set -eu

usage() {
	cat >&2 <<'USAGE'
usage: release.sh prepare <version> <source-sha> [--push]

  version     an unused vX.Y.Z
  source-sha  the full 40-hex commit this release is cut from; must be the
              current tip of origin/main
  --push      push the candidate branch (default: print, push nothing)

Publication is a separate, manual stage -- see https://github.com/nicodes/docs/blob/main/tools/history/import-2026-10-05/docs/releases.md.
USAGE
	exit 2
}

[ "${1:-}" = prepare ] || usage
version="${2:-}"
source_sha="${3:-}"
push="${4:-}"
case "$version" in v[0-9]*.[0-9]*.[0-9]*) ;; *) usage ;; esac
case "$source_sha" in *[!0-9a-f]* | "") usage ;; esac
[ "${#source_sha}" -eq 40 ] || usage
[ -z "$push" ] || [ "$push" = --push ] || usage

# The inputs must be exact, so that a retry with the same inputs produces the
# same commit. A version that already exists, or a source that is not current
# main, would each produce a release nobody can reproduce.
git fetch --quiet origin main '+refs/tags/*:refs/tags/*'
if git rev-parse -q --verify "refs/tags/$version" >/dev/null; then
	echo "release.sh: $version already exists -- tags are never moved" >&2
	exit 1
fi
tip="$(git rev-parse origin/main)"
if [ "$tip" != "$source_sha" ]; then
	echo "release.sh: source $source_sha is not the tip of origin/main ($tip)" >&2
	exit 1
fi

branch="release/$version"
if git rev-parse -q --verify "refs/heads/$branch" >/dev/null; then
	echo "release.sh: $branch exists locally; delete it or choose another version" >&2
	exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git worktree add --quiet --detach "$work" "$source_sha"

# Rewrite every self-checkout pin to the source commit.
#
# Matched by the two lines together -- `repository: nicodes/cicd` followed by
# its `ref:` -- rather than by `ref:` alone. A workflow may check out some
# OTHER repository at a pinned ref, and rewriting that to a cicd commit would
# be both wrong and hard to notice.
rewritten=0
for file in "$work"/.github/workflows/*.yml; do
	[ -f "$file" ] || continue
	before="$(sha256sum "$file" | cut -d' ' -f1)"
	awk -v sha="$source_sha" '
		/^[[:space:]]*repository:[[:space:]]*nicodes\/(cicd|tools)[[:space:]]*$/ { seen = 1 }
		seen && /^[[:space:]]*ref:[[:space:]]*[0-9a-f]{40}([[:space:]]|$)/ {
			match($0, /^[[:space:]]*ref:[[:space:]]*/)
			prefix = substr($0, 1, RLENGTH)
			rest = substr($0, RLENGTH + 41)
			print prefix sha rest
			seen = 0
			next
		}
		{ print }
	' "$file" > "$file.tmp"
	mv -f "$file.tmp" "$file"
	[ "$(sha256sum "$file" | cut -d' ' -f1)" = "$before" ] || rewritten=$((rewritten + 1))
done

# Zero rewrites is not success. Either the self-checkouts are gone -- in which
# case this script has outlived its reason and should be deleted rather than
# quietly doing nothing -- or the pattern stopped matching, which is the
# failure that ships a release claiming a pin it did not write.
if [ "$rewritten" -eq 0 ]; then
	echo "release.sh: no helper checkout pins were rewritten. Either the" >&2
	echo "            self-checkouts were removed (delete this script) or the" >&2
	echo "            pattern no longer matches (fix it) -- not a release." >&2
	exit 1
fi

# Verify with the same two-line rule, not by grepping ref: alone. A pinned
# checkout of ANOTHER repository is legitimate and must survive; a first
# version of this guard rejected one, which the tests caught.
survivors="$(
	for file in "$work"/.github/workflows/*.yml; do
		[ -f "$file" ] || continue
		awk -v sha="$source_sha" -v name="$file" '
			/^[[:space:]]*repository:[[:space:]]*nicodes\/(cicd|tools)[[:space:]]*$/ { seen = 1; next }
			seen && /^[[:space:]]*ref:[[:space:]]*[0-9a-f]{40}([[:space:]]|$)/ {
				if ($2 != sha) print name ":" NR ": " $0
				seen = 0
				next
			}
			/^[[:space:]]*(uses|with|path|persist-credentials|sparse-checkout):/ { next }
			{ seen = 0 }
		' "$file"
	done
)"
if [ -n "$survivors" ]; then
	echo "release.sh: a cicd self-checkout pin survived the rewrite -- refusing" >&2
	printf '%s\n' "$survivors" >&2
	exit 1
fi

git -C "$work" checkout --quiet -b "$branch"
git -C "$work" add -A .github/workflows
git -C "$work" -c user.name='cicd release' -c user.email='noreply@nicodes.dev' \
	commit --quiet -m "release $version: pin helper checkouts to $source_sha

Cut from $source_sha. The reusable workflows check this repository out again
for helpers/, and GitHub resolves only the caller's ref -- so those inner
pins are rewritten here to the source commit, mechanically, instead of by the
follow-up PR the bump-along rule used to require."

candidate="$(git -C "$work" rev-parse HEAD)"
tree="$(git -C "$work" rev-parse "HEAD^{tree}")"
echo "Candidate $candidate; source $source_sha; tree $tree; $rewritten file(s) repinned"

if [ "$push" = --push ]; then
	git -C "$work" push --quiet origin "$branch:$branch"
	echo "Pushed $branch"
fi
git worktree remove --force "$work"
trap - EXIT

cat <<EOF
gh pr create --repo nicodes/tools --base main --head $branch \\
  --title 'release $version' \\
  --body 'Pin helper checkouts to $source_sha.'
EOF
