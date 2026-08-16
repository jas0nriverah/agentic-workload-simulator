# Lambda troubleshooting matrix

Record the exact tested command and output here after Linux compatibility work.
Do not invent parser flags or silently change the model/configuration on the
paid host.

| Symptom | First check | Allowed action | New decision required? |
|---|---|---|---|
| GPU OOM at health-check context | memory manifest and vLLM logs | stop server; retry only with documented operational setting | yes if precision/model/context changes |
| tool call cannot parse | pinned model/vLLM parser/template smoke | use an already-tested fallback command | yes if semantic payload changes |
| Docker unavailable | `docker info`, user permissions | use reviewed split-host evaluator path | yes |
| model download stalls | byte progress, disk, registry reachability | resume if progressing; stop at billing deadline | no unless artifact changes |
| evaluator image pull fails | exact image manifest and registry status | authenticated user login or approved prebuilt image | yes if evaluator changes |
| SSH disconnect | tmux/session and process manifest | reconnect and inspect state; do not duplicate run | no |
