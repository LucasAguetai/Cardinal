#!/usr/bin/env bash
# Cardinal — auto-retry de creation d'une instance Oracle "Always Free"
# jusqu'a obtenir la capacite. Necessite l'OCI CLI configure (oci iam region list OK).
set -o pipefail

# ---- Reglages (modifiables via variables d'env) ----
SHAPE="${SHAPE:-VM.Standard.A1.Flex}"        # ARM. Pour x86 : VM.Standard.E2.1.Micro
OCPUS="${OCPUS:-1}"; MEM="${MEM:-6}"          # seulement pour A1.Flex
OSVER="${OSVER:-22.04}"
NAME="${NAME:-Cardinal}"
SSH_PUB="${SSH_PUB:-$HOME/.ssh/id_ed25519.pub}"
SLEEP="${SLEEP:-60}"                          # pause entre deux tours (s)

[ -f "$SSH_PUB" ] || { echo "Cle SSH introuvable: $SSH_PUB (edite SSH_PUB)"; exit 1; }

echo "> Decouverte de ta config Oracle..."
COMP=$(awk -F= '/^tenancy/{gsub(/[ \t]/,"",$2);print $2}' ~/.oci/config | head -1)
[ -n "$COMP" ] || { echo "tenancy introuvable dans ~/.oci/config"; exit 1; }

ADS=$(oci iam availability-domain list -c "$COMP" --output json \
  | python3 -c "import sys,json;print(' '.join(d['name'] for d in json.load(sys.stdin)['data']))")
SUBNET=$(oci network subnet list -c "$COMP" --all --output json \
  | python3 -c "import sys,json;s=json.load(sys.stdin)['data'];p=[x for x in s if not x.get('prohibit-public-ip-on-vnic')];print((p or s)[0]['id'] if (p or s) else '')")
IMAGE=$(oci compute image list -c "$COMP" --operating-system 'Canonical Ubuntu' \
  --operating-system-version "$OSVER" --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC \
  --output json | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(d[0]['id'] if d else '')")

[ -n "$SUBNET" ] || { echo "subnet public introuvable"; exit 1; }
[ -n "$IMAGE" ]  || { echo "image Ubuntu $OSVER pour $SHAPE introuvable"; exit 1; }
[ -n "$ADS" ]    || { echo "aucune availability domain"; exit 1; }

echo "  shape  : $SHAPE"
echo "  subnet : $SUBNET"
echo "  image  : $IMAGE"
echo "  ADs    : $ADS"

if [[ "$SHAPE" == *Flex* ]]; then
  SHAPE_CFG_JSON="{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM}"
else
  SHAPE_CFG_JSON=""
fi

echo "> Lancement en boucle (Ctrl+C pour arreter)..."
n=0
while true; do
  n=$((n+1))
  for AD in $ADS; do
    # garde-fou : si l'autre script (ou un tour precedent) a deja decroche, on stoppe
    [ -f /tmp/cardinal_got_it ] && { echo; echo "Une instance a deja ete obtenue -> arret."; exit 0; }
    printf '[%s] essai %d - %s ... ' "$(date +%H:%M:%S)" "$n" "$AD"
    if [ -n "$SHAPE_CFG_JSON" ]; then
      OUT=$(oci compute instance launch --availability-domain "$AD" --compartment-id "$COMP" \
        --shape "$SHAPE" --shape-config "$SHAPE_CFG_JSON" --image-id "$IMAGE" --subnet-id "$SUBNET" \
        --assign-public-ip true --display-name "$NAME" --ssh-authorized-keys-file "$SSH_PUB" \
        --wait-for-state RUNNING 2>/tmp/cardinal_oci_err)
    else
      OUT=$(oci compute instance launch --availability-domain "$AD" --compartment-id "$COMP" \
        --shape "$SHAPE" --image-id "$IMAGE" --subnet-id "$SUBNET" \
        --assign-public-ip true --display-name "$NAME" --ssh-authorized-keys-file "$SSH_PUB" \
        --wait-for-state RUNNING 2>/tmp/cardinal_oci_err)
    fi
    if [ $? -eq 0 ]; then
      touch /tmp/cardinal_got_it   # signale a l'autre script d'arreter
      IID=$(echo "$OUT" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['id'])")
      IP=$(oci compute instance list-vnics --instance-id "$IID" --output json \
        | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(d[0].get('public-ip','') if d else '')")
      echo; echo "==================================================="
      echo " INSTANCE CREEE ET RUNNING"
      echo "   IP publique : $IP"
      echo "   Connexion   : ssh ubuntu@$IP"
      echo "==================================================="
      exit 0
    fi
    if grep -qi 'capacity' /tmp/cardinal_oci_err; then
      echo "pas de capacite"
    elif grep -qi 'TooManyRequests\|429\|RequestException\|Timeout' /tmp/cardinal_oci_err; then
      echo "throttle/reseau -> pause 90s"; sleep 90
    else
      echo "erreur:"; sed 's/^/    /' /tmp/cardinal_oci_err | head -4
    fi
  done
  sleep "$SLEEP"
done
