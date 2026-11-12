#!/bin/bash

# contentcache_metrics.sh

# Show macOS Content Caching metrics from Metrics.db (bash 3.2 compatible).
# Needs only bash, sqlite3 and awk, all of which ship with macOS.
# Source: https://support.apple.com/en-gb/guide/deployment/dep0504346e1/web

# Variables
#  DB            SQLite DB where macOS Content Caching records metrics (override: --db)
#  HOURS         How far back to report, in hours (override: --hours N or --days N)
#  BY            Grouping for the summary table, e.g. hour or day (--by)
#  CSV           1 = print raw per-minute rows as CSV instead of the summary (--csv)
#  TABLE         Table to query; the Z prefix is CoreData's SQLite naming convention
#  EPOCH_OFFSET  Seconds between 1970-01-01 (Unix) and 2001-01-01 (CoreData), used to convert timestamps
DB="/Library/Application Support/Apple/AssetCache/Metrics/Metrics.db"
HOURS=24
BY=""
CSV=0
TABLE="ZMETRIC"
EPOCH_OFFSET=978307200
VERSION=1.3
UPLOAD_URL=""
UPLOAD_METHOD="POST"
SERVER_ID=""
REPORT_HOSTNAME=""
TOKEN="${CONTENTCACHE_TOKEN:-}"
CSV_UTC=0

# Shared awk helper, prepended to each awk program that prints sizes
AWK_FORMAT_BYTES='
function format_bytes(n,   u, i) {
  split("B KB MB GB TB", u, " "); i = 1
  while (n >= 1000 && i < 5) { n /= 1000; i++ }
  return (i == 1) ? sprintf("%d B", n) : sprintf("%.2f %s", n, u[i])
}
'

# Functions
# Usage
usage() {
  local APP_NAME="$(basename "$0")"
  echo "
${APP_NAME} v${VERSION}

Usage:
  ${APP_NAME}                  # last 24h summary + hourly table
  ${APP_NAME}  --hours 6
  ${APP_NAME}  --days 7 --by day/hour
  ${APP_NAME}  --csv > out.csv # raw per-minute rows
  ${APP_NAME}  --db /path/to/Metrics.db
  ${APP_NAME}  --post http://dashboard-host:8080/api/metrics
  ${APP_NAME}  --hours 6 --post http://dashboard-host:8080/api/metrics --server-id cache-01
  ${APP_NAME}  --csv --put http://dashboard-host:8080/api/metrics > out.csv

Upload options:
  --post URL       POST CSV to the full endpoint URL (requires updated server.py)
  --put URL        PUT CSV to the full endpoint URL (also works with original receiver)
  --server-id ID   Stable unique server ID (default: LocalHostName, then hostname)
  --hostname NAME Display name (default: ComputerName, then hostname)
  --token TOKEN    Enrollment token, if the receiver requires one (or set CONTENTCACHE_TOKEN).
                   A new server ID waits for approval on the dashboard before data is accepted.

Uploads use UTC timestamps and X-Timezone: UTC. Add --csv to also print
uploaded CSV to stdout. Without upload options, existing output is unchanged.
"
}

# sql read only
sqlite_ro() { sqlite3 -readonly "$@" 2>&1; }

# column_or_zero NAME -> COALESCE(ZNAME,0) if it exists, else 0
column_or_zero() {
  local u
  u=$(echo "Z$1" | tr 'a-z' 'A-Z')
  if echo "$COLS" | grep -qx "$u"; then echo "COALESCE($u,0)"; else echo "0"; fi
}

# sum_bytes_*: builds a SQL expression "a + b + c"
sum_bytes_to_dest() {
  local out="" s
  for s in $SRC; do out="$out${out:+ + }$(column_or_zero bytesFrom${s}To$1)"; done
  echo "$out"
}

# sum_bytes_from_source: builds a SQL expression "a + b + c"
sum_bytes_from_source() {
  local out="" d
  for d in $DST; do out="$out${out:+ + }$(column_or_zero bytesFrom$1To$d)"; done
  echo "$out"
}

# Check its running as root
check_root() {
  if [ "$(/usr/bin/id -u)" != "0" ]; then
    echo "$(/usr/bin/basename "$0") MUST run as root..."
    exit 1
  fi
}

