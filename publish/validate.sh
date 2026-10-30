#!/usr/bin/env bash
# publish/validate.sh - everything this action alone can check, checked before
# anything is downloaded or the registry is logged into.
#
# A file rather than a `run:` block so a test can drive it over the whole input
# matrix -- see tests/publish-inputs.test.sh. Mirrors deploy/validate.sh.
#
# Deliberately NOT re-checking what the caller's helper checks for itself: the
# manifest's image IDs, the archive digest, HEAD matching the revision. Those
# run inside the helper and stay authoritative; this file refuses only what
# would otherwise fail AFTER a download or a registry login, on the way to the
# same refusal.
#
# Inputs (environment):
#   PROJECT         project slug
#   REVISION        full 40-hex commit being released
#   COMPONENTS      space-separated image components
#   REGISTRY_USER   ghcr.io username
#   REGISTRY_TOKEN  ghcr.io password (checked, never echoed)
#   ARTIFACT_NAME   Build artifact to fetch, empty when already on disk
#   HELPER          path to the vendored release helper
set -euo pipefail
# A literal dollar for printing workflow expressions inside double quotes --
# there, ${...} is parameter expansion and a syntax error, so deploy/validate.sh
# keeps the same trick. Single quotes would need no help but trip SC2016.
D='$'


# Metadata `required: true` does not reject a missing composite-action input,
# so required is checked at runtime too (see run-task/run.sh).
if [ -z "${PROJECT:-}" ]; then
	echo '::error::project is empty. Pass the slug the images publish under, e.g. cazper.'
	exit 1
fi
# The helper's own rule, checked early: ^[a-z][a-z0-9-]*$
if [[ ! "$PROJECT" =~ ^[a-z][a-z0-9-]*$ ]]; then
	echo "::error::project must be a lowercase slug: a leading letter, then letters, digits or hyphens; got '$PROJECT'."
	exit 1
fi

if [ -z "${REVISION:-}" ]; then
	echo "::error::revision is empty. Pass the full 40-hex commit SHA being released, normally ${D}{{ github.sha }}."
	exit 1
fi
# The helper tags images with this value verbatim and refuses anything that is
# not a full commit; a short SHA would otherwise surface as image tags nothing
# can address deterministically.
if [[ ! "$REVISION" =~ ^[a-f0-9]{40}$ ]]; then
	echo "::error::revision must be a full 40-character lowercase hex commit SHA; got '$REVISION'."
	exit 1
fi

# Split on whitespace without globbing -- read does not expand wildcards the
# way a bare `for x in $COMPONENTS` would against the files in the workspace.
# Whitespace-only is not a list (same call deploy/resolve.sh makes).
read -r -a component_words <<<"${COMPONENTS:-}"
if [ "${#component_words[@]}" -eq 0 ]; then
	echo '::error::components is empty. Pass the space-separated image components, e.g. "api db gate config".'
	exit 1
fi
seen=""
unknown=""
duplicate=""
for component in "${component_words[@]}"; do
	if [[ ! "$component" =~ ^[a-z][a-z0-9-]*$ ]]; then
		unknown="$unknown $component"
	fi
	case "$seen" in
		*" $component "*) duplicate="$duplicate $component" ;;
		# Appended with a trailing space too, so every stored word sits
		# between spaces and the pattern above can match the last one.
		*) seen="$seen $component " ;;
	esac
done
if [ -n "$unknown" ]; then
	echo "::error::unknown image component(s):$unknown. Components must be lowercase slugs."
	exit 1
fi
# The helper requires distinct components; a repeated one is usually a paste
# error and would push the same ref twice.
if [ -n "$duplicate" ]; then
	echo "::error::duplicate image component(s):$duplicate. A release needs distinct components."
	exit 1
fi

# The login always runs -- ghcr.io pushes are never anonymous -- so both halves
# of the credential are required, mirroring activate's pairing rule.
if [ -z "${REGISTRY_USER:-}" ]; then
	echo "::error::registry-user is empty. The ghcr.io login needs a username; pass ${D}{{ github.repository_owner }}."
	exit 1
fi
# Brackets are admitted for bot logins: a post-merge dispatch runs as
# github-actions[bot], and callers pass registry-user straight from
# github.actor. Bot logins are valid GitHub usernames, and the username is
# display/routing for a ghcr login with GITHUB_TOKEN -- the token is what
# authorizes. Brackets cannot close a single quote, so the injection guard is
# unchanged. Keep in step with the sibling guard in activate/action.yml.
case "$REGISTRY_USER" in
	*[!]A-Za-z0-9._@[-]*)
		echo "::error::registry-user must be letters, digits, dot, underscore, at-sign, brackets or hyphen; got '$REGISTRY_USER'."
		exit 1 ;;
esac
# Read WITHOUT being echoed, in every branch: GitHub substitutes an empty
# string for a secret that does not exist, so a name declared in the workflow
# and never created in the repository looks exactly like one that was supplied
# -- and would otherwise fail as a login error downstream of this step.
if [ -z "${REGISTRY_TOKEN:-}" ]; then
	echo "::error::registry-token is empty. Pass the run-scoped ${D}{{ github.token }} rather than a long-lived password."
	exit 1
fi

# Only the shape: the value travels to actions/download-artifact as an input,
# not through a shell, but a bad name fails there with less to go on.
case "${ARTIFACT_NAME:-}" in
	"") ;;
	*[!A-Za-z0-9._-]*)
		echo "::error::artifact-name must be letters, digits, dot, underscore or hyphen; got '$ARTIFACT_NAME'."
		exit 1 ;;
esac

if [ ! -f "${HELPER:-}" ]; then
	echo "::error::release helper '${HELPER:-}' is not a file in the workspace. The helper is the caller's vendored copy (scripts/engineering/helpers/release.py by default) -- did actions/checkout run at the revision being published?"
	exit 1
fi

# Old released helpers still work when no prefix is supplied. New callers must
# provide the namespace; it is passed as an argv value, never interpolated code.
if [ -n "${IMAGE_BASE:-}" ] && [[ ! "$IMAGE_BASE" =~ ^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9][a-z0-9/._-]*$ ]]; then
  echo '::error::image-base must name a registry repository prefix' >&2
  exit 1
fi
