#!/usr/bin/env bash
# Require every Caddy stage and the final shipping stage to use a version and digest.
set -euo pipefail

dockerfile="${1:?Pass an explicit Dockerfile}"

# Docker permits an optional --platform before the image and an optional
# `AS <name>` after it. Docker Hub's official image may also be written as
# caddy, library/caddy, or docker.io/library/caddy.
pinned_image_re='^(docker\.io/)?(library/)?caddy:[0-9]+\.[0-9]+\.[0-9]+-[a-zA-Z0-9][a-zA-Z0-9.-]*@sha256:[0-9a-f]{64}$'

fail() {
  echo "::error::$1"
  exit 1
}

[ -f "$dockerfile" ] || fail "$dockerfile does not exist."

# parse_from sets from_image and from_pinned for one complete FROM line.
parse_from() {
  local line="$1" i=1
  local -a words
  read -r -a words <<<"$line"
  [ "${words[0],,}" = "from" ] || return 1

  while [ "${words[$i]:-}" != "" ] && [[ "${words[$i]}" == --* ]]; do
    i=$((i + 1))
  done
  from_image="${words[$i]:-}"
  [ -n "$from_image" ] || fail "incomplete FROM in $dockerfile: '$line'."
  [ "$from_image" != "\\" ] && [ "$from_image" != '`' ] \
    || fail "multiline FROM is not allowed in $dockerfile: '$line'."
  [[ "$from_image" != *'$'* ]] \
    || fail "variable-expanded FROM images are not allowed in $dockerfile: '$line'."
  i=$((i + 1))

  from_pinned=0
  if [[ "$from_image" =~ $pinned_image_re ]]; then
    if [ "$i" -eq "${#words[@]}" ]; then
      from_pinned=1
    elif [ "$((i + 2))" -eq "${#words[@]}" ] \
      && [ "${words[$i],,}" = "as" ] \
      && [[ "${words[$((i + 1))]}" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
      from_pinned=1
    fi
  fi
}

is_caddy_image() {
  local image="$1" base
  base="${image##*/}"
  base="${base%%:*}"
  base="${base%%@*}"
  [ "$base" = "caddy" ]
}

from_lines=()
while IFS= read -r line; do
  read -r -a words <<<"$line"
  first_word="${words[0]:-}"
  if [ "${first_word,,}" = "from" ]; then
    from_lines+=("$line")
  fi
done < "$dockerfile"

# Rule 1: reject every unpinned Caddy FROM.
for from_line in "${from_lines[@]}"; do
  parse_from "$from_line"
  if is_caddy_image "$from_image" && [ "$from_pinned" -ne 1 ]; then
    fail "unpinned Caddy FROM in $dockerfile: '$from_line' -- must be caddy:<semver>-<variant>@sha256:<digest>."
  fi
done

# Rule 2: the final stage must be a pinned Caddy. This also rejects a
# Dockerfile with no Caddy FROM at all, since its last stage then cannot
# be one.
[ "${#from_lines[@]}" -gt 0 ] || fail "$dockerfile has no FROM line at all."
last_from="${from_lines[$((${#from_lines[@]} - 1))]}"
parse_from "$last_from"
is_caddy_image "$from_image" && [ "$from_pinned" -eq 1 ] \
  || fail "the final stage of $dockerfile is not a pinned Caddy image: '$last_from'."

echo "$dockerfile: every Caddy FROM is pinned and the final stage is the pinned Caddy."