# Check passed args
parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --db|--hours|--days|--by|--post|--put|--server-id|--hostname|--token)
        if [ $# -lt 2 ] || [ -z "$2" ] || [[ "$2" == --* ]]; then
          echo "Missing value for $1" >&2
          exit 1
        fi ;;
    esac
    case "$1" in
      --db)    DB="$2"; shift 2 ;;
      --hours) HOURS="$2"; shift 2 ;;
      --days)  HOURS=$(awk -v d="$2" 'BEGIN{print d*24}'); shift 2 ;;
      --by)    BY="$2"; shift 2 ;;
      --csv)   CSV=1; shift ;;
      --post|--put)
        if [ -n "$UPLOAD_URL" ]; then
          echo "Use only one of --post or --put" >&2; exit 1
        fi
        case "$1" in --post) UPLOAD_METHOD=POST ;; --put) UPLOAD_METHOD=PUT ;; esac
        UPLOAD_URL="$2"; shift 2 ;;
      --server-id) SERVER_ID="$2"; shift 2 ;;
      --hostname) REPORT_HOSTNAME="$2"; shift 2 ;;
      --token) TOKEN="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
  done
}

# Check sqlite3 is able to read DB
check_prereqs() {
  command -v sqlite3 >/dev/null || { echo "sqlite3 not found" >&2; exit 1; }
  [ -r "$DB" ] || { echo "Can't read $DB (try sudo, or check Content Caching has been enabled)" >&2; exit 1; }
}

# CoreData upper-cases columns and prefixes Z: bytesDropped -> ZBYTESDROPPED
load_columns() {
  COLS=$(sqlite_ro "$DB" "PRAGMA table_info($TABLE);" | cut -d'|' -f2 | tr 'a-z' 'A-Z')
  if ! echo "$COLS" | grep -qx "ZCREATIONDATE"; then
    echo "No $TABLE/ZCREATIONDATE found in $DB. Tables:" >&2
    sqlite_ro "$DB" ".tables" >&2
    exit 1
  fi
}

# Build the SQL expressions used by the summary and table queries
build_expressions() {
  SRC="Cache Origin Parent Peer"
  DST="Child Client Peer"
  FROM_CACHE=$(sum_bytes_from_source Cache)
  FROM_ORIGIN=$(sum_bytes_from_source Origin)
  FROM_PARENT=$(sum_bytes_from_source Parent)
  FROM_PEER=$(sum_bytes_from_source Peer)
  TO_CLIENT=$(sum_bytes_to_dest Client)
  TO_CHILD=$(sum_bytes_to_dest Child)
  TO_PEER=$(sum_bytes_to_dest Peer)
  SERVED="($FROM_CACHE) + ($FROM_ORIGIN) + ($FROM_PARENT) + ($FROM_PEER)"
  REQS="$(column_or_zero requestsFromChild) + $(column_or_zero requestsFromClient) + $(column_or_zero requestsFromPeer)"
  UPLOADED="$(column_or_zero bytesImportedByHTTP) + $(column_or_zero bytesImportedByXPC)"
  # Cache pressure, as Activity Monitor calculates it
  PRESSURE="MAX(CASE WHEN $(column_or_zero requestsRejectedForNoSpace) > 0 THEN 100
                     WHEN $(column_or_zero bytesPurgedYoungerThan1Day) > 0 THEN 80
                     WHEN $(column_or_zero bytesPurgedYoungerThan7Days) > 0 THEN 60
                     WHEN $(column_or_zero bytesPurgedYoungerThan30Days) > 0 THEN 40
                     WHEN $(column_or_zero bytesPurgedTotal) > 0 THEN 20 ELSE 0 END)"
}

# Build the WHERE clause for the requested time window
build_time_filter() {
  local NOW SINCE
  NOW=$(date +%s)
  SINCE=$(awk -v n="$NOW" -v o="$EPOCH_OFFSET" -v h="$HOURS" 'BEGIN{printf "%d", n-o-h*3600}')
  WHERE="ZCREATIONDATE > $SINCE"
}

# CSV Raw output
print_csv() {
  local names sel n
  names="bytesFromCacheToChild bytesFromCacheToClient bytesFromCacheToPeer bytesFromOriginToChild bytesFromOriginToClient bytesFromOriginToPeer bytesFromParentToChild bytesFromParentToClient bytesFromParentToPeer bytesFromPeerToChild bytesFromPeerToClient bytesDropped bytesImportedByHTTP bytesImportedByXPC bytesPurgedTotal bytesPurgedYoungerThan1Day bytesPurgedYoungerThan7Days bytesPurgedYoungerThan30Days importsByHTTP importsByXPC period requestsFromChild requestsFromClient requestsFromPeer requestsRejectedForNoSpace"
  if [ "$CSV_UTC" = 1 ]; then
    sel="datetime(ZCREATIONDATE + $EPOCH_OFFSET,'unixepoch') AS timestamp"
  else
    sel="datetime(ZCREATIONDATE + $EPOCH_OFFSET,'unixepoch','localtime') AS timestamp"
  fi
  for n in $names; do sel="$sel, $(column_or_zero $n) AS $n"; done
  # Write headers explicitly: sqlite3 omits them when the query has no rows.
  printf 'timestamp'
  for n in $names; do printf ',%s' "$n"; done
  printf '\n'
  sqlite3 -readonly -noheader -csv "$DB" "SELECT $sel FROM $TABLE WHERE $WHERE ORDER BY ZCREATIONDATE;"
}

