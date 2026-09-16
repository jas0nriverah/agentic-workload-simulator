# PDF-NORMATIVE REQUIREMENTS

Original two-page PDF: `/home/riverahernandezjason/Coding tests Harrdware (2).pdf`. SHA-256 `02f485cdd8cca4aae242d2f1a5306184a95d7ee0fae42527d7153f48a3424761`. Both rendered pages, including the figure examples, were read in full on September 9.

This is the human-readable rendering of [the normative register](../configs/pdf_normative_requirements.v1.json). It contains only literal PDF requirements and derivations personally approved by the main agent. A/B requirements can block acquisition only within the concrete scope stated below. Final report, figure and prediction checks remain final-deliverable checks when they need collected data.

## A01 — Deliver the repository link and written report.

**A · PDF page 1, TODO.**

> Finish the following deliverables (link the repo) and a write-up report

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A02 — Produce clear, bold, publication-quality figures.

**A · PDF page 1, TODO.**

> When plotting the figures, please ensure that the fonts, axes, labels, tick labels, titles, and plot frames are bold, clear, and visually appealing. The figures should have a polished, publication-quality appearance.

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A03 — Profile both benchmark suites and reproduce the public scoreboard accuracy and E2E latency first. An unavailable reference metric or unmatched result remains a limitation, not proof of successful reproduction.

**A · PDF page 1, Step 1.**

