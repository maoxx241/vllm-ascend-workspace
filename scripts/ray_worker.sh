#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 --head-address <ip:port> --node-ip <ip> [--interface <name>] [--stop-existing]"
}

head_address=""
node_ip=""
network_interface="eth0"
stop_existing=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --head-address)
            head_address="${2:?--head-address requires a value}"
            shift 2
            ;;
        --node-ip)
            node_ip="${2:?--node-ip requires a value}"
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

if [[ -z "$head_address" || -z "$node_ip" ]]; then
    echo "--head-address and --node-ip are required" >&2
    usage >&2
    exit 2
fi

export NCCL_DEBUG="${NCCL_DEBUG:-INFO}"
export NCCL_SOCKET_IFNAME="$network_interface"
export GLOO_SOCKET_IFNAME="$network_interface"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-0}"
export NCCL_NET_GDR_LEVEL="${NCCL_NET_GDR_LEVEL:-2}"

if (( stop_existing )); then
    ray stop -f
fi

ray start \
    "--address=$head_address" \
    "--node-ip-address=$node_ip"

printf '{"status":"started","role":"worker","node_ip":"%s","head_address":"%s","interface":"%s"}\n' \
    "$node_ip" "$head_address" "$network_interface"
