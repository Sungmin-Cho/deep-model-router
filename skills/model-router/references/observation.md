# RouteObservationV1

`RouteObservationV1` is the schema and validator contract owned by `deep-model-router`; orchestrators emit observation records, while this plugin validates them and does not emit, store, aggregate, or route observations. Invoke it with:

```
python3 "$SKILL_DIR/scripts/validate_observation.py" --file <obs.json> --root <dir>
python3 "$SKILL_DIR/scripts/validate_observation.py" --file <obs.json> --root <dir> --check-refs
python3 "$SKILL_DIR/scripts/validate_observation.py" --file <obs.json> --root <dir> \
    --check-refs --check-receipts <receipt-dir>
```

Exit `0` means valid, `1` means the record violates an invariant, and `2` means invalid usage. `--check-receipts` requires `--check-refs`. The validator enforces I-JSON, I-STRUCT, I-CONTRACT, I-ACCEPTED, I-OWNER, I-NO-RAW-KEYS, I-STRING, I-NO-DIFF, I-SIZE, I-SUBJECT, I-GRAIN, I-LINK, I-OBS-MODEL, I-ATTEMPT, I-DIGEST, I-GATES, I-REFS, and I-RECEIPTS.

Input JSON and linked receipt JSON reject duplicate keys, nonfinite numbers
(including exponent overflow), invalid encodings, unpaired Unicode surrogates,
and excessive nesting. In-memory validation applies the same JSON-value checks
before canonical sizing: non-string object keys, tuples and cycles are invalid,
rather than being silently converted by serialization. Boolean fields require
actual booleans, and enum/gate-ID values are type-checked before membership tests.

Timestamps use the existing RFC3339 spelling with a valid calendar date,
hours 00–23, minutes/seconds 00–59, and numeric offset hours 00–23 and minutes
00–59. Fractional seconds and `-00:00` are preserved. Leap-second text is not
supported by this observation timestamp profile. No chronological ordering
between timestamp fields is inferred.

The observation file may be outside `--root`, including an alias to a regular
file. Its opened descriptor is checked without blocking and its 32 KiB limit
is enforced before and during reading. Referenced files retain no-follow
containment under `--root` and their separate 8 MiB limit; nonregular leaves
such as FIFOs are rejected without waiting for a writer. Receipt bytes decoded
by `--check-receipts` must match the declared digest as well as the existing
inode linkage checks. This does not authenticate receipt authors or compare
observation outcome/model/effort claims with the complete receipt semantics.

Worked subject hashes:

```text
{"artifact_id":"ep-01","producer":"deep-loop","run_id":"01ARZ3NDEKTSV4RRFFQ69G5FAV"}
→ a1a6ccd20d42089aa5bdacfba8e80f6176f785383be380961597e856b9d3966c
{"artifact_id":null,"producer":"deep-model-router","run_id":"grp-01"}
→ a4639d1b61339690dd157e980e60f93609265f26fec3267a61bec7483b9243f2
```
