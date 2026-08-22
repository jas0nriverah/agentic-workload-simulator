#!/usr/bin/env bash
set -Eeuo pipefail

# Safe-by-default creator for the single-GPU GCP pilot. It prints the exact
# command unless --apply is supplied, and never deletes resources.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
PROJECT="${GCP_PROJECT:-project-3d59272d-3213-4e06-97b}"
ZONE="${GCP_ZONE:-us-central1-a}"
NAME="${GCP_INSTANCE_NAME:-agentic-h100-spot}"
IMAGE=""
IMAGE_PROJECT=""
SERVICE_ACCOUNT=""
GCS_URI=""
BOOT_DISK_SIZE="${GCP_BOOT_DISK_SIZE_GB:-200}"
DATA_DISK_SIZE="${GCP_DATA_DISK_SIZE_GB:-250}"
MANIFEST_OUTPUT=""
APPLY=0

usage() {
  cat <<'USAGE'
Usage: create_h100_spot.sh [options]

Required for --apply:
  --image IMAGE              Immutable GPU-optimized image name
  --image-project PROJECT    Project containing IMAGE

Options:
  --project PROJECT          GCP project (default: project-3d59272d-3213-4e06-97b)
  --zone ZONE                A3 zone (default: us-central1-a)
  --name NAME                VM name (default: agentic-h100-spot)
  --service-account EMAIL    Optional least-privilege VM service account
  --gcs-uri URI              Optional artifact bucket prefix
  --boot-disk-gb N           Persistent boot disk size (default: 200)
  --data-disk-gb N           Persistent data disk size (default: 250)
  --manifest-output PATH     Write gcloud describe JSON outside the repo
  --apply                    Create the VM after validation
  --dry-run                  Explicitly print without creating (default)
USAGE
}

while (($#)); do
  case "$1" in
    --project) [[ $# -ge 2 ]] || { echo '--project requires a value' >&2; exit 2; }; PROJECT="$2"; shift 2;;
    --project=*) PROJECT="${1#*=}"; shift;;
    --zone) [[ $# -ge 2 ]] || { echo '--zone requires a value' >&2; exit 2; }; ZONE="$2"; shift 2;;
    --zone=*) ZONE="${1#*=}"; shift;;
    --name) [[ $# -ge 2 ]] || { echo '--name requires a value' >&2; exit 2; }; NAME="$2"; shift 2;;
    --name=*) NAME="${1#*=}"; shift;;
    --image) [[ $# -ge 2 ]] || { echo '--image requires a value' >&2; exit 2; }; IMAGE="$2"; shift 2;;
    --image=*) IMAGE="${1#*=}"; shift;;
    --image-project) [[ $# -ge 2 ]] || { echo '--image-project requires a value' >&2; exit 2; }; IMAGE_PROJECT="$2"; shift 2;;
    --image-project=*) IMAGE_PROJECT="${1#*=}"; shift;;
    --service-account) [[ $# -ge 2 ]] || { echo '--service-account requires a value' >&2; exit 2; }; SERVICE_ACCOUNT="$2"; shift 2;;
    --service-account=*) SERVICE_ACCOUNT="${1#*=}"; shift;;
    --gcs-uri) [[ $# -ge 2 ]] || { echo '--gcs-uri requires a value' >&2; exit 2; }; GCS_URI="$2"; shift 2;;
    --gcs-uri=*) GCS_URI="${1#*=}"; shift;;
    --boot-disk-gb) [[ $# -ge 2 ]] || { echo '--boot-disk-gb requires a value' >&2; exit 2; }; BOOT_DISK_SIZE="$2"; shift 2;;
    --boot-disk-gb=*) BOOT_DISK_SIZE="${1#*=}"; shift;;
    --data-disk-gb) [[ $# -ge 2 ]] || { echo '--data-disk-gb requires a value' >&2; exit 2; }; DATA_DISK_SIZE="$2"; shift 2;;
    --data-disk-gb=*) DATA_DISK_SIZE="${1#*=}"; shift;;
    --manifest-output) [[ $# -ge 2 ]] || { echo '--manifest-output requires a path' >&2; exit 2; }; MANIFEST_OUTPUT="$2"; shift 2;;
    --manifest-output=*) MANIFEST_OUTPUT="${1#*=}"; shift;;
    --apply) APPLY=1; shift;;
    --dry-run) APPLY=0; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2;;
  esac
