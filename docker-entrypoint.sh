#!/bin/sh
# Starts the app as an ordinary user, not root.
#
# PUID/PGID choose that user (Unraid's usual nobody:users is 99:100). Files
# in /config that an older version wrote as root are handed over to it first,
# so updating keeps working. Run with PUID=0 to stay root (not recommended),
# or start the container with --user to skip this script's changes entirely.
set -e
CONFIG="${CONFIG_DIR:-/config}"
if [ "$(id -u)" = "0" ]; then
  PUID="${PUID:-99}"
  PGID="${PGID:-100}"
  if [ "$PUID" != "0" ]; then
    mkdir -p "$CONFIG"
    if ! find "$CONFIG" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} + 2>/dev/null; then
      echo "next-stop: could not hand $CONFIG to $PUID:$PGID; saving settings and history may fail" >&2
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --no-new-privs "$@"
  fi
fi
exec "$@"
