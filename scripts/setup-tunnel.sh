#!/usr/bin/env bash
# Create the Cloudflare tunnel for hyrox.miloprogressive.fit and start it on the Pi.
#
# Idempotent: re-running reuses an existing tunnel and updates the DNS record in
# place rather than creating duplicates.
#
# Reads the API token from the cs keychain store (cs:milo:CLOUDFLARE_API_TOKEN),
# or from $CLOUDFLARE_API_TOKEN if already exported. The token is never printed.
#
#   ./scripts/setup-tunnel.sh
#
# Needs the token to carry: Account > Cloudflare Tunnel > Edit
#                           Zone > DNS > Edit  (on miloprogressive.fit)

set -euo pipefail

ZONE="miloprogressive.fit"
HOSTNAME="hyrox.${ZONE}"
TUNNEL_NAME="hyrox"
SERVICE="http://hyrox:8000"
PI="admin@raspberrypi.local"
PI_DIR="/home/admin/Projects/hyrox"

CF_TOKEN="${CLOUDFLARE_API_TOKEN:-$(cs milo -secrets get CLOUDFLARE_API_TOKEN)}"
[[ -n "$CF_TOKEN" ]] || { echo "no Cloudflare API token available" >&2; exit 1; }

api() {
  local method="$1" path="$2"; shift 2
  curl -sS -X "$method" "https://api.cloudflare.com/client/v4${path}" \
    -H "Authorization: Bearer ${CF_TOKEN}" \
    -H "Content-Type: application/json" "$@"
}

jqf() { python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1],{'d':d}) or '')" "$1"; }

echo "==> verifying token"
api GET /user/tokens/verify | jqf "d['result']['status']"

echo "==> resolving account and zone"
ACCOUNT_ID=$(api GET /accounts | jqf "d['result'][0]['id']")
ZONE_ID=$(api GET "/zones?name=${ZONE}" | jqf "d['result'][0]['id']")
echo "    account ${ACCOUNT_ID:0:8}…  zone ${ZONE_ID:0:8}…"

echo "==> finding or creating tunnel '${TUNNEL_NAME}'"
TUNNEL_ID=$(api GET "/accounts/${ACCOUNT_ID}/cfd_tunnel?name=${TUNNEL_NAME}&is_deleted=false" \
  | jqf "(d['result'][0]['id'] if d['result'] else '')")

if [[ -z "$TUNNEL_ID" ]]; then
  SECRET=$(head -c 32 /dev/urandom | base64)
  TUNNEL_ID=$(api POST "/accounts/${ACCOUNT_ID}/cfd_tunnel" \
    --data "$(python3 -c "import json,sys;print(json.dumps({'name':sys.argv[1],'tunnel_secret':sys.argv[2],'config_src':'cloudflare'}))" "$TUNNEL_NAME" "$SECRET")" \
    | jqf "d['result']['id']")
  echo "    created ${TUNNEL_ID}"
else
  echo "    reusing ${TUNNEL_ID}"
fi

echo "==> routing ${HOSTNAME} -> ${SERVICE}"
api PUT "/accounts/${ACCOUNT_ID}/cfd_tunnel/${TUNNEL_ID}/configurations" \
  --data "$(python3 -c "
import json,sys
print(json.dumps({'config':{'ingress':[
  {'hostname':sys.argv[1],'service':sys.argv[2]},
  {'service':'http_status:404'}]}}))" "$HOSTNAME" "$SERVICE")" \
  | jqf "d['success']" > /dev/null && echo "    ingress set"

echo "==> DNS record"
RECORD_ID=$(api GET "/zones/${ZONE_ID}/dns_records?name=${HOSTNAME}" \
  | jqf "(d['result'][0]['id'] if d['result'] else '')")
BODY=$(python3 -c "
import json,sys
print(json.dumps({'type':'CNAME','name':sys.argv[1],
                  'content':sys.argv[2]+'.cfargotunnel.com','proxied':True}))" "$HOSTNAME" "$TUNNEL_ID")
if [[ -z "$RECORD_ID" ]]; then
  api POST "/zones/${ZONE_ID}/dns_records" --data "$BODY" | jqf "d['success']" > /dev/null
  echo "    created CNAME"
else
  api PUT "/zones/${ZONE_ID}/dns_records/${RECORD_ID}" --data "$BODY" | jqf "d['success']" > /dev/null
  echo "    updated CNAME"
fi

echo "==> installing connector token on the Pi"
CONNECTOR=$(api GET "/accounts/${ACCOUNT_ID}/cfd_tunnel/${TUNNEL_ID}/token" | jqf "d['result']")
ssh "$PI" "cd ${PI_DIR} && \
  sed -i 's|^TUNNEL_TOKEN=.*|TUNNEL_TOKEN=${CONNECTOR}|' secrets/secrets.env && \
  chmod 600 secrets/secrets.env && \
  docker compose up -d cloudflared && sleep 8 && \
  docker compose ps --format '{{.Service}} {{.State}}'"

echo "==> checking https://${HOSTNAME}/healthz"
for attempt in 1 2 3 4 5 6; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "https://${HOSTNAME}/healthz" || true)
  [[ "$code" == "200" ]] && { echo "    200 OK — live"; exit 0; }
  echo "    attempt ${attempt}: ${code}, waiting for DNS/tunnel…"
  sleep 10
done
echo "    not answering yet; check: ssh ${PI} 'cd ${PI_DIR} && docker compose logs cloudflared'"
