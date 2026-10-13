#!/bin/sh
# Prepare the data disk, then run the application as its unprivileged user.
#
# A persistent disk is attached over /data when the container starts, which
# hides the folders the image made there and leaves the disk owned by root.
# Started as root, this creates the folders, hands the disk to the
# application's user, and drops to that user before anything else runs.
set -e

DATA_DIR="${PHARMASEARCH_DATA_DIR:-/data}"
APP_UID=10001

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR/open_registers" "$DATA_DIR/exports" "$DATA_DIR/backups" "$DATA_DIR/logs"
    # The registers run to gigabytes, so the disk is walked only when its top
    # is not the application's yet: the first start on a new disk.
    if [ "$(stat -c %u "$DATA_DIR")" != "$APP_UID" ]; then
        chown -R "$APP_UID:$APP_UID" "$DATA_DIR"
    fi
    # Whatever sits at the top -- the folders above, the database -- may have
    # been made by root since: a backup that ran before the app, or a folder
    # created just now. Those few are handed over on every start.
    chown "$APP_UID:$APP_UID" "$DATA_DIR" "$DATA_DIR"/* 2>/dev/null || true
    export HOME=/home/pharmasearch
    exec setpriv --reuid="$APP_UID" --regid="$APP_UID" --init-groups "$@"
fi

mkdir -p "$DATA_DIR/open_registers" "$DATA_DIR/exports" 2>/dev/null || true
exec "$@"
