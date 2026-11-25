#!/bin/sh
# Runs the app as PUID:PGID (Unraid default 99:100 = nobody:users).
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
umask "${UMASK:-002}"

fail() {
    echo "" >&2
    echo "ERROR: $*" >&2
    echo "" >&2
    exit 1
}

is_mounted() {
    awk -v m="$1" '$5 == m { found = 1 } END { exit !found }' /proc/self/mountinfo
}

[ -n "$GAMELIB_PASSWORD" ] || fail "GAMELIB_PASSWORD is not set. Edit the container and fill in Password."

if ! is_mounted "$DATA_DIR" && [ "${ALLOW_UNMOUNTED_DATA:-0}" != "1" ]; then
    fail "$DATA_DIR is not mapped to a host folder. Your library would be lost when the container is recreated.
       Map App data (container path $DATA_DIR) to e.g. /mnt/user/appdata/gamelib."
fi

if [ ! -d "$GAMES_DIR" ] || ! is_mounted "$GAMES_DIR"; then
    echo "WARNING: $GAMES_DIR is not mapped. Map your games share to $GAMES_DIR (container path)." >&2
fi

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    # Own the app data folder. The games share is never chowned.
    find "$DATA_DIR" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} + 2>/dev/null || true

    if [ -d "$GAMES_DIR" ] && ! setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- test -w "$GAMES_DIR"; then
        echo "WARNING: $GAMES_DIR is not writable for $PUID:$PGID. Uploads and 'delete files' will fail (fine if you mounted it read-only)." >&2
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi
exec "$@"
