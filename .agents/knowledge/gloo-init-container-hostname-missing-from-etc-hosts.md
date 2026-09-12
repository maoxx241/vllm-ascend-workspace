# vLLM service on fresh VAWS containers fails gloo/HCCL init because /etc/hosts lacks the container hostname

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-03. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Avoidance

Do not debug gloo/HCCL env vars (GLOO_SOCKET_IFNAME etc.) before checking /etc/hosts; the hostname mapping is the root cause in this container family.

## Search terms

- gloo makedeviceforhostname
- name or service not known hostname
- torch.distributed gloo init failed container

## Resolution

Add '127.0.0.1 <hostname>' (hostname from `hostname`) to /etc/hosts inside the container before starting vllm serve. vllm-ascend-serving / session bootstrap should enforce this on every new container before first serve_start.

## Root cause

Fresh session containers do not map their own hostname in /etc/hosts. PyTorch's gloo store/backend resolves the local hostname at init and fails when it does not resolve to an address.

## Symptom

vllm serve crashes or hangs during distributed init with gloo backend errors mentioning the container hostname (e.g. 'Name or service not known' / gloo::makeDeviceForHostname), even on single-node TP>1.

## Recorded context

- soc: A3.
- topology: tp2, tp4, tp8, tp16.
- component: service-bootstrap, gloo-distributed-init.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: gloo-init-container-hostname-missing-from-etc-hosts; first observed: 2026-09-03.
