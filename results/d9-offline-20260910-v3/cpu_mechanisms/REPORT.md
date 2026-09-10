# Bounded D9 CPU mechanism comparison

This is an offline, trace-conditioned diagnostic on the same four fixed fully valid `train_calibration` instances as the atomic CPU comparison. It does not change the collector, acquire data, run inference, or establish a complete D9 pass.

## Fixed population and feature contract

Cases were selected by ascending queue ordinal after the evidence validity gate, taking the first four distinct instances: 1 (astropy__astropy-14182), 3 (astropy__astropy-14995), 4 (astropy__astropy-6938), 17 (astropy__astropy-7746). Selection did not inspect durations, outcomes, or model errors.
The decoder streamed **977433 raw records** (390973200 bytes) through the existing v3 `400`-byte packet decoder. The compact typed-array store used **52781382 bytes** (50.34 MiB); no full normalized event export was created.
Features are limited to syscall number/name/kind, decoder-proven requested-size buckets, and lexical classes from bounded syscall-entry path bytes. The decoder also exposes open/openat flags, mmap length/prot/flags, and pread/pwrite offsets, but those descriptors are intentionally ignored in this bounded comparison; requested-size/path values can instead be unavailable or unknown, and openat2 flag/pointer details are opaque under the retained ABI. Return values, status, completed latency, residuals, future events, filesystem lookups, and tool wall time are excluded. Positive durations are targets; failure-status events remain valid targets.

## Leave-one-instance-out results

| Candidate | Events | Within 25% | Mean APE | P95 APE | Worst APE | Equal-instance mean coverage | Equal-instance mean APE |
|---|---:|---:|---:|---:|---:|---:|---:|
| `global_median` | 974169 | 29.11% (283534) | 52.26% | 117.24% | 418.62% | 28.55% | 52.65% |
| `operation_median` | 974169 | 39.70% (386757) | 41.40% | 104.42% | 4083.77% | 39.30% | 41.32% |
| `operation_requested_size_bucket_median` | 974169 | 43.02% (419087) | 41.02% | 110.74% | 4083.77% | 42.87% | 41.08% |
| `operation_path_class_median` | 974169 | 46.32% (451237) | 39.96% | 109.79% | 4152.24% | 46.03% | 39.89% |
| `operation_requested_size_path_median` | 974169 | 46.77% (455667) | 42.94% | 115.10% | 4152.24% | 46.53% | 43.17% |
| `operation_coverage_representative` | 974169 | 45.43% (442528) | 38.09% | 96.43% | 1490.50% | 45.04% | 37.78% |

Recommended primary candidate: `operation_path_class_median`. It reaches 46.32% held-out within-25% coverage with 39.96% mean APE. The hierarchical size+path interaction reaches 46.77% coverage (+0.45 points) but raises mean APE to 42.94% and does not improve the long tail. The secondary tail candidate `operation_coverage_representative` trades 0.89 percentage points of coverage (-0.89) for 38.09% mean APE, 96.43% P95 APE, and 1490.50% worst APE. These are candidates for new fixed held-out validation, not a D9PASS claim.

## Per-operation coverage, count, and worst error

Each row is pooled across held-out folds. The prediction maps were fit without that operation's held-out instance; `worst event` is an identity and not a case-selection rule.

### `global_median`

