# Qwen3.5 GDN Benchmark Runbook

## Scope

This runbook defines the initial public benchmark preset used by the real E2E acceptance flow.

## Preset

- preset name: `qwen3-35b-tp4`
- model path: `/home/weights/Qwen3.5-35B-A3B`
- tensor parallel size: `4`
- output markers:
  - `JSON_RESULTS_BEGIN`
  - `JSON_RESULTS_END`
  - `MARKDOWN_ROWS_BEGIN`
  - `MARKDOWN_ROWS_END`

## Standard Remote Environment

Run from the workspace runtime with:

```bash
cd /vllm-workspace/workspace
source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
source /vllm-workspace/workspace/vllm-ascend/vllm_ascend/_cann_ops_custom/vendors/vllm-ascend/bin/set_env.bash
export PATH=/usr/local/python3.11.14/bin:$PATH
export PYTHON=/usr/local/python3.11.14/bin/python3
export PIP=/usr/local/python3.11.14/bin/pip
export VLLM_WORKER_MULTIPROC_METHOD=spawn
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
```

## Result Extraction

Keep logs on the remote machine and extract only the marker block:

```bash
sed -n '/MARKDOWN_ROWS_BEGIN/,/MARKDOWN_ROWS_END/p' /tmp/qwen3-35b-tp4.out
```
