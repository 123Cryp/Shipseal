# ShipSeal

A GenLayer Intelligent Contract that settles disputes over a software-delivery contract using the public PyPI release record as evidence.

A client (party A) and a developer (party B) both stake GEN against one exact release: a PyPI package, an exact version, optional sha256, required artifact kinds (wheel and/or sdist), an expected source repository, a deadline, and a single allowed evidence host (`pypi.org`). All of it is recorded on-chain at creation, before any dispute exists.

## How an outcome is decided

1. **Structured proof, parsed in code.** Validators fetch `https://pypi.org/pypi/<package>/<version>/json` and check: name and version match, not yanked, upload time inside `[creation, deadline]`, every required artifact kind present, sha256 matches when pinned, project URLs match the agreed repository. A release with no repository link, or whose files were all deleted by the deadline plus grace, counts as non-delivery.
2. **Absence is evidence.** After the deadline plus a one-hour grace period, a 404 on the version endpoint counts as non-delivery only if the project index endpoint also loads, names the project and lacks the version. A typo or an outage can never produce NOT_DELIVERED.
3. **Model only confirms.** Free-text delivery criteria are checked by a model only after every structured check passes. A code backstop downgrades any DELIVERED claim that the structure does not support.
4. **Frozen evidence.** The two fetched pages are stored with a sha256 root. Appeals judge that frozen evidence, never a fresh fetch.
5. **Appeal.** A staked commit-reveal jury is drawn with a drand round fixed at appeal time. Commits are bound to juror and deal. Wrong and non-revealing jurors are slashed.
6. **Pull payments.** Settlement only credits balances; `withdraw` zeroes the ledger before transferring.

Outcomes: `DELIVERED` (developer gets the pot), `NOT_DELIVERED` (client gets the pot), `PARTIALLY_DELIVERED` (published on time but a required artifact kind is missing; split evenly), `UNDETERMINED` (each side gets its own stake back). One (package, version) release can settle only one deal.

## Layout

| Path | Purpose |
| --- | --- |
| `contract.py` | Documented source of truth |
| `contract_deploy.py` | Generated, comment-free file to deploy (two-line header) |
| `tools/make_deploy.py` | Regenerates the deploy file with `ast`; `--check` verifies freshness |
| `index.html` | Single-file frontend (genlayer-js, hash routing) |
| `tests/` | Offline suite with a self-written SDK stub |

## Tests

No dependencies:

```
python3 -m unittest discover -s tests
SS_CONTRACT=$PWD/contract_deploy.py python3 -m unittest discover -s tests
python3 -I tools/make_deploy.py --check
```

After every edit of `contract.py` run `python3 -I tools/make_deploy.py` to regenerate the deploy file.

## Deploy

Deploy the full contents of `contract_deploy.py` in GenLayer Studio with no constructor arguments. Then open `index.html?contract=0x<address>` or set `DEFAULT_CONTRACT` in `index.html`.

See `ARCHITECTURE.md` for the design and the known limitations.

## License

MIT
