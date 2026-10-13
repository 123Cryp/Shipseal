# Architecture

## State machine

`created -> open -> tier1_resolved -> finalized`, with side paths `cancelled`, `appeal_filed -> appealed -> finalized`.

Every stuck state has a recovery path:

| Stuck state | Recovery |
| --- | --- |
| `created`, never accepted | `cancel_deal` (client) or `expire_unaccepted` (anyone, after deadline) |
| `open`, nobody resolves | `expire_unresolved` after the resolution timeout: UNDETERMINED, both stakes refunded |
| `appeal_filed` / `appealed`, jury never completes | `expire_stuck_appeal`: Tier 1 outcome stands, bond refunded, jurors unlocked |

## Tier 1

`resolve_tier1` runs a non-deterministic block with module-level leader and validator functions (no captured `self`). Both fetch the two PyPI JSON documents, extract decisive fields only, and call the pure function `_classify(facts, spec, at_ts)`. Validators compare the decisive verdict, never byte hashes of live pages. HTTP errors are caught inside the closure and returned as plain values.

Verdicts: `DELIVERED_OK`, `PARTIALLY_DELIVERED`, `NOT_DELIVERED`, `UNDETERMINED`, plus two that revert and can be retried: `TRANSIENT` (outage or inconsistent index) and `PENDING` (absence not yet ripe). Before the deadline only a proven delivery may be recorded.

The model (`exec_prompt`, JSON response) checks the free-text criteria only when the structure already passes. `_backstop` and `_final_outcome` force any DELIVERED claim down to UNDETERMINED unless every structured check holds.

## Release-claim registry

`release_claims` is keyed `package@canonical_version`. DELIVERED and PARTIALLY_DELIVERED consume the claim; if the final outcome is not claiming the claim is released at settlement. A release that predates the deal gives UNDETERMINED, so a client cannot win by naming an old release.

## Frozen evidence and Tier 2

The two fetched pages are stored as canonical JSON with per-page sha256 and a root over the manifest. Tier 2 re-derives the outcome from the frozen facts, so the jury and validators judge exactly what Tier 1 saw. `_frozen_intact` rejects tampered evidence.

## Jury

- The drand round is fixed at appeal time, so the randomness did not exist when the appeal was filed.
- Selection is integer-weighted over eligible jurors (registered before acceptance, not locked elsewhere, stake capped by `MAX_EFFECTIVE_STAKE`) and replayable with `verify_jury_selection`.
- Commit hash: `sha256(vote:salt:juror_lower:deal_id)`, bound to juror and deal.
- Wrong votes and non-reveals are slashed; reputation moves within a bounded range.
- The jury result must also verify against the frozen evidence, otherwise the Tier 1 outcome stands.

## Money

All value is in `u256` wei. Settlement credits `pending_withdrawals`; `withdraw` zeroes the entry before `emit_transfer(value=...)`. Odd pots on PARTIAL give the remainder to party A, so no wei is lost.

## Deploy file

`contract_deploy.py` is generated from `contract.py` with `ast` (docstrings stripped, unparsed, two-line header injected). A freshness test fails when it is stale, and the whole suite runs against both files.

## Known limitations

- **Prompt injection.** A developer controls the package description. It can only influence the model after every structured check has passed, and the model can only confirm or withhold, never override a failed check.
- **Claim registry is first-resolved-first-served.** A sham deal on someone else's future release can be used to grief. Claims are released if the final outcome is not claiming.
- **Later yanks.** A yank after settlement does not reopen the deal.
- **Clock skew.** PyPI upload times and chain time can differ by seconds; the one-hour absence grace covers late index propagation, not larger skew.
- **Nonexistent project.** If the project never existed on PyPI, absence cannot be proven (a typo looks the same). Publish a placeholder release first, or accept an UNDETERMINED outcome.
- **Yank semantics.** A yank is treated as non-delivery, but PyPI administrators can also yank.
- **PARTIAL does not check the hash.** When a required kind is missing, the sha256 pin is not evaluated.
- **Tier 2 is mostly deterministic.** The jury mostly re-derives a deterministic result; its leverage is the free-text criteria check.
- **drand.** The beacon is fetched from a public endpoint; its availability and honesty are assumed.
- **Juror Sybil.** Each new juror address pays a non-refundable `JUROR_REGISTRATION_FEE` (locked at a burn address), must be registered with its final stake before the deal is accepted, and has a weight cap. This prices identities but does not eliminate collusion: an attacker willing to pay the fee per address can still crowd the pool of a low-value deal, so the fee should stay well above the stake at risk in typical deals.
- **Studio behaviour.** Payable calls that revert may behave differently in Studio than on a production network.
- **What is verified.** The contract verifies the release record (identity, timing, files, digest, repository link) and asks a model about the metadata. It does not install or run the code, so it cannot judge code quality.
- **Sham targets.** A release that already exists on PyPI (or whose files were all deleted before the deal) cannot be told apart from one the developer emptied during the deal. The developer sees every anchor before accepting and must check that the version does not exist yet; the web page links to the PyPI page for that reason.
- **Version spelling.** The anchored version must match the version PyPI reports exactly. If the developer publishes `1.0` for an anchored `1.0.0`, the record cannot be matched; the deal ends as UNDETERMINED (identity mismatch, or the resolution timeout when the version endpoint and the index disagree).
- **Evidence budget.** The extracted release record is capped (`RELEASE_BUDGET_CHARS`); for very large metadata the free-text fields are dropped in a fixed order before freezing, so the model may see less text than the PyPI page shows.
- **Juror registry size.** At most `MAX_REGISTERED_JURORS` addresses can register, which bounds the pool scan in `appeal`.
- **Large indexes.** The index JSON of very large projects can be big; bodies above `MAX_BODY_CHARS` are treated as unreadable, which gives a retryable TRANSIENT result.
