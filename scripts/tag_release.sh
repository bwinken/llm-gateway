#!/usr/bin/env bash
# Tag the commit that is checked out with the release it is: v<version>, the
# `version` of pyproject.toml. Run by CI on main, after the tests have
# passed (.github/workflows/ci.yml, job "Tag the release").
#
# It tags; it never changes the version. The version is raised by hand in
# the pull request (pyproject.toml, then `uv lock`). A merge that did not
# raise it finds its tag already there: nothing is tagged, and the run says
# so. A tag is never moved or replaced.
#
#   bash scripts/tag_release.sh             # tag HEAD and push the tag to origin
#   bash scripts/tag_release.sh --dry-run   # say what it would do, change nothing
#
# REMOTE (default origin) names the remote the tag is looked up on and
# pushed to.
set -euo pipefail

dry_run=0
[[ "${1:-}" == "--dry-run" ]] && dry_run=1
remote="${REMOTE:-origin}"

cd "$(git rev-parse --show-toplevel)"

version="$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "::error::pyproject.toml's version is '${version}', not MAJOR.MINOR.PATCH: nothing tagged." >&2
    exit 1
fi
tag="v${version}"
commit="$(git rev-parse HEAD)"

# Asked of the remote, not of the local clone: a CI checkout has no tags.
existing="$(git ls-remote --tags "$remote" "refs/tags/${tag}" "refs/tags/${tag}^{}" | tail -n 1 | cut -f 1)"
if [[ -n "$existing" ]]; then
    if [[ "$existing" == "$commit" ]]; then
        echo "${tag} is already this commit: nothing to do."
    else
        echo "::warning::${tag} already exists, at ${existing:0:7}. This merge did not raise the version in pyproject.toml, so it has no tag of its own."
    fi
    exit 0
fi

if (( dry_run )); then
    echo "Would tag ${commit:0:7} as ${tag} and push it to ${remote}."
    exit 0
fi

# An annotated tag needs an author. On a runner there is none configured.
if ! git config user.name >/dev/null; then
    git config user.name "github-actions[bot]"
    git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
fi
git tag -a "$tag" -m "Release ${tag}" "$commit"
git push "$remote" "refs/tags/${tag}"
echo "Tagged ${commit:0:7} as ${tag}."
