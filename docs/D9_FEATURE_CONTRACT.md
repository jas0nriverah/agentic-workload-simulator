# D9 feature contract (sealed)

Extractor id: `tool-feature-extractor.v1.cd-skip-20260908`

Historical `.traj` `action` and live `step.action` call the same function:
`agentic_sim.assignment.tool_features.extract_tool_features`.

## Tool features known before execution

- `tool_name` after skipping leading `cd` segments (`cd /testbed && grep` → `grep`)
- `subcommand`, `command_prefix` (first three tokens of the primary command)
- `operation_class` (view/create/str_replace of `str_replace_editor` are distinct; the substring `str_replace` in the tool name is not patch)
- `declared_command_bytes`, `declared_path_count`, `has_pipe`, `has_glob`, `command_sha256`

`declared_read_bytes` / `declared_write_bytes` remain 0: they were never captured.

## Model features known before generation

- `input_tokens`, `context_tokens`, `max_output_tokens`

## Sequential (previous events only)

After event *k* is revealed, event *k+1* may use that label (cited by `record_sha256`).
Current-event `wall_ms`, current-event `output_tokens`, future events, and evaluator
resolve labels are rejected.

## Forbidden

Holdout shopping, rescoring sympy-12481, relaxing the 25% per-event gate.
