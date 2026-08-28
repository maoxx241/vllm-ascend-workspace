#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 --node-ip <ip> [--port <port>] [--interface <name>] [--stop-existing]"
}

node_ip=""
port=6379
network_interface="eth0"
stop_existing=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --node-ip)
            node_ip="${2:?--node-ip requires a value}"
            shift 2
            ;;
        --port)
            port="${2:?--port requires a value}"
            shift 2
            ;;
        --interface)
            network_interface="${2:?--interface requires a value}"
            shift 2
            ;;
        --stop-existing)
            stop_existing=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -z "$node_ip" ]]; then
    echo "--node-ip is required" >&2
    usage >&2
    exit 2
fi
if ! [[ "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
    echo "--port must be an integer from 1 to 65535" >&2
    exit 2
fi

export NCCL_DEBUG="${NCCL_DEBUG:-INFO}"
export NCCL_SOCKET_IFNAME="$network_interface"
export GLOO_SOCKET_IFNAME="$network_interface"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-0}"
export NCCL_NET_GDR_LEVEL="${NCCL_NET_GDR_LEVEL:-2}"
export NCCL_SHM_DISABLE="${NCCL_SHM_DISABLE:-0}"

if (( stop_existing )); then
    ray stop -f
fi

ray start \
    --head \
    "--node-ip-address=$node_ip" \
    "--port=$port" \
    --dashboard-host=0.0.0.0

printf '{"status":"started","role":"head","node_ip":"%s","port":%s,"interface":"%s"}\n' \
    "$node_ip" "$port" "$network_interface"
