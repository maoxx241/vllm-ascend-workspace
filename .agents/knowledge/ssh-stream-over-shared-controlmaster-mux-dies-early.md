# ssh_stream through the shared ControlMaster mux fails on long-running remote commands; run stream/tunnel off the mux

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-07. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Avoidance

Do not retry a long-lived stream/tunnel on the mux hoping for a different result; check whether that command actually passes mux=False. Do not flip serving ssh_exec or parity stdin-upload helpers to mux=False — those are short round-trips and should keep the mux. Treat instant 'timeout' from MCP remote_bash as a tool-service fault signature, not a remote command fault.

## Search terms

- ssh_stream instant timeout controlmaster
- remote_bash instant timeout any command
- stream closed early mux

## Resolution

Use base_ssh_options(mux=False) only for long-lived stream/tunnel commands. The helpers that actually do this are .agents/skills/ascend-profiling-analysis/scripts/_common.py `_ssh_base_cmd` (ssh_stream) and .agents/skills/ascend-profiling-collection/scripts/_common.py `open_local_tunnel` (ssh -N -L), both from PR #70. remote-code-parity and vllm-ascend-serving still call base_ssh_options() with default mux=True for short captured SSH and stdin uploads; they were not switched off the mux. Fall back to direct 'ssh -o BatchMode=yes -p <port> root@<host>' when MCP remote_bash misbehaves.

## Root cause

The shared ControlMaster mux channel does not reliably carry long-lived stream sessions in this environment; stream-type invocations must bypass the mux (dedicated connection) instead of multiplexing over it.

## Symptom

Long-lived SSH streaming commands (service logs, analyse progress, tunnels) die or hang immediately / return early when issued over the shared ControlMaster connection; MCP remote_bash separately reported instant 'timeout' for any command while remote_probe/remote_ls kept working.

## Recorded context

- component: ssh-transport, controlmaster-mux.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: ssh-stream-over-shared-controlmaster-mux-dies-early; first observed: 2026-09-03.
