#!/usr/bin/env bash
# Download a GeoIP city database for Memento's share-link visitor location.
#
# Two sources supported, in order of preference:
#
#   1. MaxMind GeoLite2-City (best accuracy)
#      Requires a free MaxMind account + license key. Export
#      MAXMIND_LICENSE_KEY=... before running this script.
#      https://dev.maxmind.com/geoip/geolite2-free-geolocation-data
#
#   2. db-ip.com city-lite  (free, no signup)
#      Falls back here if no license key is set. Updated monthly; accuracy
#      is slightly lower than MaxMind but fine for "which country / city
#      visited my share link".
#
# Output: server/data/geoip/GeoLite2-City.mmdb  (this filename is what the
# server's MEMENTO_GEOIP_DB env var expects; db-ip download is renamed).
set -euo pipefail

DEST_DIR="$(cd "$(dirname "$0")/.." && pwd)/server/data/geoip"
mkdir -p "$DEST_DIR"
DEST="$DEST_DIR/GeoLite2-City.mmdb"

if [ -n "${MAXMIND_LICENSE_KEY:-}" ]; then
  echo "→ Downloading MaxMind GeoLite2-City…"
  TMP=$(mktemp -d)
  trap "rm -rf $TMP" EXIT
  URL="https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-City&license_key=$MAXMIND_LICENSE_KEY&suffix=tar.gz"
  curl -fL "$URL" -o "$TMP/db.tar.gz"
  tar -xzf "$TMP/db.tar.gz" -C "$TMP"
  MMDB=$(find "$TMP" -name "GeoLite2-City.mmdb" | head -1)
  [ -z "$MMDB" ] && { echo "extract failed"; exit 1; }
  mv "$MMDB" "$DEST"
else
  YM=$(date -u +%Y-%m)
  echo "→ MAXMIND_LICENSE_KEY not set. Falling back to db-ip.com free ($YM)…"
  URL="https://download.db-ip.com/free/dbip-city-lite-$YM.mmdb.gz"
  echo "   URL: $URL"
  TMP=$(mktemp -d)
  trap "rm -rf $TMP" EXIT
  if ! curl -fL "$URL" -o "$TMP/db.mmdb.gz"; then
    # db-ip publishes new month's file a few days into the month — if
    # today's YM isn't up yet, retry with the previous month.
    PREV_YM=$(date -u -v-1m +%Y-%m 2>/dev/null || date -u -d "1 month ago" +%Y-%m)
    echo "   Current month not available yet, trying $PREV_YM"
    URL="https://download.db-ip.com/free/dbip-city-lite-$PREV_YM.mmdb.gz"
    curl -fL "$URL" -o "$TMP/db.mmdb.gz"
  fi
  gunzip "$TMP/db.mmdb.gz"
  mv "$TMP/db.mmdb" "$DEST"
fi

ls -lh "$DEST"
echo "✓ GeoIP DB ready at $DEST"
echo "  Restart api container to pick it up: docker compose restart api"
