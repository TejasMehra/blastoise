#!/usr/bin/env bash
# Run measure_profiles.py on three GCE hardware profiles and collect the JSON.
#
# Needs: gcloud authenticated (`gcloud auth login`) with compute access on
# the configured project. Creates three preemptible-free, on-demand VMs,
# ships the measurement bundle, runs the script under nohup, polls until
# every run has written its result, copies the results back, and deletes
# the VMs. Total runtime is bounded by the slowest profile (the burstable
# one, ~1.5-2 h at 10M rows); cost is a few dollars.
#
#   artifacts/scripts/run_profiles_gce.sh [out_dir]
#
# Profiles (CPU and disk characteristics deliberately spread):
#   burstable : e2-small, 2 shared vCPU / 2 GB, pd-standard (HDD-class)
#   compute   : c3-standard-4, 4 vCPU / 16 GB, pd-ssd
#   storage   : n2-standard-4, 4 vCPU / 16 GB, one local NVMe SSD for PGDATA
#
# Afterwards:
#   python artifacts/scripts/derive_constants.py --anchor laptop-nvme \
#       scratch/profiles/laptop-nvme.json "$out_dir"/*.json --out artifacts/profiles/derived.json

set -euo pipefail

OUT_DIR="${1:-scratch/profiles}"
ZONE="${GCE_ZONE:-us-central1-a}"
IMAGE_FLAGS=(--image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud)
SIZES="${PROFILE_SIZES:-t_1k,t_100k,t_1m,t_10m}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUNDLE="$(mktemp -d)/bundle.tar.gz"

mkdir -p "$OUT_DIR"

# The measurement bundle: only what measure_profiles.py imports.
tar -C "$ROOT" -czf "$BUNDLE" \
  src/blastoise/__init__.py src/blastoise/ir.py \
  src/blastoise/catalog/__init__.py src/blastoise/catalog/model.py \
  src/blastoise/live/__init__.py src/blastoise/live/model.py src/blastoise/live/calibrate.py \
  src/blastoise/verdict/__init__.py src/blastoise/verdict/constants.py \
  validation/__init__.py validation/harness/__init__.py \
  validation/harness/fixtures.py validation/harness/labeling.py \
  artifacts/scripts/measure_profiles.py

declare -A MACHINE=( [burstable]=e2-small [compute]=c3-standard-4 [storage]=n2-standard-4 )
declare -A DISK=( [burstable]="pd-standard" [compute]="pd-ssd" [storage]="pd-balanced" )
declare -A LABEL=(
  [burstable]="e2-small, pd-standard 60GB (HDD-class network disk)"
  [compute]="c3-standard-4, pd-ssd 60GB"
  [storage]="n2-standard-4, local NVMe SSD 375GB"
)

for p in burstable compute storage; do
  name="blastoise-profile-$p"
  extra=()
  if [[ $p == storage ]]; then extra=(--local-ssd=interface=nvme); fi
  gcloud compute instances create "$name" --zone "$ZONE" \
    --machine-type "${MACHINE[$p]}" "${IMAGE_FLAGS[@]}" \
    --boot-disk-size=60GB --boot-disk-type="${DISK[$p]}" "${extra[@]}" \
    --metadata=enable-oslogin=false --quiet
done

setup='set -e
sudo apt-get update -qq
sudo apt-get install -y -qq postgresql-common python3-venv python3-pip >/dev/null
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y >/dev/null
sudo apt-get install -y -qq postgresql-17 >/dev/null
sudo systemctl stop postgresql || true
mkdir -p ~/blastoise && tar -C ~/blastoise -xzf ~/bundle.tar.gz
python3 -m venv ~/venv && ~/venv/bin/pip install -q "psycopg[binary]>=3.2"
WORK=$HOME/pgwork
if [ -e /dev/nvme0n1 ] && ! mountpoint -q /mnt/ssd; then
  sudo mkfs.ext4 -F /dev/nvme0n1 >/dev/null && sudo mkdir -p /mnt/ssd && sudo mount /dev/nvme0n1 /mnt/ssd && sudo chown $USER /mnt/ssd && WORK=/mnt/ssd/pgwork
fi
echo "$WORK" > ~/workdir'

for p in burstable compute storage; do
  name="blastoise-profile-$p"
  for i in $(seq 1 30); do
    if gcloud compute ssh "$name" --zone "$ZONE" --quiet --command "true" 2>/dev/null; then break; fi
    sleep 10
  done
  gcloud compute scp "$BUNDLE" "$name:~/bundle.tar.gz" --zone "$ZONE" --quiet
  gcloud compute ssh "$name" --zone "$ZONE" --quiet --command "$setup"
  gcloud compute ssh "$name" --zone "$ZONE" --quiet --command \
    "cd ~/blastoise && nohup ~/venv/bin/python artifacts/scripts/measure_profiles.py \
       --profile gce-$p --pg-bin /usr/lib/postgresql/17/bin --disk '${LABEL[$p]}' \
       --sizes '$SIZES' --work-dir \$(cat ~/workdir) --out ~/gce-$p.json > ~/measure.log 2>&1 &"
  echo "started $name"
done

# Poll for completion (the script prints 'done in' as its last line).
pending=(burstable compute storage)
while ((${#pending[@]})); do
  sleep 120
  still=()
  for p in "${pending[@]}"; do
    name="blastoise-profile-$p"
    if gcloud compute ssh "$name" --zone "$ZONE" --quiet --command "grep -q 'done in' ~/measure.log" 2>/dev/null; then
      gcloud compute scp "$name:~/gce-$p.json" "$OUT_DIR/gce-$p.json" --zone "$ZONE" --quiet
      gcloud compute scp "$name:~/measure.log" "$OUT_DIR/gce-$p.log" --zone "$ZONE" --quiet
      gcloud compute instances delete "$name" --zone "$ZONE" --quiet
      echo "collected $p"
    else
      still+=("$p")
    fi
  done
  pending=("${still[@]}")
done
echo "all profiles collected into $OUT_DIR"