> Profile the SWE-bench-Lite/Verified on any GPU/CPU combination and reproduce the accuracy and end-to-end latency reported on the public scoreboard first (https://www.swebench.com/).

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A04 — Use SWE-agent.

**A · PDF page 1, Step 1 / Agent.**

> Agent: Use SWE-agent

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A05 — Use a Qwen model; the listed checkpoints are suggestions.

**A · PDF page 1, Step 1 / Model.**

> Model: Choose one from Qwen, here are some suggestions:

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A06 — Include Lite and Verified.

**A · PDF page 1, Step 1 / Datasets.**

> Datasets: SWE-bench-Lite/Verified (Lite and Verified)

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A07 — Use vLLM.

**A · PDF page 1, Step 1 / Serving engine.**

> Serving engine: vllm

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A08 — Report resolved rate and mean E2E separately for both suites, in text.

**A · PDF page 1, Deliverable 1.**

> Report the accuracy (resolved rate) and average end-to-end latency for Lite and Verified (text only).

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A09 — Produce a per-sample ratio/category scatter plot.

**A · PDF page 1, Deliverable 2.**

> Categorize the repositories into different categories and plot a figure with the CPU-to-GPU latency ratio on the x-axis and the different categories on the y-axis, with each dot representing a sample from the corresponding category.

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A10 — Reproduce the three illustrated relationships: category accuracy versus average latency; category accuracy versus CPU–GPU latency; individual latency versus CPU–GPU latency, with category grouping. The figure labels read “Acc”, “Avg. Latency”, “CPU-GPU Latency”, “Latency”, “category”, and “each data point”.

**A · PDF page 1, Deliverable 3; figure continues at top of page 2.**

> Plot the following three figures after categorization.

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A11 — Explain category-dependent CPU/GPU ratios.

**A · PDF page 1, Step 1 / Deliverable 4.**

> Write up your observations and explain in detail what causes different categories to have different ratios

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A12 — Sweep four hyperparameters over multiple values and show accuracy/latency trade-offs.

**A · PDF page 2, Step 2.**

> Select four hyperparameters, sweep over different values for each hyperparameter, and plot the resulting accuracy–latency trade-off.

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A13 — For each parameter, reproduce the illustrated category accuracy/average-latency, category accuracy/CPU–GPU-latency, and individual latency/CPU–GPU-latency relationships across parameter values.

**A · PDF page 2, Step 2 / Deliverable 4 and its figure.**

> For each hyperparameter, plots the following

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A14 — Attempt the combined figure summarizing all four parameters; retain the literal “Try” wording.

**A · PDF page 2, Deliverable 5.**

> Try to plot all parameters on on one figure (This is to assess your summarization ability)

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A15 — Explain the observed hyperparameter trade-offs.

**A · PDF page 2, Deliverable 6.**

> Write up your observations and explain in detail why

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A16 — Select high-CPU/GPU-ratio instances for Step 3; the PDF specifies no numerical high-ratio cutoff.

**A · PDF page 2, Step 3 / Deliverable 7.**

> For instances with a high CPU-to-GPU latency ratio

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A17 — For the Step-3 instances, retain individual CPU/GPU events and produce the E2E breakdown.

**A · PDF page 2, Deliverable 7.**

> plot the end-to-end latency breakdown and log each CPU event (e.g., reading files, writing files, and traversing the file system) and GPU event (e.g., input tokens, output tokens, and context length).

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A18 — Explain individual-event contributions within a selected instance.

**A · PDF page 2, Deliverable 8.**

> Since each event has been logged, focus on the events within a single instance and explain the source of its end-to-end latency in detail.

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A19 — Analyze token/context effects and relate inference latency to model/hardware work; the bandwidth formula is an example, not a requirement to collect a particular hardware counter.

**A · PDF page 2, Deliverable 8 / GPU example.**

> For example, for GPU events, analyze the effects of input-token count, output-token count, and context length. Since LLM inference is often memory-bandwidth-bound, relate the GPU latency to factors such as model size and effective memory bandwidth (e.g., model weights divided by effective bandwidth).

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A20 — Analyze individual CPU operation latency and its contribution.

**A · PDF page 2, Deliverable 8 / CPU.**

> For CPU events, analyze the latency of individual operations, such as file reads, file writes, and file-system traversal, and explain how these operations contribute to the overall latency.

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A21 — Separate workload/event models from replaceable hardware-specific parameters.

**A · PDF page 2, Step 4.**

> After Step 3, you will have models for all events on a specific CPU–GPU platform. To characterize the workload on a different platform, simply update the hardware-specific parameters; the same models can then be used to predict event latencies across platforms.

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A22 — Expose the CPU/GPU parameters actually relevant to the simulator models.

**A · PDF page 2, Deliverable 9.**

> Exposes all relevant CPU and GPU hardware parameters so that different hardware configurations can be easily plugged in.

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A23 — The simulator must support all Step-1–3 figures.

**A · PDF page 2, Deliverable 9.**

> it should be able to generate all figures and plots from Steps 1–3.

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## A24 — Evaluate each individual event and E2E prediction against 25%; mean error or fraction passing cannot substitute for the universal requirement.

**A · PDF page 2, Deliverable 9.**

> We will run SWE-bench on our lab’s server and evaluate the accuracy of the simulator’s latency models and predictions. Please ensure that the modeling error for each individual event is within 25%, and that the resulting end-to-end latency prediction error is also within 25%..

**Execution blocking scope:** Only an acquisition deficiency that prevents this deliverable; final report/figure/model acceptance is not automatically a prelaunch prerequisite.

## B01 — Valid benchmark outcomes and denominators

**B · Main-approved derivation.**

PDF A03, A06, A08 -> Separate suite resolved rates and means -> Case/suite membership, evaluated patch outcome and declared timing population must be identifiable; infrastructure failure cannot silently become a resolved/unresolved outcome or disappear from the denominator. -> Use valid SWE-bench outcome evaluation and retain enough case/patch/result association to reproduce the reported denominators and means.

**Execution blocking scope:** Execution would lose outcome/identity/timing evidence or systematically run/evaluate the wrong tasks. An exact harness commit/hash mechanism is not the requirement.

## B02 — Honest scoreboard comparison and E2E definition

**B · Main-approved derivation.**

PDF A03, A08 -> Comparable reproduction text -> The compared scoreboard entry, conditions and available metrics must be identifiable, and each reported E2E sample must have a consistent observed boundary. -> Retain actual elapsed times and settings; compare with a supported public entry and disclose material differences or unavailable public latency instead of inventing a match.

**Execution blocking scope:** A timing boundary is absent/inconsistent in a way that cannot be repaired from raw data. Looking up the public entry and writing the comparison can occur after collection.

## B03 — Per-sample categorized timing

**B · Main-approved derivation.**

PDF A09, A10, A11, A13 -> Each required dot/category aggregate and ratio -> Outcomes, repository/category assignment and CPU/GPU timing must join to the same samples and parameter settings. -> Retain sample identity and the component measurements needed for CPU/GPU ratios and all illustrated relationships; choose and label timing semantics consistently.

**Execution blocking scope:** One required axis or its sample linkage would be unavailable after the run. Category names and visual styling may be derived afterward.

## B04 — Individual event reconstruction

**B · Main-approved derivation.**

PDF A17, A18, A20, A24 -> CPU/GPU event logs, individual latency analysis and individual prediction checks -> Individual relevant operations must be identifiable and timed, associated with the case/work and terminal outcome; aggregates alone cannot reconstruct each operation. -> Retain individual CPU operation and inference-event identity, durations or valid start/end intervals, observed work descriptors and completion/failure/censor information for the required Step-3 and simulator traces.

**Execution blocking scope:** Required individual events are lost, replaced solely by aggregates, or irrecoverably misattributed. The PDF does not require raw syscall capture on every one of 1088 cases, a named backend, or one universal syscall-to-model grouping.

## B05 — Valid timing attribution and composition

**B · Main-approved derivation.**

PDF A09, A17, A18, A24 -> CPU/GPU ratios, E2E breakdown and predicted E2E -> Durations must describe the claimed work; union/composition cannot mix incompatible clocks or double-count overlapping intervals. -> Keep sufficient clock/domain/order information to compose events correctly; separate transport/host wall time from device or native-serving timing and preserve meaningful setup/retry/teardown contributions when they affect E2E.

**Execution blocking scope:** Unrecoverable missing components or invalid timing would prevent an honest breakdown/model. No 95% attribution, 5% unknown, 1 ms or 0.1% closure threshold follows from the PDF.

## B06 — Work and hardware inputs support the event models

**B · Main-approved derivation.**

PDF A17, A19, A20, A21, A22, A24 -> Detailed event explanation and hardware-parameterized predictions -> Token/context/output work, CPU operation work and the relevant model/platform characteristics must remain observable or defensibly derivable. -> Retain the workload inputs and hardware/model characteristics used by the chosen model; distinguish measured, derived and assumed quantities, and retain unreconstructable inputs during acquisition.

**Execution blocking scope:** A necessary explanatory/model input would be absent after execution. Exact scripts, mount snapshots, cache details or counters are mandatory only where their absence actually prevents the selected explanation/model; no blanket maximal telemetry obligation.

## B07 — Interpretable sweep comparisons

**B · Main-approved derivation.**

PDF A12, A13, A14, A15 -> Accuracy/latency trade-offs for four parameters -> Samples/outcomes/timing must be associated with actual parameter values and material confounding settings must be known. -> Execute identifiable multiple values of four parameters with a comparison design supporting the reported observations; keep copied baseline coordinates distinct from new executions.

**Execution blocking scope:** There are fewer than four varied parameters, no outcome/timing linkage, or irrecoverable confounding prevents the claimed comparison. Exact grids, 24 tasks, 288 sweeps, 96 copied coordinates and a separate 96-case configuration campaign are not PDF necessities.

## B08 — Measurement validity under perturbation

**B · Main-approved derivation.**

PDF A03, A09, A17, A18, A24 -> Characterization and models of the intended agent workload -> Acquisition-added work must not be silently presented as agent work, and instrumentation must not materially alter event behavior in a way that invalidates the intended conclusions. -> Use observed perturbation evidence to distinguish collection cost from workload execution and determine whether event timings, distributions and sequencing support the claimed characterization. Correct a demonstrated validity defect while preserving required events.

**Execution blocking scope:** Verified distortion or an unresolved demonstrated effect makes the intended measurements scientifically invalid or uninterpretable. A numeric overhead percentage alone, an uncompleted arbitrary fixture count, or a generic concern does not establish this. No replacement numeric threshold is approved.

## B09 — Non-circular prediction validation

**B · Main-approved derivation.**

PDF A21, A22, A24 -> Evidence of individual-event and E2E prediction accuracy on the evaluated workload/platform -> Predictions must be paired with actual observations and must not directly use the target timing/answer as an input or be reported as independent validation after tuning on those same labels. -> Separate fit/selection and validation evidence sufficiently for the claimed accuracy; retain per-event and E2E predictions, observations and error calculations, including failures. Hardware changes must affect relevant model terms.

**Execution blocking scope:** Execution would irreversibly destroy required labels/work inputs or expose them in a way that defeats the chosen independent test. Model fitting, prediction and the 25% final assessment belong after acquisition; exact split sizes/hash seeds and beating a legacy model are not prelaunch requirements.

## B10 — Required evidence survives and remains attributable

**B · Main-approved derivation.**

PDF A08, A17, A18, A23, A24 -> Reconstructable tables, figures, explanations and simulator validation -> Required observations must be retrievable with unambiguous case/attempt/configuration association; corrupt, duplicate or partially written records cannot be silently accepted as complete results. -> Retain and verify enough actual data and execution context to reconstruct required outputs. In a concurrent/crash-prone implementation, prevent ambiguous accepted results and recover required incomplete evidence before declaring success.

**Execution blocking scope:** The chosen path would lose or corrupt necessary evidence or confuse accepted attempts. Per-record fsync, duplicate journals, remote copies, SHA sidecars, clean Git, a particular queue, all workers ready and fixed storage reserves are implementation safeguards, not separately normative requirements.
