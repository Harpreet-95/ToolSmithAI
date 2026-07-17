# Enterprise Business Validation Suite

## What this is

The permanent Enterprise Acceptance Test Suite: the canonical, versioned corpus of
business questions used to validate ToolSmithAI's Intent Resolver and downstream SQL
pipeline against deterministic, expected outcomes. Every future milestone that touches
question classification, semantic resolution, SQL planning, SQL generation, or
answering **must** validate against this exact suite and report pass/fail per category.

**Status: LOCKED as of v1.0.0 (2026-07-13).**

## Provenance

`docs/ENTERPRISE_DELIVERY_PROGRAM.md` (milestones M-19, M-20) references a prior
"37-question Enterprise Business Validation Suite" / "Phase 6 Enterprise Business
Question Validation report." That original corpus was searched for across session
memory, generated artifacts, the full repository (including all `docs/*.md` files),
and complete git history (including deleted files) on 2026-07-13, and **could not be
located**. Only 8 individual questions survived, quoted inline inside M-19/M-20's own
re-validation tables.

**`enterprise_business_validation_suite.v1.json` is a newly authored replacement, not
a recovery of the original.** It was approved as a 45-question, 7-category framework,
then expanded to its locked 98-question, 26-category v1.0.0 form before freeze, per
explicit instruction. It is the permanent baseline going forward.

## Structure

- One JSON file per major version: `enterprise_business_validation_suite.v<major>.json`.
- 26 categories: **Counts, Lists, Aggregations, Time Intelligence, Filters,
  Relationships, Metadata, Sorting, Grouping, Distinct, Ranking, Top/Bottom N,
  Multiple Filters, Date Ranges, Cross-Domain Business Questions, Multi-Table Joins,
  Dictionary, Governance, Clarification Required, Ambiguous Business Concepts, Safe
  Refusals, Time Comparisons, Trend Questions, Percentage Calculations, Null Handling,
  Multi-Source Scenarios**.
- Every question has a stable `id` (`EBVS-<CATEGORY-PREFIX>-<NN>`), its own `category`,
  the literal `question` text, an `expected_intent` (the correct target `IntentType`
  value), and an `expected_outcome` (controlled vocabulary — see `expected_outcome_values`
  in the JSON: `SUCCESS`, `REFUSED_SAFE`, `AMBIGUOUS`, `CLARIFICATION_NEEDED`,
  `NOT_SUPPORTED`, `KNOWN_DEFECT`).
- `notes` documents whether an entry's `expected_outcome` is **EVIDENCE-BASED** (exact
  question text already reproduced against real, live CCPP data and documented in
  `ENTERPRISE_DELIVERY_PROGRAM.md`) or **ARCHITECTURAL-TARGET** (the correct outcome
  assuming adequately governed/disambiguated data — see `schema_notes.expected_outcome`
  in the JSON for the full policy). This distinction matters: several ids intentionally
  encode known, documented, currently-wrong or currently-blocked behavior rather than
  an idealized pass, so the suite stays honest instead of inflating a pass rate.

## Versioning / immutability policy

Now that v1.0.0 is locked:

- Existing entries (`id`, `question`, `expected_intent`, `expected_outcome`) are
  **immutable**. They are never edited, reworded, renumbered, or removed.
- New coverage is added only as a **new version file** (`v1.1.0`, `v2.0.0`, ...) —
  never as an in-place edit to this locked file.
- Each new version must state, in its own changelog entry below, exactly which ids were
  added and why.
- A category may only be removed by a major version bump with an explicit, documented
  rationale — never silently.

## Changelog

- **v1.0.0** (2026-07-13) — Locked. 45 originally-approved questions (7 categories)
  expanded to 98 questions across 26 categories before freeze, per explicit
  pre-lock expansion instruction. Authored to replace the unrecoverable original
  corpus (see Provenance). This is the permanent Enterprise Acceptance Test Suite.

## How to use this suite

1. Run every `question` through `core.orchestrator.intent_resolver.IntentResolver.resolve()`
   (or the relevant downstream stage — semantic resolution, SQL planning, SQL
   generation, answering — for later-pipeline milestones).
2. Compare the actual result's intent against `expected_intent`, and the actual
   pipeline outcome against `expected_outcome`.
3. Report pass/fail per category, not just an aggregate number.
4. Any question that fails must be explained — either the code has a real gap (fix it
   and re-validate), or the suite's own `expected_intent`/`expected_outcome` was wrong
   (raise it for review as a new versioned addition; do not silently edit this file).
