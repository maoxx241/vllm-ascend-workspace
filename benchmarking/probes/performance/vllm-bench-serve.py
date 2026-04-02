from __future__ import annotations

import argparse
import subprocess


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset-name", required=True)
    parser.add_argument("--num-prompts", type=int, required=True)
    parser.add_argument("--random-input-len", type=int, required=True)
    parser.add_argument("--random-output-len", type=int, required=True)
    parser.add_argument("--result-json", required=True)
    args = parser.parse_args()
    command = [
        "vllm",
        "bench",
        "serve",
        "--backend",
        "openai-chat",
        "--endpoint",
        "/v1/chat/completions",
        "--base-url",
        args.base_url,
        "--model",
        args.model,
        "--dataset-name",
        args.dataset_name,
        "--num-prompts",
        str(args.num_prompts),
        "--random-input-len",
        str(args.random_input_len),
        "--random-output-len",
        str(args.random_output_len),
        "--save-result",
        "--result-filename",
        args.result_json,
    ]
    subprocess.run(command, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