| Operation | Count | Within 25% | Mean APE | Worst APE | Worst event |
|---|---:|---:|---:|---:|---|
| `nr=0;syscall=read;kind=read` | 248867 | 20.30% (50515) | 70.31% | 332.18% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:891476253668047685:374949472180207:118130400` |
| `nr=257;syscall=openat;kind=open` | 193724 | 27.84% (53935) | 46.21% | 137.22% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964520380861:126246400` |
| `nr=262;syscall=unknown;kind=stat` | 152617 | 48.30% (73721) | 29.94% | 234.22% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964546981767:126794000` |
| `nr=9;syscall=mmap;kind=mmap` | 95673 | 56.60% (54154) | 26.60% | 134.27% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:7126398423717088801:374961881810574:124757200` |
| `nr=3;syscall=close;kind=close` | 160074 | 21.50% (34421) | 57.68% | 418.62% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:10257906033194468032:374963499648034:124977200` |
| `nr=1;syscall=write;kind=write` | 30958 | 2.86% (885) | 74.82% | 226.43% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:6433169219372776007:374816633726700:14513200` |
| `nr=56;syscall=clone;kind=clone` | 3191 | 0.00% (0) | 89.05% | 99.84% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:15811920986986898295:374901861687521:100082800` |
| `nr=59;syscall=execve;kind=exec` | 18258 | 28.70% (5240) | 39.38% | 99.92% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:13889683564117855315:374835598927080:24028400` |
| `nr=17;syscall=pread64;kind=read` | 9219 | 4.79% (442) | 90.61% | 279.80% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3734057338957818965:374961021797370:122386400` |
| `nr=217;syscall=getdents64;kind=getdents` | 36134 | 13.78% (4978) | 65.63% | 337.21% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964530791586:126434800` |
| `nr=89;syscall=readlink;kind=stat` | 3079 | 32.80% (1010) | 41.88% | 210.74% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964435777246:125230400` |
| `nr=82;syscall=rename;kind=rename` | 4048 | 0.00% (0) | 84.56% | 99.96% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:2889759616275614200:374807024439417:18199200` |
| `nr=87;syscall=unknown;kind=metadata_mutation` | 7123 | 0.10% (7) | 90.27% | 136.69% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:3735169723926332838:374800959318234:11838800` |
| `nr=83;syscall=unknown;kind=metadata_mutation` | 7729 | 54.61% (4221) | 32.98% | 99.97% | `assignment-production-v2:d204afe54b23a1ef26e269881f1cc7a74c1eb78a4799bc75846b78530c3a9ad3:16605619094281164647:374813835381034:16887200` |
| `nr=58;syscall=vfork;kind=fork` | 73 | 0.00% (0) | 99.63% | 99.81% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:2889759616275614200:374802710029316:13542400` |
| `nr=84;syscall=unknown;kind=metadata_mutation` | 36 | 0.00% (0) | 89.15% | 99.24% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:6433169219372776007:374821104075848:22273200` |
| `nr=263;syscall=unknown;kind=metadata_mutation` | 26 | 0.00% (0) | 93.94% | 99.95% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:4437193936378751189:374829896543495:18842800` |
| `nr=77;syscall=ftruncate;kind=metadata_mutation` | 3340 | 0.15% (5) | 61.15% | 99.74% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:15395733229907248622:374897568026139:96928400` |

### `operation_median`

| Operation | Count | Within 25% | Mean APE | Worst APE | Worst event |
|---|---:|---:|---:|---:|---|
| `nr=0;syscall=read;kind=read` | 248867 | 30.32% (75456) | 39.40% | 177.59% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:891476253668047685:374949472180207:118130400` |
| `nr=257;syscall=openat;kind=open` | 193724 | 29.16% (56487) | 56.23% | 370.66% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964520380861:126246400` |
| `nr=262;syscall=unknown;kind=stat` | 152617 | 48.44% (73922) | 30.54% | 247.56% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964546981767:126794000` |
| `nr=9;syscall=mmap;kind=mmap` | 95673 | 50.71% (48514) | 28.47% | 183.21% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:7126398423717088801:374961881810574:124757200` |
| `nr=3;syscall=close;kind=close` | 160074 | 56.28% (90094) | 26.79% | 260.69% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:10257906033194468032:374963499648034:124977200` |
| `nr=1;syscall=write;kind=write` | 30958 | 19.94% (6173) | 81.29% | 1534.12% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:6433169219372776007:374816633726700:14513200` |
| `nr=56;syscall=clone;kind=clone` | 3191 | 45.69% (1458) | 101.58% | 743.56% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:9352785134238214289:374811237275282:5975200` |
| `nr=59;syscall=execve;kind=exec` | 18258 | 63.86% (11660) | 29.42% | 99.93% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:13889683564117855315:374835598927080:24028400` |
| `nr=17;syscall=pread64;kind=read` | 9219 | 83.27% (7677) | 14.15% | 99.37% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:7699481260623462470:374820101632350:4934400` |
| `nr=217;syscall=getdents64;kind=getdents` | 36134 | 15.26% (5515) | 85.24% | 478.20% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964530791586:126434800` |
| `nr=89;syscall=readlink;kind=stat` | 3079 | 33.13% (1020) | 41.26% | 210.33% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964435777246:125230400` |
| `nr=82;syscall=rename;kind=rename` | 4048 | 55.78% (2258) | 27.37% | 99.78% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:2889759616275614200:374807024439417:18199200` |
| `nr=87;syscall=unknown;kind=metadata_mutation` | 7123 | 13.48% (960) | 108.25% | 4083.77% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:3735169723926332838:374800959318234:11838800` |
| `nr=83;syscall=unknown;kind=metadata_mutation` | 7729 | 52.61% (4066) | 31.54% | 99.96% | `assignment-production-v2:d204afe54b23a1ef26e269881f1cc7a74c1eb78a4799bc75846b78530c3a9ad3:16605619094281164647:374813835381034:16887200` |
| `nr=58;syscall=vfork;kind=fork` | 73 | 63.01% (46) | 63.26% | 434.05% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:2889759616275614200:374810064628874:20945600` |
| `nr=84;syscall=unknown;kind=metadata_mutation` | 36 | 33.33% (12) | 41.93% | 103.85% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:4437193936378751189:374829800801025:18840000` |
| `nr=263;syscall=unknown;kind=metadata_mutation` | 26 | 23.08% (6) | 58.80% | 104.79% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:290778274863218144:374913033274705:76991200` |
| `nr=77;syscall=ftruncate;kind=metadata_mutation` | 3340 | 42.90% (1433) | 33.10% | 102.76% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:891476253668047685:374949603837717:119650000` |

