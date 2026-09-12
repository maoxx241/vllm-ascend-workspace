# Business input example

This is an illustrative configuration shape. Select cases and values for the
actual task. The tool generates report metadata internally; observed output
files are produced by the relevant execution or measurement harness.

Each role parses its vLLM arguments when its managed process starts. The
topology does not run a separate parse-only import before launch. Coordinator
still owns group preparation, resource admission and execution cleanup.

```json
{
  "group_id": "pd-group",
  "connector": {
    "type": "mooncake",
    "options": {
      "port": 5000
    }
  },
  "services": [
    {
      "name": "decode",
      "role": "decode",
      "model": "/models/example",
      "tp": 1,
      "args": [
        "--kv-transfer-config",
        "{\"kv_role\":\"kv_consumer\"}"
      ]
    },
    {
      "name": "prefill",
      "role": "prefill",
      "model": "/models/example",
      "tp": 1,
      "args": [
        "--kv-transfer-config",
        "{\"kv_role\":\"kv_producer\"}"
      ]
    }
  ],
  "startup_order": [
    "decode",
    "prefill"
  ],
  "proxy": {
    "base_url": "http://proxy:9000",
    "health_path": "/health"
  },
  "smoke": {
    "path": "/v1/chat/completions",
    "request": {
      "model": "example",
      "messages": [
        {
          "role": "user",
          "content": "hello"
        }
      ]
    }
  }
}
```
