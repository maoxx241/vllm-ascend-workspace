# Business input example

This is an illustrative configuration shape. Select cases and values for the
actual task. The tool generates report metadata internally; observed output
files are produced by the relevant execution or measurement harness.

```json
{
  "expected_world_size": 2,
  "ranks": [
    {
      "global_rank": 0,
      "node": "host-a",
      "device": 0,
      "local_rank": 0,
      "tp_rank": 0,
      "pp_rank": 0,
      "dp_rank": 0,
      "ep_rank": 0,
      "pcp_rank": 0,
      "dcp_rank": 0
    },
    {
      "global_rank": 1,
      "node": "host-a",
      "device": 1,
      "local_rank": 1,
      "tp_rank": 1,
      "pp_rank": 0,
      "dp_rank": 0,
      "ep_rank": 0,
      "pcp_rank": 0,
      "dcp_rank": 0
    }
  ],
  "groups": [
    {
      "name": "tp-0",
      "type": "tp",
      "ranks": [
        0,
        1
      ]
    }
  ],
  "network_endpoints": [
    {
      "name": "master",
      "address": "10.0.0.1",
      "port": 29500
    }
  ],
  "process_tree": {}
}
```
