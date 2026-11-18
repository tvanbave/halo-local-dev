# ESP and Pi pairing comparison — dev62

Reviewed 2026-11-17 against the local ESP hub and recsrmesh observer/ACK-validation source revisions recorded in PROVENANCE.json. This is a source and synthetic-test comparison, not a claim that every model or the currently running ESP firmware has passed new hardware tests.

## Findings

| Layer | ESP | Pi | Decision |
| --- | --- | --- | --- |
| Pairing exchange | Shared MASP state machine, not one protocol per model | Same 12-request sequence | Keep the shared implementation. Controller button programming and wake handling are separate steps. |
| Cryptography | SECP192R1; public Y then X, little-endian; confirmation HMAC; encrypted address and network key | Same ordering and calculations, exercised with an independent synthetic peer | No cryptographic change. |
| GATT writes | Short packets on HIGH; long packets split LOW 20 bytes then HIGH; writes without response | Same split and write mode, serialized with a lock | No added retransmissions or speculative pacing. A submitted write is not a device acknowledgement. |
| Complete short packet on LOW | Timer delivers pending LOW after 200 ms | Previously discarded | Restore timed delivery. Preserve original receive time; cancel on disconnect, replacement and queue failure. Authentication still happens above transport. |
| LOW followed by HIGH | Assemble within the 200 ms receive window | Assemble within the same window; reject expired splicing | Keep the bounded window. Poll before a new notification if the timer was delayed. |
| Duplicate / foreign replies | Older source has weaker ACK correlation; reviewed ACK-validation revision tightens it | Target hash, expected phase/chunk, authentication and queue filtering | Keep Pi checks; do not accept unrelated ACKs to imitate the older parser. |
| Reply deadlines | Hub uses 30 seconds overall | 5 seconds for early replies, 15 for address/key replies, 60 overall | Difference retained pending measured late replies. Longer waits cannot repair a lost fragment. |
| Initial request version byte | Incrementing low byte of ESP's process-local counter | Fixed 1 | Documented difference, unchanged in this patch. The observed failure progressed through several public-key replies, so the initial request was answered. Counter semantics and repeated-claim behavior need separate evidence before changing it. |
| Acceptance | ESP hub adds inventory at claim start and reports protocol completion | Pi commits after final protocol ACK and authenticated Ping; preserves uncertain journal | Keep durable Pi verification and recovery. Distinguish pairing from subsequent controller setup. |
| Discovery results | ESP presents advertised candidates | Pi used to require a hit in the final scan round | Corrected in dev61: five-minute actual-observation window plus backend fresh exact-UUID check. |

## What this fixes and what remains unproven

Dev62 fixes a demonstrated packet-delivery difference that can lose short association responses and address/key acknowledgements. A public-key reply is longer than 20 bytes and needs both fragments, so this fix alone does **not** establish the cause of a missing public-key chunk.

New per-attempt diagnostics retain only stage/chunk, elapsed write/receive/processing times, and an allowlisted set of transport-counter differences. They contain no packet contents, private keys, network keys or passphrases. They separate a missing reply from incomplete fragments, queue loss or write failures. No uncertain association or reset is repeated automatically.

## Offline verification

- Compiled the original recsrmesh `mtl.cpp` and compared six synthetic notification traces directly against the Pi assembler: all matched.
- Full installed-package suite: 1,533 tests, 1,525 passed and eight expected skips.
- Added ten MTL tests, including a real timer, original receive timestamps, complete 12-request cryptographic exchanges with short replies on LOW, fragmented public-key replies, duplicate/replaced fragments, queue overflow and reconnect isolation.
- Strengthened existing successful and interrupted association tests to check bounded payload-free diagnostics.
- No browser behavior changed in dev62; dev61's Chromium and WebKit suites remain the browser baseline.

## Next physical check

Perform one owner-initiated A19 pairing from the main Pi UI while observing the saved receipt. If it fails, inspect the stage timings and LOW/HIGH counters before choosing any further change. Do not reset, automatically retry or relax acknowledgement matching. Test discovery and pairing on other controller types afterward; do not infer their wake/programming behavior from the A19 result.
