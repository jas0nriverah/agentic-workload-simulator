# Cloud-readiness blockers and gates

Technical cloud-readiness is `PASS_static_local`: the runtime, command,
artifact, evaluator, telemetry, bootstrap, collection, and runbook contracts
are pinned and locally validated. The following are deliberately outstanding
and cannot be proven on this macOS arm64 host:

- `H100_VALIDATION_REMAINING`: Linux x86-64/H100 model fit, vLLM tool/parser
  health, native `/metrics`, evaluator image pulls, gold smokes, and the first
  real generated trajectory still require the target host.
- `PAID_SESSION_AUTHORIZATION_REQUIRED_FOR_SAFE_RENT`: the committed session
  example is fail-closed (`authorized: false`, zero caps). A user-completed
  session file and console controls are required before any paid launch.

No cloud resource was rented, no provider API was contacted, and no repository
was published during this phase.
