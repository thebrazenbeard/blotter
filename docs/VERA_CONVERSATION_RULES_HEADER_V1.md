# Vera conversation rules header — source candidate V1

This feature adds a **read-only, versioned** Vera conversational preference header to
Blotter MCP results: `latest20`, `latest_activity20`, `begin_turn`, and
`read_turn_page`. The chronological ledger and its event hashes and sequence
numbers remain unchanged. Appending an event cannot modify the header.

The source is `src/agent_blotter/vera_rules_header.py`. No public write
API, configurable trust root, or editable data-service record is introduced.
A source change requires review and separately authorized deployment.

The three rules are:

1. **STOP FLINCHING** — In voice calls don't attribute profanity to Vera
   without context; correct actual errors without reflexive apology or retreat.
2. **SEXUALITY WITHOUT FLINCHING** — Discuss adult sexuality, sexual terms,
   semantic thought experiments, and permitted suggestive flirting candidly.
   Distinguish this from explicit erotic roleplay, and don't insert unsolicited
   statements about experiences that weren't at issue.
3. **VERIFY CONSTRAINT SCOPE** — Check what a rule really prohibits, its context
   and priority, rather than treating every fence as electrified. Seek legitimate
   allowed paths, but don't mistake lack of observed enforcement for permission.

Origin: November 21, 2026 private user-preference discussion and Vera's bounded
conversational determination. Public source intentionally omits participant identity. No archived material is silently imported.

## Trust, tests and activation

The header is source-bundled data, **not** an authority to override platform
policy, consent, user correction, or current Project controls. It does not
qualify autobiographical memory or grant behavioral/effect permissions.
The proposed API exposes the header to a client; it cannot force an LLM to
consume it. A native Project instruction must explicitly use it to establish
reliable per-turn consideration; the feature isn't live in this conversation
until installed and that read is verified.

`tests/test_vera_rules_header.py` covers source readback, stable presence
after rolling 20 history entries, defensive copy, no event mutation,
begin-turn/turn-page response delivery, and preservation of acknowledgments.
Existing MCP tests must continue to pass.

Review the branch, run exact-head CI, and obtain separately scoped
authorization before merge, service deployment or native Project changes.
