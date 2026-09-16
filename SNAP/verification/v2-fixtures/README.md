# Synthetic v2 fixtures

Every file in this directory is generated offline by
`scripts/validation/generate_v2_fixtures.py`. The fixed clock, action/request
identities, failures, timeout, retry, script edit, parser structures, and
UNKNOWN complement are synthetic evidence for contract tests. They are not
measurements from a live pilot and do not establish any acceptance gate.

Regenerate into a new empty directory and audit with:

```text
PYTHONPATH=src python3 scripts/validation/generate_v2_fixtures.py --output <new-directory>
PYTHONPATH=src python3 scripts/validation/audit_v2_journals.py <new-directory>
```