### `operation_requested_size_bucket_median`

| Operation | Count | Within 25% | Mean APE | Worst APE | Worst event |
|---|---:|---:|---:|---:|---|
| `nr=0;syscall=read;kind=read` | 248867 | 41.72% (103824) | 39.90% | 584.55% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:6433169219372776007:374819823043718:18820400` |
| `nr=257;syscall=openat;kind=open` | 193724 | 29.16% (56487) | 56.23% | 370.66% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964520380861:126246400` |
| `nr=262;syscall=unknown;kind=stat` | 152617 | 48.44% (73922) | 30.54% | 247.56% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964546981767:126794000` |
| `nr=9;syscall=mmap;kind=mmap` | 95673 | 50.71% (48514) | 28.47% | 183.21% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:7126398423717088801:374961881810574:124757200` |
| `nr=3;syscall=close;kind=close` | 160074 | 56.28% (90094) | 26.79% | 260.69% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:10257906033194468032:374963499648034:124977200` |
| `nr=1;syscall=write;kind=write` | 30958 | 32.59% (10090) | 64.04% | 825.11% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:6433169219372776007:374816633726700:14513200` |
| `nr=56;syscall=clone;kind=clone` | 3191 | 45.69% (1458) | 101.58% | 743.56% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:9352785134238214289:374811237275282:5975200` |
| `nr=59;syscall=execve;kind=exec` | 18258 | 63.86% (11660) | 29.42% | 99.93% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:13889683564117855315:374835598927080:24028400` |
| `nr=17;syscall=pread64;kind=read` | 9219 | 83.27% (7677) | 14.15% | 99.37% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:7699481260623462470:374820101632350:4934400` |
| `nr=217;syscall=getdents64;kind=getdents` | 36134 | 15.39% (5560) | 86.37% | 992.71% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964492429076:125358800` |
| `nr=89;syscall=readlink;kind=stat` | 3079 | 33.13% (1020) | 41.26% | 210.33% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964435777246:125230400` |
| `nr=82;syscall=rename;kind=rename` | 4048 | 55.78% (2258) | 27.37% | 99.78% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:2889759616275614200:374807024439417:18199200` |
| `nr=87;syscall=unknown;kind=metadata_mutation` | 7123 | 13.48% (960) | 108.25% | 4083.77% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:3735169723926332838:374800959318234:11838800` |
| `nr=83;syscall=unknown;kind=metadata_mutation` | 7729 | 52.61% (4066) | 31.54% | 99.96% | `assignment-production-v2:d204afe54b23a1ef26e269881f1cc7a74c1eb78a4799bc75846b78530c3a9ad3:16605619094281164647:374813835381034:16887200` |
| `nr=58;syscall=vfork;kind=fork` | 73 | 63.01% (46) | 63.26% | 434.05% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:2889759616275614200:374810064628874:20945600` |
| `nr=84;syscall=unknown;kind=metadata_mutation` | 36 | 33.33% (12) | 41.93% | 103.85% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:4437193936378751189:374829800801025:18840000` |
| `nr=263;syscall=unknown;kind=metadata_mutation` | 26 | 23.08% (6) | 58.80% | 104.79% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:290778274863218144:374913033274705:76991200` |
| `nr=77;syscall=ftruncate;kind=metadata_mutation` | 3340 | 42.90% (1433) | 33.10% | 102.76% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:891476253668047685:374949603837717:119650000` |

### `operation_path_class_median`

| Operation | Count | Within 25% | Mean APE | Worst APE | Worst event |
|---|---:|---:|---:|---:|---|
| `nr=0;syscall=read;kind=read` | 248867 | 48.36% (120347) | 36.57% | 311.30% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:891476253668047685:374948532917755:116891600` |
| `nr=257;syscall=openat;kind=open` | 193724 | 30.33% (58753) | 58.33% | 2295.69% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:10810450555700474486:374955367661849:121037600` |
| `nr=262;syscall=unknown;kind=stat` | 152617 | 55.96% (85411) | 27.57% | 329.33% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964546981767:126794000` |
| `nr=9;syscall=mmap;kind=mmap` | 95673 | 49.58% (47435) | 28.34% | 155.45% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:7126398423717088801:374961881810574:124757200` |
| `nr=3;syscall=close;kind=close` | 160074 | 59.03% (94486) | 24.39% | 163.64% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964493938935:125373600` |
| `nr=1;syscall=write;kind=write` | 30958 | 25.71% (7958) | 72.28% | 2293.88% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:13476285590194029423:374906582983387:106278400` |
| `nr=56;syscall=clone;kind=clone` | 3191 | 45.69% (1458) | 101.58% | 743.56% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:9352785134238214289:374811237275282:5975200` |
| `nr=59;syscall=execve;kind=exec` | 18258 | 63.86% (11660) | 29.42% | 99.93% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:13889683564117855315:374835598927080:24028400` |
| `nr=17;syscall=pread64;kind=read` | 9219 | 83.23% (7673) | 14.10% | 99.27% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:17528083785035111341:374875497586516:53252000` |
| `nr=217;syscall=getdents64;kind=getdents` | 36134 | 16.70% (6034) | 85.90% | 562.60% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:891476253668047685:374949472707476:118153600` |
| `nr=89;syscall=readlink;kind=stat` | 3079 | 33.13% (1020) | 41.03% | 208.68% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964435777246:125230400` |
| `nr=82;syscall=rename;kind=rename` | 4048 | 57.24% (2317) | 27.27% | 99.80% | `assignment-production-v2:d204afe54b23a1ef26e269881f1cc7a74c1eb78a4799bc75846b78530c3a9ad3:6604771015891070537:374836216670122:29510800` |
| `nr=87;syscall=unknown;kind=metadata_mutation` | 7123 | 13.62% (970) | 108.46% | 4152.24% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:3735169723926332838:374800959318234:11838800` |
| `nr=83;syscall=unknown;kind=metadata_mutation` | 7729 | 52.65% (4069) | 31.63% | 931.23% | `assignment-production-v2:d204afe54b23a1ef26e269881f1cc7a74c1eb78a4799bc75846b78530c3a9ad3:4274674607261411946:374917519049846:107724800` |
| `nr=58;syscall=vfork;kind=fork` | 73 | 63.01% (46) | 63.26% | 434.05% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:2889759616275614200:374810064628874:20945600` |
| `nr=84;syscall=unknown;kind=metadata_mutation` | 36 | 33.33% (12) | 41.93% | 103.85% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:4437193936378751189:374829800801025:18840000` |
| `nr=263;syscall=unknown;kind=metadata_mutation` | 26 | 11.54% (3) | 332.58% | 1540.22% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:290778274863218144:374913033274705:76991200` |
| `nr=77;syscall=ftruncate;kind=metadata_mutation` | 3340 | 47.46% (1585) | 31.70% | 127.92% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:891476253668047685:374949603837717:119650000` |

### `operation_requested_size_path_median`

| Operation | Count | Within 25% | Mean APE | Worst APE | Worst event |
|---|---:|---:|---:|---:|---|
| `nr=0;syscall=read;kind=read` | 248867 | 49.47% (123112) | 49.52% | 1764.21% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:15468003261347880249:374840452179145:36986800` |
| `nr=257;syscall=openat;kind=open` | 193724 | 30.33% (58753) | 58.33% | 2295.69% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:10810450555700474486:374955367661849:121037600` |
| `nr=262;syscall=unknown;kind=stat` | 152617 | 55.96% (85411) | 27.57% | 329.33% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964546981767:126794000` |
| `nr=9;syscall=mmap;kind=mmap` | 95673 | 49.58% (47435) | 28.34% | 155.45% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:7126398423717088801:374961881810574:124757200` |
| `nr=3;syscall=close;kind=close` | 160074 | 59.03% (94486) | 24.39% | 163.64% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964493938935:125373600` |
| `nr=1;syscall=write;kind=write` | 30958 | 30.96% (9585) | 60.79% | 870.52% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:2889759616275614200:374809527999409:19494000` |
| `nr=56;syscall=clone;kind=clone` | 3191 | 45.69% (1458) | 101.58% | 743.56% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:9352785134238214289:374811237275282:5975200` |
| `nr=59;syscall=execve;kind=exec` | 18258 | 63.86% (11660) | 29.42% | 99.93% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:13889683564117855315:374835598927080:24028400` |
| `nr=17;syscall=pread64;kind=read` | 9219 | 83.23% (7673) | 14.10% | 99.27% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:17528083785035111341:374875497586516:53252000` |
| `nr=217;syscall=getdents64;kind=getdents` | 36134 | 16.80% (6072) | 86.87% | 967.63% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:891476253668047685:374949463525641:118051600` |
| `nr=89;syscall=readlink;kind=stat` | 3079 | 33.13% (1020) | 41.03% | 208.68% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964435777246:125230400` |
| `nr=82;syscall=rename;kind=rename` | 4048 | 57.24% (2317) | 27.27% | 99.80% | `assignment-production-v2:d204afe54b23a1ef26e269881f1cc7a74c1eb78a4799bc75846b78530c3a9ad3:6604771015891070537:374836216670122:29510800` |
| `nr=87;syscall=unknown;kind=metadata_mutation` | 7123 | 13.62% (970) | 108.46% | 4152.24% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:3735169723926332838:374800959318234:11838800` |
| `nr=83;syscall=unknown;kind=metadata_mutation` | 7729 | 52.65% (4069) | 31.63% | 931.23% | `assignment-production-v2:d204afe54b23a1ef26e269881f1cc7a74c1eb78a4799bc75846b78530c3a9ad3:4274674607261411946:374917519049846:107724800` |
| `nr=58;syscall=vfork;kind=fork` | 73 | 63.01% (46) | 63.26% | 434.05% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:2889759616275614200:374810064628874:20945600` |
| `nr=84;syscall=unknown;kind=metadata_mutation` | 36 | 33.33% (12) | 41.93% | 103.85% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:4437193936378751189:374829800801025:18840000` |
| `nr=263;syscall=unknown;kind=metadata_mutation` | 26 | 11.54% (3) | 332.58% | 1540.22% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:290778274863218144:374913033274705:76991200` |
| `nr=77;syscall=ftruncate;kind=metadata_mutation` | 3340 | 47.46% (1585) | 31.70% | 127.92% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:891476253668047685:374949603837717:119650000` |

### `operation_coverage_representative`

| Operation | Count | Within 25% | Mean APE | Worst APE | Worst event |
|---|---:|---:|---:|---:|---|
| `nr=0;syscall=read;kind=read` | 248867 | 47.21% (117492) | 34.94% | 144.18% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:891476253668047685:374949472180207:118130400` |
| `nr=257;syscall=openat;kind=open` | 193724 | 26.06% (50478) | 59.89% | 395.66% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964520380861:126246400` |
| `nr=262;syscall=unknown;kind=stat` | 152617 | 51.39% (78437) | 28.81% | 210.00% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964546981767:126794000` |
| `nr=9;syscall=mmap;kind=mmap` | 95673 | 57.06% (54587) | 26.37% | 157.01% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:7126398423717088801:374961881810574:124757200` |
| `nr=3;syscall=close;kind=close` | 160074 | 58.02% (92875) | 25.71% | 241.38% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:10257906033194468032:374963499648034:124977200` |
| `nr=1;syscall=write;kind=write` | 30958 | 22.66% (7014) | 57.41% | 851.54% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:6433169219372776007:374816633726700:14513200` |
| `nr=56;syscall=clone;kind=clone` | 3191 | 48.54% (1549) | 108.85% | 808.16% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:9352785134238214289:374811237275282:5975200` |
| `nr=59;syscall=execve;kind=exec` | 18258 | 63.53% (11600) | 29.05% | 99.93% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:13889683564117855315:374835598927080:24028400` |
| `nr=17;syscall=pread64;kind=read` | 9219 | 70.96% (6542) | 19.61% | 120.87% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3734057338957818965:374961021797370:122386400` |
| `nr=217;syscall=getdents64;kind=getdents` | 36134 | 28.90% (10441) | 53.37% | 159.45% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964530791586:126434800` |
| `nr=89;syscall=readlink;kind=stat` | 3079 | 41.93% (1291) | 36.68% | 135.54% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:3519081704084685377:374964435777246:125230400` |
| `nr=82;syscall=rename;kind=rename` | 4048 | 66.82% (2705) | 25.89% | 99.80% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:2889759616275614200:374807024439417:18199200` |
| `nr=87;syscall=unknown;kind=metadata_mutation` | 7123 | 19.12% (1362) | 60.82% | 1490.50% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:3735169723926332838:374800959318234:11838800` |
| `nr=83;syscall=unknown;kind=metadata_mutation` | 7729 | 61.28% (4736) | 30.49% | 99.96% | `assignment-production-v2:d204afe54b23a1ef26e269881f1cc7a74c1eb78a4799bc75846b78530c3a9ad3:16605619094281164647:374813835381034:16887200` |
| `nr=58;syscall=vfork;kind=fork` | 73 | 57.53% (42) | 61.48% | 401.99% | `assignment-production-v2:ee4fc39eafebc5d7c2ea1dce540d511f472d14ec3c13b9c8a70c78b3b4340129:2889759616275614200:374810064628874:20945600` |
| `nr=84;syscall=unknown;kind=metadata_mutation` | 36 | 38.89% (14) | 36.48% | 94.60% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:6433169219372776007:374821104075848:22273200` |
| `nr=263;syscall=unknown;kind=metadata_mutation` | 26 | 42.31% (11) | 46.00% | 99.46% | `assignment-production-v2:648009374f4193ea13938de660cec315c8e9402e8e50dddfd752d3b735a57023:4437193936378751189:374829896543495:18842800` |
| `nr=77;syscall=ftruncate;kind=metadata_mutation` | 3340 | 40.48% (1352) | 34.10% | 100.99% | `assignment-production-v2:394940ef3cccb81d3ce05b1c54505ade4d4452f6da1470c0e31b11783eecdd7a:1498500910102402815:374906986816203:73627600` |

## Equal-instance results

These are macro averages across the four held-out instances, so the largest trace does not dominate the comparison. Detailed rows are retained in `model.json`.

| Candidate | Macro coverage | Macro mean APE | Macro worst APE |
|---|---:|---:|---:|
| `global_median` | 28.55% | 52.65% | 312.07% |
| `operation_median` | 39.30% | 41.32% | 2485.61% |
| `operation_requested_size_bucket_median` | 42.87% | 41.08% | 2485.61% |
| `operation_path_class_median` | 46.03% | 39.89% | 2576.38% |
| `operation_requested_size_path_median` | 46.53% | 43.17% | 2576.38% |
| `operation_coverage_representative` | 45.04% | 37.78% | 1011.35% |

## Timing distributions and same-feature spread

Quantiles are nearest-rank positive-duration values pooled over the four traces. `q90/q10` reports within-feature timing spread; these distributions describe the target and are not used to choose cases.

| Operation | Count | q01 (ns) | q10 | q50 | q90 | q99 | q90/q10 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `nr=0;syscall=read;kind=read` | 248867 | 2380 | 3370 | 4960 | 15240 | 464849 | 4.52 |
| `nr=257;syscall=openat;kind=open` | 193724 | 4910 | 6620 | 13830 | 41370 | 1011660 | 6.25 |
| `nr=262;syscall=unknown;kind=stat` | 152617 | 4110 | 5340 | 7430 | 16590 | 82800 | 3.11 |
| `nr=9;syscall=mmap;kind=mmap` | 95673 | 4710 | 6460 | 8780 | 17760 | 36520 | 2.75 |
| `nr=3;syscall=close;kind=close` | 160074 | 2700 | 3550 | 5110 | 8970 | 19530 | 2.53 |
| `nr=1;syscall=write;kind=write` | 30958 | 5490 | 14180 | 36810 | 159850 | 1612609 | 11.27 |
| `nr=56;syscall=clone;kind=clone` | 3191 | 21380 | 24800 | 115050 | 246220 | 1660500 | 9.93 |
| `nr=59;syscall=execve;kind=exec` | 18258 | 4860 | 5180 | 6190 | 213490 | 511869 | 41.21 |
| `nr=17;syscall=pread64;kind=read` | 9219 | 2729 | 3420 | 3730 | 5400 | 13120 | 1.58 |
| `nr=217;syscall=getdents64;kind=getdents` | 36134 | 2180 | 3850 | 9991 | 54579 | 201970 | 14.18 |
| `nr=89;syscall=readlink;kind=stat` | 3079 | 3311 | 4590 | 7350 | 28729 | 58140 | 6.26 |
| `nr=82;syscall=rename;kind=rename` | 4048 | 30720 | 33840 | 46200 | 91659 | 3343779 | 2.71 |
| `nr=87;syscall=unknown;kind=metadata_mutation` | 7123 | 28800 | 33730 | 97129 | 1498940 | 4256058 | 44.44 |
| `nr=83;syscall=unknown;kind=metadata_mutation` | 7729 | 5991 | 7100 | 9590 | 107910 | 1887780 | 15.20 |
| `nr=58;syscall=vfork;kind=fork` | 73 | 565450 | 820049 | 3043478 | 3720308 | 3938447 | 4.54 |
| `nr=84;syscall=unknown;kind=metadata_mutation` | 36 | 36930 | 42370 | 67000 | 243900 | 976449 | 5.76 |
| `nr=263;syscall=unknown;kind=metadata_mutation` | 26 | 57900 | 66241 | 95850 | 2752879 | 15430012 | 41.56 |
| `nr=77;syscall=ftruncate;kind=metadata_mutation` | 3340 | 10430 | 12071 | 19940 | 36220 | 707539 | 3.00 |

The full operation+size, operation+path, and operation+size+path timing tables are in `model.json`; the ten widest groups per family are summarized below.

| Feature family | Groups | Widest groups by q90/q10 |
|---|---:|---|
| `operation` | 18 | `nr=87;syscall=unknown;kind=metadata_mutation` (7123, 44.44x); `nr=263;syscall=unknown;kind=metadata_mutation` (26, 41.56x); `nr=59;syscall=execve;kind=exec` (18258, 41.21x); `nr=83;syscall=unknown;kind=metadata_mutation` (7729, 15.20x); `nr=217;syscall=getdents64;kind=getdents` (36134, 14.18x) |
| `operation_requested_size` | 25 | `nr=87;syscall=unknown;kind=metadata_mutation|entry_size_unavailable` (7123, 44.44x); `nr=263;syscall=unknown;kind=metadata_mutation|entry_size_unavailable` (26, 41.56x); `nr=59;syscall=execve;kind=exec|entry_size_unavailable` (18258, 41.21x); `nr=0;syscall=read;kind=read|entry_size_gt_64KiB` (8757, 34.91x); `nr=217;syscall=getdents64;kind=getdents|entry_size_1_4KiB` (340, 15.86x) |
| `operation_path` | 51 | `nr=257;syscall=openat;kind=open|entry_path_truncated` (4048, 83.55x); `nr=263;syscall=unknown;kind=metadata_mutation|entry_path_relative` (18, 49.25x); `nr=87;syscall=unknown;kind=metadata_mutation|entry_path_relative` (7020, 44.55x); `nr=217;syscall=getdents64;kind=getdents|entry_path_dot_relative` (106, 43.41x); `nr=59;syscall=execve;kind=exec|entry_path_absolute` (18258, 41.21x) |
| `operation_requested_size_path` | 72 | `nr=0;syscall=read;kind=read|entry_size_gt_64KiB|entry_path_unknown` (2, 11312.60x); `nr=0;syscall=read;kind=read|entry_size_4_64KiB|entry_path_unknown` (1125, 704.39x); `nr=0;syscall=read;kind=read|entry_size_4_64KiB|entry_path_relative` (4590, 93.62x); `nr=0;syscall=read;kind=read|entry_size_4_64KiB|entry_path_truncated` (107, 90.62x); `nr=257;syscall=openat;kind=open|entry_size_unavailable|entry_path_truncated` (4048, 83.55x) |

## Population gate and limitations

Positive duration targets scored: **974169**; zero targets: **3264** (3264 known fork/clone/thread lineage records); censored: **0**; negative: **0**.
Status counts were success **895425** and failure **82008**; status was not a feature. Perf-buffer losses, callback errors, pending losses, and range mismatches were **0 / 0 / 0 / 0**.
The sample is an ABI, leakage, and mechanism comparison on four fixed traces. It does not establish all-production CPU accuracy, cross-hardware transfer, a prospective pre-execution predictor, or the assignment's complete CPU+GPU+E2E acceptance gate. The robust representative is optimized only on each fold's training targets and may overfit their within-25% count; it is a candidate for additional fixed held-out validation.

`run_cpu_mechanisms.py` is the reproducible helper. Its default raw-read bound is 400,000,000 bytes and it does not write a prediction JSONL stream.