# Upload a complete export only after SQLite succeeds. Temporary files are private
# and removed on success, failure or interruption. Upload replies go to stderr.
upload_csv() (
  # Subshell scope keeps this path available to the EXIT trap.
  csv_file=""
  command -v curl >/dev/null || { echo "curl not found" >&2; exit 1; }
  case "$UPLOAD_URL" in
    http://*|https://*) ;;
    *) echo "Upload URL must begin with http:// or https://" >&2; exit 1 ;;
  esac
  if [ -z "$SERVER_ID" ]; then
    SERVER_ID=$(/usr/sbin/scutil --get LocalHostName 2>/dev/null) || SERVER_ID=$(hostname)
  fi
  if [ -z "$REPORT_HOSTNAME" ]; then
    REPORT_HOSTNAME=$(/usr/sbin/scutil --get ComputerName 2>/dev/null) || REPORT_HOSTNAME=$(hostname)
  fi
  if [ -z "$SERVER_ID" ] || [ ${#SERVER_ID} -gt 200 ] || [ ${#REPORT_HOSTNAME} -gt 200 ]; then
    echo "Server ID must be 1–200 characters; hostname must be at most 200 characters" >&2
    exit 1
  fi
  case "$SERVER_ID$REPORT_HOSTNAME" in
    *$'\r'*|*$'\n'*) echo "Server ID and hostname cannot contain line breaks" >&2; exit 1 ;;
  esac
  case "$TOKEN" in
    *[!A-Za-z0-9._~+/=-]*) echo "Token may contain only letters, digits and . _ ~ + / = -" >&2; exit 1 ;;
  esac
  umask 077
  csv_file=$(mktemp "${TMPDIR:-/tmp}/contentcache_metrics.XXXXXX") || exit 1
  trap 'rm -f "$csv_file"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM HUP
  CSV_UTC=1
  print_csv > "$csv_file" || { echo "CSV export failed; nothing uploaded" >&2; exit 1; }
  # The token goes to curl through a private config stream so it never appears in the process list.
  curl --config <(if [ -n "$TOKEN" ]; then printf 'header = "Authorization: Bearer %s"\n' "$TOKEN"; fi) \
    --fail-with-body --silent --show-error --connect-timeout 10 --max-time 120 \
    --request "$UPLOAD_METHOD" \
    --header 'Content-Type: text/csv' \
    --header "X-Server-ID: $SERVER_ID" \
    --header "X-Hostname: $REPORT_HOSTNAME" \
    --header 'X-Timezone: UTC' \
    --data-binary "@$csv_file" "$UPLOAD_URL" >&2 || exit $?
  printf '\n' >&2
  if [ "$CSV" = 1 ]; then cat "$csv_file"; fi
)

