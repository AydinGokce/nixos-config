# dc — orchestrate ephemeral DataCrunch GPU instances from the head node.
#
#   dc types [--gpu|--cpu]              list instance types + prices
#   dc ls                              list your running instances
#   dc launch <type> [--spot] [--name N] [--image ubuntu-24.04] [--loc FIN-02]
#                                      provision an instance; prints id + ip when ready
#   dc ssh <id|name> [-- cmd...]       ssh in (as root, automation key)
#   dc run <type> [opts] -- <cmd...>   launch, run cmd, then ALWAYS destroy (trap)
#   dc rm <id|name|all>                destroy instance(s)
#   dc spend                           estimated cumulative spend vs the ceiling
#
# Budget: refuses to launch once estimated spend >= $DC_BUDGET_CEILING (default 500).
# Creds: /root/.config/datacrunch/credentials.env (600, NOT in the repo).
# Ledger: /var/lib/dc/ledger.tsv  (id  type  price/hr  start_epoch  end_epoch).

set -euo pipefail
API=https://api.datacrunch.io/v1
CRED=/root/.config/datacrunch/credentials.env
LEDGER=/var/lib/dc/ledger.tsv
KEY=/root/.ssh/datacrunch_ed25519
CEILING="${DC_BUDGET_CEILING:-500}"
mkdir -p "$(dirname "$LEDGER")"; touch "$LEDGER"

[ -r "$CRED" ] || { echo "dc: missing $CRED (scp the DataCrunch creds there, chmod 600)" >&2; exit 3; }
# shellcheck source=/dev/null
. "$CRED"

tok() {
  curl -s -X POST "$API/oauth2/token" -H 'Content-Type: application/json' \
    -d "{\"grant_type\":\"client_credentials\",\"client_id\":\"$DATACRUNCH_CLIENT_ID\",\"client_secret\":\"$DATACRUNCH_CLIENT_SECRET\"}" \
    | jq -r .access_token
}
api() { # METHOD PATH [jsonbody]
  local m="$1" p="$2" body="${3:-}" t; t="$(tok)"
  if [ -n "$body" ]; then
    curl -s -X "$m" -H "Authorization: Bearer $t" -H 'Content-Type: application/json' -d "$body" "$API$p"
  else
    curl -s -X "$m" -H "Authorization: Bearer $t" "$API$p"
  fi
}

spend_now() { # -> prints total USD estimate
  local now; now=$(date +%s)
  awk -v now="$now" -F'\t' '{e=($5>0?$5:now); c+=($3*(e-$4)/3600)} END{printf "%.4f", c+0}' "$LEDGER"
}

resolve() { # id-or-name -> id (from ledger or live list)
  local q="$1"
  if grep -qP "^$q\t" "$LEDGER" 2>/dev/null; then echo "$q"; return; fi
  api GET /instances | jq -r --arg q "$q" '.[] | select(.id==$q or .hostname==$q) | .id' | head -1
}

