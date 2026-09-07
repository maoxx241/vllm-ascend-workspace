#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 --container NAME [--dry-run]"
}

container=""
dry_run=0
while (($#)); do
    case "$1" in
        --container)
            container="${2:-}"
            shift 2
            ;;
        --dry-run)
            dry_run=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
done

if [[ ! "$container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    echo "Invalid or missing container name" >&2
    exit 2
fi

running=$(docker inspect --type container --format '{{.State.Running}}' "$container")
if [[ "$running" != "true" ]]; then
    echo "Container is not running: $container" >&2
    exit 1
fi

log_path="/root/ascend/log"
if ! docker exec "$container" test -d "$log_path"; then
    printf '{"status":"ok","container":"%s","path":"%s","cleared":false,"reason":"missing"}\n' \
        "$container" "$log_path"
    exit 0
fi

resolved=$(docker exec "$container" readlink -f "$log_path")
if [[ "$resolved" != "$log_path" ]]; then
    echo "Refusing to clear unexpected resolved path: $resolved" >&2
    exit 1
fi

if ((dry_run)); then
    echo "Would clear entries under $container:$log_path" >&2
    docker exec "$container" find "$log_path" -mindepth 1 -maxdepth 1 -print >&2
    printf '{"status":"ok","container":"%s","path":"%s","dry_run":true,"cleared":false}\n' \
        "$container" "$log_path"
    exit 0
fi

docker exec "$container" find "$log_path" -mindepth 1 -maxdepth 1 \
    -exec rm -rf -- '{}' '+'
printf '{"status":"ok","container":"%s","path":"%s","cleared":true}\n' \
    "$container" "$log_path"
