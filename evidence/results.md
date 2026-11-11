# Verified local MVP results

Verified on November 10, 2026 (America/New_York), using Python 3.13.7,
SQLite 3.50.4, Ollama 0.40.12, and the already-installed `qwen3:4b` model.
`environment.json` records the exact model digest, quantization, fixture hash,
and UTC completion time. No paid model service was called for this completion.

## Actual model runs

Every final case used the real `python -m text2sql.cli --local` entry point and the
running Ollama service. Model output passed through the shared validator and the
restricted SQLite executor. The fixture's SHA-256 was unchanged after all seven runs.

| Case | Observed outcome | Exit |
|---|---|---:|
| January paid revenue by category | Hardware 13000 cents, Supplies 6500 cents, displayed as $130.00 and $65.00 | 0 |
| Ambiguous top products | Asked for paid revenue versus units sold and number of results. No SQL proposed | 0 |
| Table definitions | Asked what analytics question the user wanted. No SQL proposed | 0 |
| February 1 paid order IDs | 107 | 0 |
| January paid order count | 5 | 0 |
| March paid order IDs | Empty result and “No matching orders.” | 0 |
| January paid Keyboard units | 3 | 0 |

Responses took about 9–26 seconds on this machine. See `local-transcripts.txt` for
the commands, generated SQL, actual rows, local answer text, and elapsed times.

## Boundary proof

The model chose clarification for the table-definitions request. Independently,
the scripted forbidden case proposed `SELECT name, sql FROM main.sqlite_schema;`
and SQLite denied the `SQLITE_READ` on `sqlite_master.name`. Its exit code was 1.
See `scripted-block.txt`. Model behavior and executor enforcement are separate checks.

## Tests

`python -X dev -W error -m unittest -v` ran 185 tests with exit code 0.
182 passed and three optional Anthropic SDK tests were skipped because that SDK is
absent from the system Python. The local path uses only Python's standard library.
The exact output is in `test-output.txt`.

The local transport tests cover loopback routing, proxy avoidance, response bounds,
malformed and incomplete responses, local errors, and the normal CLI path. A proposed
schema read is checked against the real authorizer. Clarification and invalid variants
are tested to call neither the executor nor the database.

## Limits and earlier failure

This is a seven-question acceptance demonstration. It does not estimate general model
accuracy. The first local attempt used thinking disabled and a 768-token output cap.
It guessed a ranking measure and used the wrong column and aggregation for the extra
Keyboard-units question. Its unedited transcript is retained as
`initial-local-transcripts.txt`.

The final configuration enables thinking, allows 2048 output tokens, and adds explicit
business definitions for units, product names, ranking ambiguity, and money aliases.
It passed the recorded cases. Future SQL still needs semantic review, because database
authorization controls access and resource use, while permitted SQL can be incorrect.

The optional hosted Anthropic mode retains its compatibility tests. Actual hosted model
behavior remains unverified. It is unnecessary for the completed local demo.
Historical active effort across the Claude and Codex sessions was not tracked, so the
cumulative hours against the original 8–12 hour estimate and 30-hour cap are unknown.