cmd="${1:-}"; [ $# -gt 0 ] && shift || true
case "$cmd" in
  types)
    filt="${1:-}"
    api GET /instance-types | jq -r --arg f "$filt" '
      .[] | [.instance_type, (.gpu.description//"cpu"), (.price_per_hour+"/hr od"), (.spot_price+"/hr spot")] |
      @tsv' | { case "$filt" in --gpu) grep -vP '\tcpu\t';; --cpu) grep -P '\tcpu\t';; *) cat;; esac; } | column -t -s$'\t' ;;

  ls)
    api GET /instances | jq -r '.[] | [.id[0:8], .instance_type, .status, (.ip//"-"), (.hostname//"-")] | @tsv' | column -t -s$'\t' || echo "(none)" ;;

  spend)
    s=$(spend_now); printf "estimated spend: \$%.2f / \$%s ceiling  (\$%.2f remaining)\n" "$s" "$CEILING" "$(awk -v c="$CEILING" -v s="$s" 'BEGIN{print c-s}')" ;;

  launch)
    type=""; spot=false; name="gpu-$$"; image="ubuntu-24.04-cuda-12.8-open-docker"; loc="FIN-02"; vols=""
    while [ $# -gt 0 ]; do case "$1" in
      --spot) spot=true;; --name) name="$2"; shift;; --image) image="$2"; shift;; --loc) loc="$2"; shift;;
      --volume) vols="$vols $2"; shift;;
      -*) echo "dc launch: unknown $1" >&2; exit 2;; *) type="$1";; esac; shift; done
    [ -n "$type" ] || { echo "dc launch <instance_type> (see: dc types --gpu)" >&2; exit 2; }
    # budget guard
    s=$(spend_now); over=$(awk -v c="$CEILING" -v s="$s" 'BEGIN{print (s>=c)?1:0}')
    [ "$over" = 1 ] && { echo "dc: BUDGET HALT — estimated \$$s >= \$$CEILING ceiling; not launching" >&2; exit 4; }
    price=$(api GET /instance-types | jq -r --arg t "$type" --argjson spot "$spot" '.[]|select(.instance_type==$t)|(if $spot then .spot_price else .price_per_hour end)')
    [ -n "$price" ] && [ "$price" != null ] || { echo "dc: unknown instance type '$type'" >&2; exit 2; }
    keyids=$(api GET /sshkeys | jq -c '[.[].id]')
    # shellcheck disable=SC2086
    volsjson=$(printf '%s\n' $vols | sed '/^$/d' | jq -R . | jq -sc .)
    body=$(jq -nc --arg t "$type" --arg img "$image" --arg n "$name" --arg loc "$loc" --argjson keys "$keyids" --argjson spot "$spot" --argjson vols "$volsjson" \
      '{instance_type:$t,image:$img,ssh_key_ids:$keys,hostname:$n,description:"ephemeral GPU job",location_code:$loc,is_spot:$spot} + (if ($vols|length)>0 then {existing_volumes:$vols} else {} end)')
    id=$(api POST /instances "$body" | tr -d '"')
    # a successful deploy returns a bare UUID; anything else (e.g. a
    # {code:service_unavailable,...} capacity error) is a failure.
    if ! printf '%s' "$id" | grep -qiE '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'; then
      echo "dc: launch failed: $id" >&2; exit 1
    fi
    echo "launched $id ($type @ \$$price/hr); waiting for running..."
    ip=""
    for _ in $(seq 1 60); do
      j=$(api GET "/instances/$id"); st=$(echo "$j" | jq -r '.status // .state'); ip=$(echo "$j" | jq -r '.ip // "-"')
      [ "$st" = running ] && [ "$ip" != "-" ] && break; sleep 10
    done
    printf '%s\t%s\t%s\t%s\t0\n' "$id" "$type" "$price" "$(date +%s)" >> "$LEDGER"
    echo "READY id=$id ip=$ip  (ssh: dc ssh $id)"
    ;;

  rm)
    target="${1:-}"; [ -n "$target" ] || { echo "dc rm <id|name|all>" >&2; exit 2; }
    if [ "$target" = all ]; then ids=$(api GET /instances | jq -r '.[].id'); else ids=$(resolve "$target"); fi
    [ -n "$ids" ] || { echo "dc: nothing to remove"; exit 0; }
    for id in $ids; do
      api PUT /instances "$(jq -nc --arg id "$id" '{id:$id,action:"delete"}')" >/dev/null || api DELETE "/instances/$id" >/dev/null || true
      now=$(date +%s); tmp=$(mktemp); awk -v id="$id" -v now="$now" -F'\t' 'BEGIN{OFS="\t"} $1==id && $5==0 {$5=now} {print}' "$LEDGER" > "$tmp" && mv "$tmp" "$LEDGER"
      echo "removed $id"
    done ;;

  ssh)
    target="${1:-}"; shift || true; [ "${1:-}" = "--" ] && shift || true
    id=$(resolve "$target"); [ -n "$id" ] || { echo "dc: no such instance '$target'" >&2; exit 2; }
    ip=$(api GET "/instances/$id" | jq -r '.ip // "-"')
    [ "$ip" != "-" ] || { echo "dc: instance has no ip yet" >&2; exit 1; }
    # ephemeral nodes reuse IPs, so don't persist/verify host keys (throwaway compute)
    exec ssh -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR root@"$ip" "$@" ;;

  run)
    # dc run <type> [launch opts] -- <cmd...>  : launch, run, ALWAYS destroy.
    largs=(); rcmd=(); sep=0
    while [ $# -gt 0 ]; do
      if [ "$1" = "--" ]; then sep=1; shift; continue; fi
      if [ "$sep" = 1 ]; then rcmd+=("$1"); else largs+=("$1"); fi; shift
    done
    [ "${#rcmd[@]}" -gt 0 ] || { echo "dc run <type> [opts] -- <cmd...>" >&2; exit 2; }
    out=$("$0" launch "${largs[@]}") || { echo "$out" >&2; exit 1; }
    echo "$out"
    id=$(printf '%s' "$out" | grep -oE 'id=[0-9a-f-]{36}' | head -1 | cut -d= -f2)
    [ -n "$id" ] || { echo "dc run: launch produced no id" >&2; exit 1; }
    # guarantee teardown on any exit (success, error, Ctrl-C)
    trap '"'"$0"'" rm "'"$id"'" >/dev/null 2>&1 || true; echo "dc run: destroyed '"$id"'"' EXIT INT TERM
    echo "dc run: waiting for sshd on $id ..."
    for _ in $(seq 1 18); do "$0" ssh "$id" -- true 2>/dev/null && break; sleep 10; done
    "$0" ssh "$id" -- "${rcmd[@]}"
    ;;

  ""|-h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) echo "dc: unknown command '$cmd' (try: dc --help)" >&2; exit 2 ;;
esac
