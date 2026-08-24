# GCP H100 Request Profile 20260823G

## Measured status

- Profile: `gcp-request-profile7`
- Requests: **31**; interval-unique vLLM metric deltas: **31/31**
- Proxy/model boundaries: **serialized_request_interval_deltas**
- GPU attribution: **NVML_utilization_integral_proxy_only_not_device_time**
- GPU time claim: **false** (NVML utilization integral is a proxy, not device/kernel execution time)
- Clock: `CLOCK_MONOTONIC_RAW`; host/boot identity retained in JSON
- Official evaluator: generated patch unresolved (see report hash/path)

## Aggregate measured values

- Proxy duration: 58.788 s
- Prompt tokens: 432289
- Completion tokens: 8388
- NVML utilization-integral proxy: 44.193 s

## Per-request evidence

| # | duration ms | prompt | completion | e2e s | inference s | TTFT s | TPOT s | NVML util integral s | unique |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| 1 | 482.6909 | 1621 | 76 | 0.4724 | 0.4713 | 0.0138 | 0.4589 | 0.2009 | yes |
| 2 | 328.6000 | 2774 | 50 | 0.3182 | 0.3167 | 0.0135 | 0.3049 | 0.0763 | yes |
| 3 | 375.0300 | 3579 | 57 | 0.3640 | 0.3625 | 0.0174 | 0.3467 | 0.1481 | yes |
| 4 | 413.9143 | 7645 | 60 | 0.3957 | 0.3939 | 0.0183 | 0.3779 | 0.1378 | yes |
| 5 | 445.9418 | 9887 | 64 | 0.4250 | 0.4229 | 0.0184 | 0.4069 | 0.2144 | yes |
| 6 | 305.3753 | 10065 | 43 | 0.2840 | 0.2820 | 0.0119 | 0.2723 | 0.0661 | yes |
| 7 | 328.7330 | 10132 | 46 | 0.3055 | 0.3035 | 0.0143 | 0.2915 | 0.0919 | yes |
| 8 | 397.8514 | 10489 | 56 | 0.3757 | 0.3737 | 0.0182 | 0.3576 | 0.1519 | yes |
| 9 | 401.4042 | 11209 | 56 | 0.3769 | 0.3750 | 0.0183 | 0.3590 | 0.1322 | yes |
| 10 | 417.2896 | 11906 | 59 | 0.3926 | 0.3907 | 0.0123 | 0.3804 | 0.1431 | yes |
| 11 | 450.6406 | 12400 | 63 | 0.4250 | 0.4228 | 0.0188 | 0.4062 | 0.1695 | yes |
| 12 | 312.2075 | 12487 | 42 | 0.2858 | 0.2838 | 0.0166 | 0.2696 | 0.0903 | yes |
| 13 | 326.4591 | 12553 | 44 | 0.2999 | 0.2979 | 0.0185 | 0.2820 | 0.0952 | yes |
| 14 | 299.8781 | 12621 | 40 | 0.2741 | 0.2722 | 0.0180 | 0.2561 | 0.0712 | yes |
| 15 | 465.1206 | 12685 | 65 | 0.4391 | 0.4372 | 0.0193 | 0.4202 | 0.1916 | yes |
| 16 | 2304.4822 | 13434 | 345 | 2.2767 | 2.2749 | 0.0187 | 2.2583 | 1.7472 | yes |
| 17 | 421.7331 | 13875 | 58 | 0.3932 | 0.3909 | 0.0162 | 0.3772 | 0.1217 | yes |
| 18 | 583.5240 | 14204 | 82 | 0.5552 | 0.5530 | 0.0197 | 0.5356 | 0.2847 | yes |
| 19 | 397.3595 | 14471 | 54 | 0.3685 | 0.3661 | 0.0164 | 0.3519 | 0.1306 | yes |
| 20 | 438.5435 | 14552 | 60 | 0.4090 | 0.4067 | 0.0166 | 0.3926 | 0.1699 | yes |
| 21 | 2700.3509 | 15526 | 398 | 2.6676 | 2.6654 | 0.0172 | 2.6505 | 2.1430 | yes |
| 22 | 251.9596 | 15955 | 30 | 0.2202 | 0.2180 | 0.0247 | 0.1958 | 0.0448 | yes |
| 23 | 9351.3189 | 16226 | 1380 | 9.3194 | 9.3163 | 0.0389 | 9.2796 | 7.8721 | yes |
| 24 | 270.3298 | 17638 | 32 | 0.2357 | 0.2335 | 0.0249 | 0.2111 | 0.0000 | yes |
| 25 | 3410.3562 | 18866 | 486 | 3.3735 | 3.3712 | 0.0922 | 3.2818 | 2.6632 | yes |
| 26 | 14118.1878 | 19769 | 2048 | 14.0791 | 14.0765 | 0.0518 | 14.0274 | 11.9540 | yes |
| 27 | 282.7307 | 21857 | 31 | 0.2422 | 0.2397 | 0.0296 | 0.2130 | 0.0498 | yes |
| 28 | 1330.4528 | 22549 | 173 | 1.2883 | 1.2860 | 0.0778 | 1.2110 | 0.9097 | yes |
| 29 | 303.1031 | 22909 | 31 | 0.2602 | 0.2578 | 0.0458 | 0.2148 | 0.0000 | yes |
| 30 | 2312.8936 | 23725 | 311 | 2.2638 | 2.2614 | 0.0858 | 2.1784 | 1.7203 | yes |
| 31 | 14559.5668 | 24680 | 2048 | 14.5140 | 14.5116 | 0.0792 | 14.4351 | 12.4015 | yes |

## Interpretation

This profile closes the request-boundary and serialized aggregate latency evidence gap: each request has a same-host monotonic proxy interval, token counts, and (where the scrape interval uniquely brackets one completion) vLLM cumulative latency deltas. The NVML integral is retained only as an interval utilization proxy. It must not be presented as per-request GPU execution time or as a replacement for kernel-level attribution.

Raw JSONL stays on the VM under the untracked experiment artifact directory; only this compact manifest/report and hashes are tracked.