done

[[ "$PROJECT" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{4,62}$ ]] || { echo 'invalid project id' >&2; exit 2; }
[[ "$ZONE" =~ ^us-central1-[a-cf]$ ]] || { echo 'zone must be us-central1-a, -b, -c, or -f' >&2; exit 2; }
[[ "$NAME" =~ ^[a-z]([-a-z0-9]*[a-z0-9])?$ ]] || { echo 'invalid instance name' >&2; exit 2; }
[[ "$BOOT_DISK_SIZE" =~ ^[2-9][0-9]{2,}$ ]] || { echo 'boot disk must be at least 200 GB' >&2; exit 2; }
[[ "$DATA_DISK_SIZE" =~ ^[2-9][0-9]{2,}$ ]] || { echo 'data disk must be at least 250 GB' >&2; exit 2; }
if (( APPLY )); then
  [[ -n "$IMAGE" && -n "$IMAGE_PROJECT" ]] || { echo '--image and --image-project are required with --apply' >&2; exit 2; }
  command -v gcloud >/dev/null 2>&1 || { echo 'gcloud is required with --apply' >&2; exit 1; }
  gcloud auth list --filter=status:ACTIVE --format='value(account)' | awk 'NF { found=1 } END { exit !found }' || { echo 'no active gcloud account' >&2; exit 1; }
fi

shutdown_script="$ROOT/cloud/gcp/preemption_shutdown.sh"
[[ -x "$shutdown_script" ]] || { echo "shutdown script is not executable: $shutdown_script" >&2; exit 1; }
data_disk="${NAME}-data"
cmd=(
  gcloud compute instances create "$NAME"
  --project="$PROJECT"
  --zone="$ZONE"
  --machine-type=a3-highgpu-1g
  --provisioning-model=SPOT
  --maintenance-policy=TERMINATE
  --instance-termination-action=STOP
  --no-restart-on-failure
  --image="$IMAGE"
  --image-project="$IMAGE_PROJECT"
  --boot-disk-type=pd-balanced
  --boot-disk-size="${BOOT_DISK_SIZE}GB"
  --create-disk="name=${data_disk},device-name=${data_disk},size=${DATA_DISK_SIZE}GB,type=pd-balanced,auto-delete=no,boot=no,mode=rw"
  --metadata-from-file="shutdown-script=$shutdown_script"
  --metadata="EIC_WORK_ROOT=/mnt/eic-work,EIC_GCS_URI=$GCS_URI"
  --tags=eic-h100
  --scopes=https://www.googleapis.com/auth/cloud-platform
)
[[ -n "$SERVICE_ACCOUNT" ]] && cmd+=(--service-account="$SERVICE_ACCOUNT")

printf 'DRY-RUN: no VM will be created unless --apply is supplied.\n'
printf 'DRY-RUN:'
printf ' %q' "${cmd[@]}"
printf '\n'
printf 'DRY-RUN: expected shape=a3-highgpu-1g, 1x H100 80GB, Spot, zone=%s\n' "$ZONE"

if (( ! APPLY )); then
  exit 0
fi

"${cmd[@]}"
if [[ -n "$MANIFEST_OUTPUT" ]]; then
  mkdir -p -- "$(dirname -- "$MANIFEST_OUTPUT")"
  gcloud compute instances describe "$NAME" --project="$PROJECT" --zone="$ZONE" --format=json >"$MANIFEST_OUTPUT"
  printf 'instance manifest: %s\n' "$MANIFEST_OUTPUT"
fi