# Sets SUMMARY (pipe-separated totals) and N (sample count)
query_summary() {
  SUMMARY=$(sqlite3 -readonly -separator '|' "$DB" "
SELECT COUNT(*),
  datetime(MIN(ZCREATIONDATE)+$EPOCH_OFFSET,'unixepoch','localtime'),
  datetime(MAX(ZCREATIONDATE)+$EPOCH_OFFSET,'unixepoch','localtime'),
  IFNULL(SUM($SERVED),0), IFNULL(SUM($FROM_CACHE),0), IFNULL(SUM($FROM_ORIGIN),0),
  IFNULL(SUM($FROM_PARENT),0), IFNULL(SUM($FROM_PEER),0),
  IFNULL(SUM($TO_CLIENT),0), IFNULL(SUM($TO_CHILD),0), IFNULL(SUM($TO_PEER),0),
  IFNULL(SUM($UPLOADED),0), IFNULL(SUM($(column_or_zero bytesDropped)),0), IFNULL(SUM($(column_or_zero bytesPurgedTotal)),0),
  IFNULL(SUM($REQS),0), IFNULL(SUM($(column_or_zero requestsRejectedForNoSpace)),0), IFNULL($PRESSURE,0)
FROM $TABLE WHERE $WHERE;" 2>&1)

  N=$(echo "$SUMMARY" | cut -d'|' -f1)
}

# If there is no data, print a message
print_no_data() {
  echo "No metrics rows in the last ${HOURS}h (idle periods are skipped by the cache)."
  echo
  echo "Diagnostics"
  echo "-----------"
  echo "Files:"; ls "$(dirname "$DB")" | sed 's/^/  /'
  local TOTAL
  TOTAL=$(sqlite_ro "$DB" "SELECT COUNT(*) FROM $TABLE;")
  echo "$TABLE rows total: $TOTAL"
  if [ "$TOTAL" != "0" ]; then
    sqlite_ro "$DB" "SELECT 'oldest ' || datetime(MIN(ZCREATIONDATE)+$EPOCH_OFFSET,'unixepoch','localtime') || ', newest ' || datetime(MAX(ZCREATIONDATE)+$EPOCH_OFFSET,'unixepoch','localtime') FROM $TABLE;"
    echo "Try: --days 30 (or the cache has been idle since the newest row)"
  else
    echo "Empty: Content Caching hasn't served anything yet, or the db was reset."
    echo "Check: sudo AssetCacheManagerUtil status"
  fi
}

# Output summary
print_summary() {
  echo "$SUMMARY" | awk -F'|' "$AWK_FORMAT_BYTES"'
{
  hit = ($4 > 0) ? 100 * $5 / $4 : 0
  printf "Content Cache metrics  (%d samples, %s -> %s)\n", $1, substr($2,1,16), substr($3,1,16)
  printf "=======================================================================\n"
  printf "Bytes served           %12s\n", format_bytes($4)
  printf "  from cache           %12s   hit rate %.1f%%\n", format_bytes($5), hit
  printf "  from origin          %12s\n", format_bytes($6)
  printf "  from parent          %12s\n", format_bytes($7)
  printf "  from peer            %12s\n", format_bytes($8)
  printf "Served to clients      %12s\n", format_bytes($9)
  printf "Served to children     %12s\n", format_bytes($10)
  printf "Served to peers        %12s\n", format_bytes($11)
  printf "Bytes downloaded       %12s\n", format_bytes($6 + $7 + $8)
  printf "Bytes uploaded         %12s\n", format_bytes($12)
  printf "Bytes dropped          %12s\n", format_bytes($13)
  printf "Bytes purged           %12s\n", format_bytes($14)
  printf "Requests               %12d  (rejected, no space: %d)\n", $15, $16
  printf "Max cache pressure     %11d%%\n\n", $17
}'
}

# Output table of data
print_table() {
  if [ -z "$BY" ]; then
    BY=$(awk -v h="$HOURS" 'BEGIN{print (h > 48) ? "day" : "hour"}')
  fi
  local FMT
  if [ "$BY" = "day" ]; then FMT='%Y-%m-%d'; else FMT='%Y-%m-%d %H:00'; fi

  sqlite3 -readonly -separator '|' "$DB" "
SELECT strftime('$FMT', ZCREATIONDATE+$EPOCH_OFFSET,'unixepoch','localtime') AS b,
  SUM($SERVED), SUM($FROM_CACHE), SUM($FROM_ORIGIN), SUM($TO_CLIENT), SUM($REQS), $PRESSURE
FROM $TABLE WHERE $WHERE GROUP BY b ORDER BY b;" | awk -F'|' "$AWK_FORMAT_BYTES"'
BEGIN {
  printf "%-17s%11s%11s%11s%7s%11s%9s%7s\n", "Period", "Served", "Cache", "Origin", "Hit %", "Clients", "Reqs", "Press"
  print "------------------------------------------------------------------------------------"
}
{
  hit = ($2 > 0) ? 100 * $3 / $2 : 0
  printf "%-17s%11s%11s%11s%6.1f%%%11s%9d%6d%%\n", $1, format_bytes($2), format_bytes($3), format_bytes($4), hit, format_bytes($5), $6, $7
}'
}

# Main function
main() {
  parse_args "$@"
  check_root
  check_prereqs
  load_columns
  build_expressions
  build_time_filter

  if [ -n "$UPLOAD_URL" ]; then
    upload_csv
    return $?
  fi
  if [ "$CSV" = 1 ]; then
    print_csv
    return $?
  fi

  query_summary
  if [ -z "$N" ] || [ "$N" = "0" ]; then
    print_no_data
    return 0
  fi

  print_summary
  print_table
}

# Lets a go
main "$@"
exit $?