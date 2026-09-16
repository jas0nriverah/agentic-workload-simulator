# Entry flags improve openat coverage, but do not satisfy D9

The preceding operation analysis attributed 134,971 misses to `openat`, the largest contributor under the existing operation/path median. The collector already preserves `openat` flags from syscall-entry argument 2. This check uses those flags and fixed generic lexical path classes (special filesystem prefixes, site-packages, and a small fixed extension list); it does not use return values, observed durations, case identities, or evaluator outcomes as features.

The fixed four Astropy traces provide leave-one-instance-out fits. The exact same fits are also scored on the fixed ordinal-18 Django trace, without fitting on its targets. All five raw hashes match; 702,041,200 bytes were scanned under a 750 MB bound. Only openat rows are retained for fitting/scoring. There are no unsupported openat targets in this sample. Each fitted group requires at least 20 training events from two training instances; otherwise the operation median is used.

The separate 750 MB bound covers this combined-domain experiment; the preceding mechanism and transfer experiments each retain their own 400 MB bounds. Action-range/token integrity and zero-loss checks are inherited from the hash-bound atomic and transfer artifacts, whose hashes are recorded in the output. The same raw files are rehashed here. Any nonpositive or censored openat target causes a failure rather than a partial score. The fallback median is specific to openat because no other operation is fitted in this experiment.

| Entry-feature model | Astropy coverage within 25% | Astropy worst error | Django mean coverage across four fits | Django worst error |
|---|---:|---:|---:|---:|
| Coarse path control | 30.33% | 2,295.69% | 33.97% | 706.65% |
| Flags | 32.36% | 3,107.36% | 37.10% | 282.35% |
| Flags + generic path group | 46.27% | 3,107.36% | 39.14% | 339.36% |

Flags plus generic path grouping is a useful development candidate: coverage improves on both the grouped Astropy comparison and the single Django trace. The Astropy worst error worsens, however, and all variants fail the literal 25% criterion. These are openat-only results, not overall CPU coverage or proof of repository-wide generalization. Repeatedly scoring the same Django trace does not create four independent validation instances.

`report.json` preserves every fold's fitted tables and the final fit on Astropy alone, allowing prediction without refitting from raw files. `compare.py` regenerates the comparison. Three tests check exclusion of result fields, independent-instance support, and prediction-key serialization. No inference, source snapshot change, or acquisition modification was made.
