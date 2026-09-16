# First-request hypothesis: do not promote

The earlier diagnostic found disproportionate native request errors at the first request. This bounded comparison tests whether a nonnegative first-request intercept helps the existing token and conditional cache models. The indicator uses the preceding physical client request-start history; it does not infer cold state from measured latency. Prompt/output/cache quantities retain their explicit supplied-workload contract. It is not a prospective output-length or cache-state forecast.

All 2,080 requests and the same five instance-grouped development folds are retained. Start journals are hash-checked and exact-joined by case and physical request ID. No protected evaluation case or new inference is used.

| Model | Within 25% | Equal-instance coverage | Worst error | Cases with all requests passing |
|---|---:|---:|---:|---:|
| Token control | 94.8558% | 94.8558% | 96.6848% | 7/49 |
| Token + first indicator | 94.6635% | 94.5805% | 96.6908% | 7/49 |
| Conditional cache control | 97.4519% | 97.8552% | 96.7683% | 19/49 |
| Conditional cache + first indicator | 97.5000% | 97.8213% | 96.7895% | 20/49 |

The token variant loses four passing requests. The cache variant gains one passing request but worsens equal-instance coverage and worst error. That is not persuasive improvement, especially after adaptive development selection. Keep the existing candidates. First-position association alone does not establish a predictable cold-start mechanism; these results do not justify collecting more first-request repetitions merely to fit this indicator.

`compare.py` reproduces the fit using retained native evidence and start journals. `report.json` includes fold metrics, coefficients and source hashes; `predictions.jsonl` retains all candidate predictions. Two feature-contract tests pass. Hardware transfer and the literal all-event D9 gate remain unvalidated.
