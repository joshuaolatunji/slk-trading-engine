# SLK tradebook and AI analysis package

Research version 1.0 - 3 October 2026. Covers forex, gold, NAS100 and JAP225.

## Files

- `SLK_TRADEBOOK.md`: complete methodology, source links, rules, risk policy and validation plan.
- `AGENT_INSTRUCTIONS.md`: reusable agent instruction prompt.
- `strategy_config.json`: explicit unvalidated research defaults and market profiles.
- `signal.schema.json`: machine-readable output contract.
- `examples/insufficient_data.json`: correct response when market inputs are absent.
- `examples/synthetic_qualified_setup.json`: format/calculation demonstration, clearly synthetic and not a current signal.
- `VALIDATION_CASES.md`: acceptance/falsification cases and worked arithmetic.
- `SLK_Tradebook.pdf`: readable edition of the full tradebook, supplied in the distribution archive.

## Ingest and run

1. Give the agent the tradebook, agent instructions, configuration and schema. Do not upload a PDF alone if text/JSON attachments are supported.
2. Supply read-only broker-specific market data, contract details, timestamps, calendar and account-risk snapshot. This package contains no live data or trading connection.
3. Ask the agent to validate inputs, apply B1/P1, and return JSON plus a brief rationale. The example prompt is in AGENT_INSTRUCTIONS.md.
4. Validate the output's JSON shape and its semantic invariants. JSON Schema cannot prove chart truth or arithmetic correctness by itself.
5. Start with replay and paper signals; evaluate each instrument/feed independently using the prescribed out-of-sample and prospective process.

## Status and limitations

This is an agent-ready research specification, not a runnable trading bot or a validated source of profitable signals. It distinguishes the reviewed educator concepts from added deterministic rules. All numeric settings are proposed defaults. B2 and P2 remain disabled until deliberately tested. No guarantee of signal accuracy is made.

Two 4thman transcripts were reviewed, alongside public practitioner material, official execution/data references, market calendars and research. Additional video access was blocked, including after retry; the tradebook lists the unresolved lessons. Nothing in sources/ was changed.

Broker-specific contract fields are intentionally null until provided. A null field is a missing operational input, not an invitation for the agent to guess. Without adequate inputs the required response is INSUFFICIENT_DATA.
