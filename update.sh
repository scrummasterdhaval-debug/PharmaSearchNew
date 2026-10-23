#!/bin/sh
# Bring this server up to date with the branch it runs, and restart it.
#
#   ./update.sh
#
# Run from the project folder on the server. It needs that folder to be a
# git checkout; DEPLOY.md, "Updating", shows the one-time setup. Settings
# (secrets.env, .env) and the data volume are not in git and are untouched.
set -e
cd "$(dirname "$0")"

if [ ! -d .git ]; then
    echo "This folder is not a git checkout yet: see DEPLOY.md, 'Updating', for the one-time setup." >&2
    exit 1
fi

BRANCH="${PHARMASEARCH_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"

if ! git diff --quiet HEAD --; then
    echo "These files were changed on the server and would be overwritten:" >&2
    git status --short --untracked-files=no >&2
    echo "Put them back with:  git checkout -- <file>   then run ./update.sh again." >&2
    exit 1
fi

before=$(git rev-parse --short HEAD)
git fetch --quiet origin "$BRANCH"
git merge --ff-only --quiet "origin/$BRANCH"
after=$(git rev-parse --short HEAD)

if [ "$before" = "$after" ]; then
    echo "Already up to date ($after); rebuilding anyway."
else
    echo "Updated $before -> $after:"
    git log --oneline "$before..$after"
fi

docker compose up -d --build
docker compose ps
