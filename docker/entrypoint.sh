#!/bin/sh
# Renders the cron schedule into a crontab file and hands off to supercronic,
# which runs it in the foreground (so the container just sleeps between runs)
# and streams job stdout/stderr straight into `docker logs`.
set -eu

CRON_SCHEDULE="${CRON_SCHEDULE:-*/5 * * * *}"
CONFIG_FILE="${CONFIG_FILE:-/config/pylibrus.ini}"
DATA_DIR="${DATA_DIR:-/data}"
# Extra CLI flags appended verbatim, e.g. `--debug`.
PYLIBRUS_EXTRA_ARGS="${PYLIBRUS_EXTRA_ARGS:-}"

mkdir -p "$DATA_DIR"

CRONTAB_FILE=/app/crontab
cat > "$CRONTAB_FILE" <<EOF
$CRON_SCHEDULE cd /app && uv run --no-sync src/pylibrus/pylibrus.py --workdir "$DATA_DIR" --config "$CONFIG_FILE" $PYLIBRUS_EXTRA_ARGS
EOF

echo "pylibrus: workdir=$DATA_DIR config=$CONFIG_FILE schedule='$CRON_SCHEDULE'"
if [ -f "$CONFIG_FILE" ]; then
    echo "pylibrus: using config file at $CONFIG_FILE"
else
    echo "pylibrus: no config file at $CONFIG_FILE, falling back to LIBRUS_* env vars (single user)"
fi

exec supercronic "$CRONTAB_FILE"
