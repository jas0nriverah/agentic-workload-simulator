# Review and acceptance decisions

Main-agent review checked the bounded experiments, source/target contracts and numerical comparisons. Independent Luna review found no material correctness objections to the final CPU mechanism, Django transfer, openat entry-feature and native sequence experiments.

Resolved objections:

- A maximum-coverage representative cannot be restricted to observed training values: durations 100 and 160 both admit prediction 120 although neither observed value covers both. The estimator now uses overlapping acceptance intervals, with a regression test.
- Openat integrity is linked to the exact atomic and transfer raw hashes and their zero-loss/range/token checks. The separate combined-domain scan has an explicit 750 MB bound; the two preceding scans keep their own 400 MB bounds.
- Openat nonpositive/censored targets now cause failure rather than disappearing from coverage. No such target occurs in the scored sample.
- Four Astropy fits applied to one Django trace are not four independent Django tests. Reports preserve that limitation.
- The first-request indicator does not establish a cold-start mechanism or justify a model change. Its tiny/mixed effects are retained as a rejected hypothesis.

Accepted for development: serialized openat flags/path fits and the alternative CPU tail estimator. Neither is declared a validated all-operation or cross-hardware simulator. The existing general CPU and GPU references remain available; no acquisition or production implementation is changed.

Focused checks: CPU mechanisms 7 tests, Django transfer 3, openat entry features 3, native sequence 2. The mechanism controls reproduce the prior atomic metrics exactly. E2E review and tests are documented separately.
