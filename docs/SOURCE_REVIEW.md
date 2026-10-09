# Source-material review (retrieved 2026-10-09)

I inspected current GitHub metadata/file trees and selected README, API, design and implementation files for each repository below. This is a mechanism audit, not a complete line-by-line forensic audit, installation or claim of runtime compatibility. No code is copied. Links point to exact retrieved heads.

| Reference | Finding | Blotter decision |
|---|---|---|
| Delgan/loguru @ 0412a60d6aff63a4ed009c7445993b42d294c2f1 | Easy Python structured logging, binding context, levels, sinks | Adapt easy record API and context. Do not build another console logger. |
| madzak/python-json-logger @ be42d82ebd1b3276b739a314545f7a3f518ff222 | Standard logging JSON output, extra fields, custom serialization; upstream retired in favor of nhairs fork | Adapt machine-readable structured export; no dependency. |
| mehulj94/Radium @ ad1b998a5ca342d5dcedff3b65f1133095ee66e4 | Windows keylogging, credential extraction, screenshots and outward transfer despite misleading description | Reject all mechanisms; activity logging must be explicit, not surveillance. |
| tmuth/Logger---A-PL-SQL-Logging-Utility @ 2bbc1b25af2205434948af5e665035df839e6680 | Scope, params, client logging levels, timing; old repo points to OraOpenSource | Adapt typed scope and metadata; Oracle-specific integration unnecessary. |
| 3kh0/echolog @ 34c509df545169e4debc240e880e2ca24f23f5bc | Captures IP, device, browser and location fingerprint; warns about consent | Reject unsolicited capture, keep as privacy counterexample. |
| daroczig/logger @ 3c3f3aa7a8b20601e20c5d184793ad62bde4c9fe | R threshold/formatter/layout/appender pipeline | Adapt record/view separation and severity metadata; no new appender framework. |
| winstonjs/winston @ ff0b79de8562bb322c390fbc82fe71c11f373428 | Node loggers, metadata, composable formatting and transports | Adopt structured event format, not transport-routing responsibilities. |
| thebrazenbeard/tattler @ d091584cf1bb514dcf1ba786ed16f705fabf807d | Versioned typed sampled observations and a recovery-aware JSONL event journal | Preserve observation/provenance distinction; future adapter only. |
| BigCactusLabs/blotter @ ccadce7dcea3291d0b33c8ce59af8c1218018e1c | Append-only repo-local agent-friction record, corrective follow-through, explicit finding promotions | Useful append-only precedent; user goal differs: **all** activity, not selected friction or promoted findings. No separate promotion channel. |
| uogbuji/Anya @ 411886e4558ddfdbe3e1ba630696a68c579fc2d1 | Readable append-only job blotter, lock-based writers, controller-owned effects | Adapt per-agent activity descriptions and concurrency control. |
| thebrazenbeard/portal @ 7d00d8bf3b48c0676b2278359838ee563c66c67f | Currentness, authority, durable receipts, and independent verification separated from dispatch | Keep activity record separate from real execution/verification/authorization. |
| thebrazenbeard/ccb-core @ 0b542a78ebf5827a518e0731a63f17a7ac8a2727 | Identity normalization, versioned envelopes, safe idempotency, causal predecessor rules | Useful fields and invariants; not an architectural foundation for Blotter communication. |
| thebrazenbeard/chat-communication-bus @ e0bcb5eb18630693de55a1af2066411c7af079bb | Durable writer lanes, routing, acknowledgments, and projection machinery; each lane fragments common awareness | Blotter is designed to replace **multiple communication lanes** with a **single activity ledger**, not re-create messaging. |

All reference URLs are of the form https://github.com/<owner>/<repo>/tree/<head-sha>. None is an installed or runtime dependency.
