# Contributing

Open an issue describing the concrete behavior before a large protocol change.
Keep UI, protocol and deployment changes reviewable independently.

Use Python 3.10+ (Docker uses 3.12) and Node 22+ for browser tests. Follow the README
commands. The complete Python suite runs in a container with `--network none` and
`AVIONMESH_ISOLATED_TESTS=1`; this enables the fake HTTP/MQTT wire tests. Browser
fixtures must intercept every request and abort external origins. Never point
CI at a real bridge, broker or Bluetooth adapter.

Keep fixtures synthetic. Do not include credentials, raw captures, home inventory,
private paths or state databases. Hardware evidence should record the model,
operation and observed result without installation identifiers.

For protocol/state changes, preserve single-writer ownership, monotonically
committed sequence allocation and journal-before-write ordering. Never replay an
uncertain setter automatically; retry reads only within existing admission rules.
Do not infer stored assignments or physical qualification from an observed press.

Keep upstream attribution and inspected source hashes when porting behavior.
Distinguish offline tests, broker delivery, HA processing and physical light
responses in reports. Include tests that exercise meaningful failure behavior.
