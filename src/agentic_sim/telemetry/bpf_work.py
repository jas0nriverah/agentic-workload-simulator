"""Privileged BCC Linux work collection for persistent runtime processes.

This module is the low-overhead production candidate for CPU work descriptors.
The collector attaches BCC tracepoints to an already-running persistent shell,
keeps aggregation in kernel maps, and exposes only explicit action-boundary
operations to the runtime.  It never wraps an action, starts a replacement
shell, or turns a command string into a file/work estimate.

The BPF clock is ``bpf_ktime_get_ns`` (kernel ``CLOCK_MONOTONIC``).  It is used
only for syscall latency sums and kernel lineage timestamps.  Runtime action
boundaries retain their caller-supplied wall/monotonic anchors separately;
kernel timestamps are never subtracted from ``CLOCK_MONOTONIC_RAW`` values.

The service/client pair lets a root-owned collector receive guarded action
boundaries from an unprivileged instrumentation hook over a Unix socket.  The
socket protocol is intentionally small and binds every request to the
collector's PID/start-ticks/boot/namespace and run/attempt/case identity.
"""

from __future__ import annotations

import argparse
import ctypes as ct
import gc
import hashlib
import io
import json
import os
import platform
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .clock import clock_fields, monotonic_ns
from .linux_work import (
    ActionBoundary,
    BoundaryJournal,
    CollectorAttachError,
    IdentityBindingError,
    LinuxWorkError,
    ProcessIdentity,
    ProcessTarget,
    _append_jsonl,
    _canonical_json,
    _read_boot_id,
    _read_proc_stat,
    _sha256_path,
)
from .native_bpf_sink import (
    DEFAULT_PERF_BUFFER_PAGES_PER_CPU,
    perf_buffer_descriptor,
)


BPF_COLLECTOR_SCHEMA = "assignment.linux-bpf-work-collector.v1"
BPF_RAW_SCHEMA = "assignment.linux-bpf-work-raw.v2"
BPF_SUMMARY_SCHEMA = "assignment.linux-bpf-work-summary.v1"
BPF_SOCKET_SCHEMA = "assignment.linux-bpf-work-socket.v1"
BPF_SERVICE_LIFECYCLE_SCHEMA = "assignment.linux-bpf-work-service-lifecycle.v1"
BPF_PATH_CAP = 128
BPF_FLUSH_TIMEOUT_S = 0.250
BPF_EVENT_SCHEMA_LEGACY = "assignment.linux-bpf-work-event.v2"
BPF_EVENT_SCHEMA = "assignment.linux-bpf-work-event.v3"
BPF_EVENT_ABI = "assignment.linux-bpf-work-scalar-args.v1"
BPF_DERIVED_SCHEMA = "assignment.linux-bpf-work-derived.v1"
BPF_CWD_SCHEMA = "assignment.persistent-shell-cwd.v1"
# Keep the native and Python BCC readers on the same burst-capacity contract.
# The actual successfully-opened value is retained in every collector
# manifest; setup failure is never allowed to silently fall back to 64 pages.
BPF_PERF_BUFFER_PAGES_PER_CPU = DEFAULT_PERF_BUFFER_PAGES_PER_CPU


try:  # BCC is intentionally optional for offline imports and unit tests.
    from bcc import BPF as _BCCBPF
except ImportError:  # pragma: no cover - exercised on non-BCC hosts
    _BCCBPF = None


# BCC's TRACEPOINT_PROBE functions are autoloaded by BPF(text=...).  Keep the
# C program in one stable string so its digest can bind every raw aggregate to
# the exact program that produced it.  The maps are intentionally bounded: a
# full path record is retained when capacity permits, and map loss is counted.
BPF_PROGRAM = r"""
#include <uapi/linux/ptrace.h>
#include <linux/sched.h>
#include <asm/unistd.h>

#define PATH_CAP 128
#define PATH_OBSERVED 1
#define PATH_TRUNCATED 2
#define PATH_UNKNOWN 3

#define EVENT_SUCCESS 1
#define EVENT_FAILURE 2

#define K_READ 1
#define K_WRITE 2
#define K_OPEN 3
#define K_STAT 4
#define K_GETDENTS 5
#define K_CLOSE 6
#define K_EXEC 7
#define K_FORK 8
#define K_CLONE 9
#define K_THREAD 10
#define K_MSG_READ 11
#define K_MSG_WRITE 12
#define K_MUTATION 13
#define K_RENAME 14
#define K_MMAP 15
#define K_MSYNC 16

struct action_state {
    u32 root_pid;
    u32 closing;
    u64 in_flight;
    u64 started_ns;
};

struct proc_action {
    u64 token;
    u64 birth_ns;
    u32 parent_tgid;
};

struct pending_syscall {
    u64 token;
    u64 started_ns;
    u32 syscall_nr;
    u32 kind;
    s32 fd;
    u32 path_status;
    u32 path_len;
    u32 fd_path_backed;
    u32 parent_tgid;
    char path[PATH_CAP];
    u32 path2_status;
    u32 path2_len;
    char path2[PATH_CAP];
    /* Raw tracepoint scalar arguments.  These are copied verbatim and are
     * never dereferenced; the userspace decoder gives them syscall-specific
     * names.  The fixed six-slot payload is the v3 event ABI. */
    u64 raw_args[6];
};

struct pending_clone {
    u64 token;
    u32 classification;
    u32 fork_seen;
};

struct fd_key {
    u32 tgid;
    s32 fd;
};

struct fd_value {
    u64 token;
    u32 path_status;
    u32 path_len;
    char path[PATH_CAP];
};

/* This is the stable full-fidelity record sent through the perf buffer. */
struct work_event {
    u64 token;
    u64 sequence;
    u64 kernel_start_ns;
    u64 kernel_end_ns;
    s64 ret;
    u32 syscall_nr;
    u32 tgid;
    u32 tid;
    u32 parent_tgid;
    u32 kind;
    u32 status;
    u32 path_status;
    u32 path_len;
    u32 child_pid;
    s32 fd;
    u32 reserved;
    char path[PATH_CAP];
    u32 path2_status;
    u32 path2_len;
    char path2[PATH_CAP];
    u64 raw_args[6];
};

struct action_aggregate {
    u64 tracked_syscall_count;
    u64 read_syscall_count;
    u64 write_syscall_count;
    u64 open_count;
    u64 stat_count;
    u64 getdents_count;
    u64 close_count;
    u64 exec_count;
    u64 fork_count;
    u64 thread_count;
    u64 unclassified_fork_count;
    u64 failed_syscall_count;
    u64 returned_read_bytes;
    u64 returned_write_bytes;
    u64 path_backed_read_bytes;
    u64 path_backed_write_bytes;
    u64 unknown_fd_read_bytes;
    u64 unknown_fd_write_bytes;
    u64 message_read_count;
    u64 message_write_count;
    u64 getdents_bytes;
    u64 syscall_latency_ns;
    u64 read_latency_ns;
    u64 write_latency_ns;
    u64 open_latency_ns;
    u64 stat_latency_ns;
    u64 getdents_latency_ns;
    u64 path_sequence;
    u64 event_sequence;
    u64 required_event_count;
    u64 lost_event_records;
    u64 lost_path_records;
    u64 lost_pending_records;
    u64 lineage_map_failures;
    u64 censored_pending_records;
    u64 mutation_count;
    u64 mmap_count;
    u64 msync_count;
    u64 mutation_latency_ns;
    u64 mmap_latency_ns;
    u64 msync_latency_ns;
};

BPF_HASH(active_actions, u64, struct action_state, 4096);
BPF_HASH(closed_actions, u64, u32, 4096);
BPF_HASH(proc_actions, u32, struct proc_action, 65536);
BPF_HASH(pending_syscalls, u32, struct pending_syscall, 65536);
BPF_HASH(pending_clones, u32, struct pending_clone, 65536);
BPF_HASH(fd_paths, struct fd_key, struct fd_value, 65536);
BPF_HASH(aggregates, u64, struct action_aggregate, 4096);
/* A work_event is 400 bytes in the v3 ABI.  Keep it in per-CPU scratch
 * storage rather than on the tracepoint stack: raw_syscalls:sys_exit already
 * has pending/clone locals, and the kernel rejects a combined stack frame
 * above 512 bytes.  perf_submit copies the value before this CPU can reuse
 * the slot for the next event. */
BPF_PERCPU_ARRAY(work_event_scratch, struct work_event, 1);
BPF_PERF_OUTPUT(work_events);

static __always_inline int current_action(
    u32 *tgid, u32 *tid, u64 *token, u32 *parent_tgid,
    struct action_state **action) {
    u64 id = bpf_get_current_pid_tgid();
    *tgid = id >> 32;
    *tid = (u32)id;
    struct proc_action *proc = proc_actions.lookup(tgid);
    if (!proc)
        return 0;
    struct action_state *state = active_actions.lookup(&proc->token);
    /* Once an action closes, keep already-mapped descendants on the same
     * token so background work can be captured until quiescence/collector
     * stop.  The persistent root shell is excluded from starting new work;
     * its syscall that was already pending still finishes against the token. */
    if (!state || (closed_actions.lookup(&proc->token) && *tgid == state->root_pid))
        return 0;
    *token = proc->token;
    *parent_tgid = proc->parent_tgid;
    *action = state;
    return 1;
}

static __always_inline struct action_aggregate *aggregate_for(u64 token) {
    return aggregates.lookup(&token);
}

static __always_inline int read_path(
    char *destination, u32 *status, u32 *length, const char *source) {
    int result = bpf_probe_read_user_str(destination, PATH_CAP, source);
    if (result < 0) {
        *status = PATH_UNKNOWN;
        *length = 0;
        destination[0] = 0;
        return result;
    }
    if (result >= PATH_CAP) {
        *status = PATH_TRUNCATED;
        *length = PATH_CAP - 1;
        destination[PATH_CAP - 1] = 0;
        return result;
    }
    *status = PATH_OBSERVED;
    *length = result > 0 ? result - 1 : 0;
    return result;
}

static __always_inline void record_path(
    u64 token, u32 tgid, u32 tid, s32 fd, u32 kind,
    u32 status, u32 length, const char *path) {
    /* Full path bytes/status already travel in work_event.  A second kernel
     * path map only duplicates every descriptor and forces a costly map scan
     * at each action boundary, so retain this ABI-compatible no-op while
     * deriving path descriptors from the durable event stream. */
    (void)token;
    (void)tgid;
    (void)tid;
    (void)fd;
    (void)kind;
    (void)status;
    (void)length;
    (void)path;
}

static __always_inline int begin_syscall(
    u32 syscall_nr, u32 kind, s32 fd, const unsigned long *raw_args) {
    u32 tgid, tid, parent_tgid;
    u64 token;
    struct action_state *action;
    if (!current_action(&tgid, &tid, &token, &parent_tgid, &action))
        return 0;
    struct action_aggregate *aggregate = aggregate_for(token);
    if (!aggregate)
        return 0;
    struct pending_syscall pending = {};
    pending.token = token;
    pending.started_ns = bpf_ktime_get_ns();
    pending.syscall_nr = syscall_nr;
    pending.kind = kind;
    pending.fd = fd;
    pending.path_status = PATH_UNKNOWN;
    pending.path2_status = PATH_UNKNOWN;
    pending.parent_tgid = parent_tgid;
    if (raw_args) {
        pending.raw_args[0] = (u64)raw_args[0];
        pending.raw_args[1] = (u64)raw_args[1];
        pending.raw_args[2] = (u64)raw_args[2];
        pending.raw_args[3] = (u64)raw_args[3];
        pending.raw_args[4] = (u64)raw_args[4];
        pending.raw_args[5] = (u64)raw_args[5];
    }
    struct fd_key fdkey = {.tgid = tgid, .fd = fd};
    if (fd >= 0) {
        struct fd_value *fdvalue = fd_paths.lookup(&fdkey);
        if (fdvalue && fdvalue->token == token && fdvalue->path_status != PATH_UNKNOWN) {
            pending.fd_path_backed = 1;
            pending.path_status = fdvalue->path_status;
            pending.path_len = fdvalue->path_len;
            __builtin_memcpy(&pending.path, &fdvalue->path, PATH_CAP);
        }
    }
    struct pending_syscall *old = pending_syscalls.lookup(&tid);
    if (old) {
        struct action_aggregate *old_aggregate = aggregate_for(old->token);
        if (old_aggregate)
            __sync_fetch_and_add(&old_aggregate->lost_pending_records, 1);
        pending_syscalls.delete(&tid);
    }
    int result = pending_syscalls.update(&tid, &pending);
    if (result < 0) {
        __sync_fetch_and_add(&aggregate->lost_pending_records, 1);
        return 0;
    }
    __sync_fetch_and_add(&action->in_flight, 1);
    __sync_fetch_and_add(&aggregate->tracked_syscall_count, 1);
    if (kind == K_READ || kind == K_MSG_READ)
        __sync_fetch_and_add(&aggregate->read_syscall_count, 1);
    if (kind == K_WRITE || kind == K_MSG_WRITE)
        __sync_fetch_and_add(&aggregate->write_syscall_count, 1);
    if (kind == K_OPEN)
        __sync_fetch_and_add(&aggregate->open_count, 1);
    if (kind == K_STAT)
        __sync_fetch_and_add(&aggregate->stat_count, 1);
    if (kind == K_GETDENTS)
        __sync_fetch_and_add(&aggregate->getdents_count, 1);
    if (kind == K_CLOSE)
        __sync_fetch_and_add(&aggregate->close_count, 1);
    if (kind == K_MUTATION || kind == K_RENAME)
        __sync_fetch_and_add(&aggregate->mutation_count, 1);
    if (kind == K_MMAP)
        __sync_fetch_and_add(&aggregate->mmap_count, 1);
    if (kind == K_MSYNC)
        __sync_fetch_and_add(&aggregate->msync_count, 1);
    return 0;
}

static __always_inline void emit_syscall_event_status(
    void *ctx, struct pending_syscall *pending, long ret, u32 status) {
    struct action_aggregate *aggregate = aggregate_for(pending->token);
    if (!aggregate)
        return;
    u64 sequence = bpf_ktime_get_ns();
    __sync_fetch_and_add(&aggregate->event_sequence, 1);
    u32 scratch_key = 0;
    struct work_event *event = work_event_scratch.lookup(&scratch_key);
    if (!event) {
        /* A one-entry per-CPU array should be available for every CPU.  If a
         * lookup nevertheless fails, retain an explicit loss count instead of
         * emitting a partial or fabricated record. */
        __sync_fetch_and_add(&aggregate->lost_event_records, 1);
        return;
    }
    __builtin_memset(event, 0, sizeof(*event));
    event->token = pending->token;
    event->sequence = sequence;
    event->kernel_start_ns = pending->started_ns;
    event->kernel_end_ns = bpf_ktime_get_ns();
    event->ret = (s64)ret;
    event->syscall_nr = pending->syscall_nr;
    u64 id = bpf_get_current_pid_tgid();
    event->tgid = id >> 32;
    event->tid = (u32)id;
    event->parent_tgid = pending->parent_tgid;
    event->kind = pending->kind;
    event->status = status;
    event->path_status = pending->path_status;
    event->path_len = pending->path_len;
    event->path2_status = pending->path2_status;
    event->path2_len = pending->path2_len;
    event->fd = pending->fd;
    event->child_pid = 0;
    event->raw_args[0] = pending->raw_args[0];
    event->raw_args[1] = pending->raw_args[1];
    event->raw_args[2] = pending->raw_args[2];
    event->raw_args[3] = pending->raw_args[3];
    event->raw_args[4] = pending->raw_args[4];
    event->raw_args[5] = pending->raw_args[5];
    if (pending->path_status != PATH_UNKNOWN && pending->path_len > 0)
        __builtin_memcpy(&event->path, &pending->path, PATH_CAP);
    if (pending->path2_status != PATH_UNKNOWN && pending->path2_len > 0)
        __builtin_memcpy(&event->path2, &pending->path2, PATH_CAP);
    __sync_fetch_and_add(&aggregate->required_event_count, 1);
    work_events.perf_submit(ctx, event, sizeof(*event));
}

static __always_inline void emit_syscall_event(
    void *ctx, struct pending_syscall *pending, long ret) {
    emit_syscall_event_status(ctx, pending, ret, ret < 0 ? EVENT_FAILURE : EVENT_SUCCESS);
}

static __always_inline int finish_syscall(void *ctx, u32 kind, long ret) {
    u64 id = bpf_get_current_pid_tgid();
    u32 tid = (u32)id;
    struct pending_syscall *pending = pending_syscalls.lookup(&tid);
    if (!pending || pending->kind != kind)
        return 0;
    u64 token = pending->token;
    u64 latency = bpf_ktime_get_ns() - pending->started_ns;
    struct action_aggregate *aggregate = aggregate_for(token);
    if (aggregate) {
        __sync_fetch_and_add(&aggregate->syscall_latency_ns, latency);
        if (kind == K_READ || kind == K_MSG_READ)
            __sync_fetch_and_add(&aggregate->read_latency_ns, latency);
        if (kind == K_WRITE || kind == K_MSG_WRITE)
            __sync_fetch_and_add(&aggregate->write_latency_ns, latency);
        if (kind == K_OPEN)
            __sync_fetch_and_add(&aggregate->open_latency_ns, latency);
        if (kind == K_STAT)
            __sync_fetch_and_add(&aggregate->stat_latency_ns, latency);
        if (kind == K_GETDENTS)
            __sync_fetch_and_add(&aggregate->getdents_latency_ns, latency);
        if (kind == K_MUTATION || kind == K_RENAME)
            __sync_fetch_and_add(&aggregate->mutation_latency_ns, latency);
        if (kind == K_MMAP)
            __sync_fetch_and_add(&aggregate->mmap_latency_ns, latency);
        if (kind == K_MSYNC)
            __sync_fetch_and_add(&aggregate->msync_latency_ns, latency);
        if (ret < 0) {
            __sync_fetch_and_add(&aggregate->failed_syscall_count, 1);
        } else if (kind == K_READ) {
            __sync_fetch_and_add(&aggregate->returned_read_bytes, ret);
            if (pending->fd_path_backed)
                __sync_fetch_and_add(&aggregate->path_backed_read_bytes, ret);
            else
                __sync_fetch_and_add(&aggregate->unknown_fd_read_bytes, ret);
        } else if (kind == K_WRITE) {
            __sync_fetch_and_add(&aggregate->returned_write_bytes, ret);
            if (pending->fd_path_backed)
                __sync_fetch_and_add(&aggregate->path_backed_write_bytes, ret);
            else
                __sync_fetch_and_add(&aggregate->unknown_fd_write_bytes, ret);
        } else if (kind == K_MSG_READ) {
            __sync_fetch_and_add(&aggregate->message_read_count, ret);
        } else if (kind == K_MSG_WRITE) {
            __sync_fetch_and_add(&aggregate->message_write_count, ret);
        } else if (kind == K_GETDENTS) {
            __sync_fetch_and_add(&aggregate->getdents_bytes, ret);
        } else if (kind == K_EXEC && ret == 0) {
            __sync_fetch_and_add(&aggregate->exec_count, 1);
        }
        emit_syscall_event(ctx, pending, ret);
    }
    struct action_state *action = active_actions.lookup(&token);
    if (action && action->in_flight > 0)
        __sync_fetch_and_add(&action->in_flight, (u64)-1);
    pending_syscalls.delete(&tid);
    return 0;
}

static __always_inline int finish_open(void *ctx, long ret) {
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    u32 tid = (u32)id;
    struct pending_syscall *pending = pending_syscalls.lookup(&tid);
    if (pending && pending->kind == K_OPEN && ret >= 0 && pending->path_status != PATH_UNKNOWN) {
        struct fd_key key = {.tgid = tgid, .fd = (s32)ret};
        struct fd_value value = {};
        value.token = pending->token;
        value.path_status = pending->path_status;
        value.path_len = pending->path_len;
        __builtin_memcpy(&value.path, &pending->path, PATH_CAP);
        int result = fd_paths.update(&key, &value);
        if (result < 0) {
            struct action_aggregate *aggregate = aggregate_for(pending->token);
            if (aggregate)
                __sync_fetch_and_add(&aggregate->lineage_map_failures, 1);
        }
    }
    return finish_syscall(ctx, K_OPEN, ret);
}

static __always_inline int finish_close(void *ctx, long ret) {
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    u32 tid = (u32)id;
    struct pending_syscall *pending = pending_syscalls.lookup(&tid);
    if (pending && pending->kind == K_CLOSE && ret == 0) {
        struct fd_key key = {.tgid = tgid, .fd = pending->fd};
        fd_paths.delete(&key);
    }
    return finish_syscall(ctx, K_CLOSE, ret);
}

static __always_inline int begin_user_paths(
    u32 syscall_nr, u32 kind, s32 fd,
    const char *source, const char *source2,
    const unsigned long *raw_args) {
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    u32 tid = (u32)id;
    begin_syscall(syscall_nr, kind, fd, raw_args);
    struct pending_syscall *pending = pending_syscalls.lookup(&tid);
    if (!pending || pending->kind != kind)
        return 0;
    read_path(pending->path, &pending->path_status, &pending->path_len, source);
    record_path(pending->token, tgid, tid, fd, kind, pending->path_status, pending->path_len, pending->path);
    if (source2) {
        read_path(pending->path2, &pending->path2_status, &pending->path2_len, source2);
        record_path(pending->token, tgid, tid, fd, kind, pending->path2_status, pending->path2_len, pending->path2);
    }
    return 0;
}

static __always_inline int begin_user_path(
    u32 syscall_nr, u32 kind, s32 fd, const char *source,
    const unsigned long *raw_args) {
    return begin_user_paths(syscall_nr, kind, fd, source, 0, raw_args);
}

static __always_inline int begin_getdents(
    u32 syscall_nr, s32 fd, const unsigned long *raw_args) {
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    u32 tid = (u32)id;
    begin_syscall(syscall_nr, K_GETDENTS, fd, raw_args);
    struct pending_syscall *pending = pending_syscalls.lookup(&tid);
    if (!pending || pending->kind != K_GETDENTS)
        return 0;
    struct fd_key key = {.tgid = tgid, .fd = fd};
    struct fd_value *value = fd_paths.lookup(&key);
    if (value && value->token == pending->token && value->path_status != PATH_UNKNOWN) {
        pending->path_status = value->path_status;
        pending->path_len = value->path_len;
        pending->fd_path_backed = 1;
        __builtin_memcpy(&pending->path, &value->path, PATH_CAP);
        record_path(pending->token, tgid, tid, fd, K_GETDENTS, value->path_status, value->path_len, value->path);
    } else {
        pending->path_status = PATH_UNKNOWN;
        pending->path_len = 0;
        record_path(pending->token, tgid, tid, fd, K_GETDENTS, PATH_UNKNOWN, 0, 0);
    }
    return 0;
}

static __always_inline int begin_clone(
    u32 syscall_nr, u32 kind, u32 classification,
    const unsigned long *raw_args) {
    u64 id = bpf_get_current_pid_tgid();
    u32 tid = (u32)id;
    begin_syscall(syscall_nr, kind, -1, raw_args);
    struct pending_syscall *pending = pending_syscalls.lookup(&tid);
    if (!pending || pending->kind != kind)
        return 0;
    struct pending_clone clone = {};
    clone.token = pending->token;
    clone.classification = classification;
    pending_clones.update(&tid, &clone);
    return 0;
}

static __always_inline void emit_lineage_event(
    void *ctx, u64 token, u32 parent_tgid, u32 child_pid, u32 classification) {
    struct action_aggregate *aggregate = aggregate_for(token);
    if (!aggregate)
        return;
    u64 now = bpf_ktime_get_ns();
    u64 sequence = bpf_ktime_get_ns();
    __sync_fetch_and_add(&aggregate->event_sequence, 1);
    u32 scratch_key = 0;
    struct work_event *event = work_event_scratch.lookup(&scratch_key);
    if (!event) {
        __sync_fetch_and_add(&aggregate->lost_event_records, 1);
        return;
    }
    __builtin_memset(event, 0, sizeof(*event));
    event->token = token;
    event->sequence = sequence;
    event->kernel_start_ns = now;
    event->kernel_end_ns = now;
    event->ret = child_pid;
    event->syscall_nr = 0;
    u64 id = bpf_get_current_pid_tgid();
    event->tgid = id >> 32;
    event->tid = (u32)id;
    event->parent_tgid = parent_tgid;
    event->kind = classification == 1 ? K_THREAD : (classification == 2 ? K_FORK : K_CLONE);
    event->status = EVENT_SUCCESS;
    event->path_status = PATH_UNKNOWN;
    event->path_len = 0;
    event->path2_status = PATH_UNKNOWN;
    event->path2_len = 0;
    event->child_pid = child_pid;
    event->fd = -1;
    __sync_fetch_and_add(&aggregate->required_event_count, 1);
    work_events.perf_submit(ctx, event, sizeof(*event));
}

static __always_inline void map_child_from_clone(
    void *ctx, u32 child_pid, u32 parent_tgid, u32 parent_tid,
    u64 token, u32 classification) {
    struct pending_clone *clone = pending_clones.lookup(&parent_tid);
    if (clone && clone->fork_seen)
        return;
    struct proc_action child = {
        .token = token,
        .birth_ns = bpf_ktime_get_ns(),
        .parent_tgid = parent_tgid,
    };
    if (classification != 1) {
        int result = proc_actions.update(&child_pid, &child);
        if (result < 0) {
            struct action_aggregate *aggregate = aggregate_for(token);
            if (aggregate)
                __sync_fetch_and_add(&aggregate->lineage_map_failures, 1);
        }
    }
    struct action_aggregate *aggregate = aggregate_for(token);
    if (!aggregate)
        return;
    if (classification == 1)
        __sync_fetch_and_add(&aggregate->thread_count, 1);
    else if (classification == 2)
        __sync_fetch_and_add(&aggregate->fork_count, 1);
    else
        __sync_fetch_and_add(&aggregate->unclassified_fork_count, 1);
    emit_lineage_event(ctx, token, parent_tgid, child_pid, classification);
    if (clone)
        clone->fork_seen = 1;
}

/* Raw syscall tracepoints avoid relying on generated per-syscall structs. */
TRACEPOINT_PROBE(raw_syscalls, sys_enter) {
    long nr = args->id;
    unsigned long *a = args->args;
    switch (nr) {
    case __NR_read: return begin_syscall(__NR_read, K_READ, (s32)a[0], a);
    case __NR_write: return begin_syscall(__NR_write, K_WRITE, (s32)a[0], a);
    case __NR_readv: return begin_syscall(__NR_readv, K_READ, (s32)a[0], a);
    case __NR_writev: return begin_syscall(__NR_writev, K_WRITE, (s32)a[0], a);
    case __NR_pread64: return begin_syscall(__NR_pread64, K_READ, (s32)a[0], a);
    case __NR_pwrite64: return begin_syscall(__NR_pwrite64, K_WRITE, (s32)a[0], a);
    case __NR_preadv: return begin_syscall(__NR_preadv, K_READ, (s32)a[0], a);
    case __NR_pwritev: return begin_syscall(__NR_pwritev, K_WRITE, (s32)a[0], a);
    case __NR_preadv2: return begin_syscall(__NR_preadv2, K_READ, (s32)a[0], a);
    case __NR_pwritev2: return begin_syscall(__NR_pwritev2, K_WRITE, (s32)a[0], a);
    case __NR_recvfrom: return begin_syscall(__NR_recvfrom, K_MSG_READ, (s32)a[0], a);
    case __NR_recvmsg: return begin_syscall(__NR_recvmsg, K_MSG_READ, (s32)a[0], a);
    case __NR_recvmmsg: return begin_syscall(__NR_recvmmsg, K_MSG_READ, (s32)a[0], a);
    case __NR_sendto: return begin_syscall(__NR_sendto, K_MSG_WRITE, (s32)a[0], a);
    case __NR_sendmsg: return begin_syscall(__NR_sendmsg, K_MSG_WRITE, (s32)a[0], a);
    case __NR_sendmmsg: return begin_syscall(__NR_sendmmsg, K_MSG_WRITE, (s32)a[0], a);
    case __NR_open: return begin_user_path(__NR_open, K_OPEN, -1, (const char *)a[0], a);
    case __NR_openat: return begin_user_path(__NR_openat, K_OPEN, (s32)a[0], (const char *)a[1], a);
    case __NR_openat2: return begin_user_path(__NR_openat2, K_OPEN, (s32)a[0], (const char *)a[1], a);
    case __NR_newfstatat: return begin_user_path(__NR_newfstatat, K_STAT, (s32)a[0], (const char *)a[1], a);
    case __NR_statx: return begin_user_path(__NR_statx, K_STAT, (s32)a[0], (const char *)a[1], a);
    case __NR_execve: return begin_user_path(__NR_execve, K_EXEC, -1, (const char *)a[0], a);
    case __NR_execveat: return begin_user_path(__NR_execveat, K_EXEC, (s32)a[0], (const char *)a[1], a);
    case __NR_readlink: return begin_user_path(__NR_readlink, K_STAT, -1, (const char *)a[0], a);
    case __NR_readlinkat: return begin_user_path(__NR_readlinkat, K_STAT, (s32)a[0], (const char *)a[1], a);
    case __NR_unlink: return begin_user_path(__NR_unlink, K_MUTATION, -1, (const char *)a[0], a);
    case __NR_unlinkat: return begin_user_path(__NR_unlinkat, K_MUTATION, (s32)a[0], (const char *)a[1], a);
    case __NR_mkdir: return begin_user_path(__NR_mkdir, K_MUTATION, -1, (const char *)a[0], a);
    case __NR_mkdirat: return begin_user_path(__NR_mkdirat, K_MUTATION, (s32)a[0], (const char *)a[1], a);
    case __NR_rmdir: return begin_user_path(__NR_rmdir, K_MUTATION, -1, (const char *)a[0], a);
    case __NR_truncate: return begin_user_path(__NR_truncate, K_MUTATION, -1, (const char *)a[0], a);
    case __NR_ftruncate: return begin_syscall(__NR_ftruncate, K_MUTATION, (s32)a[0], a);
    case __NR_rename: return begin_user_paths(__NR_rename, K_RENAME, -1, (const char *)a[0], (const char *)a[1], a);
    case __NR_renameat: return begin_user_paths(__NR_renameat, K_RENAME, (s32)a[0], (const char *)a[1], (const char *)a[3], a);
    case __NR_renameat2: return begin_user_paths(__NR_renameat2, K_RENAME, (s32)a[0], (const char *)a[1], (const char *)a[3], a);
    case __NR_mmap: return begin_syscall(__NR_mmap, K_MMAP, (s32)a[4], a);
    case __NR_msync: return begin_syscall(__NR_msync, K_MSYNC, -1, a);
    case __NR_getdents: return begin_getdents(__NR_getdents, (s32)a[0], a);
    case __NR_getdents64: return begin_getdents(__NR_getdents64, (s32)a[0], a);
    case __NR_close: return begin_syscall(__NR_close, K_CLOSE, (s32)a[0], a);
    case __NR_fork: return begin_clone(__NR_fork, K_FORK, 2, a);
    case __NR_vfork: return begin_clone(__NR_vfork, K_FORK, 2, a);
    case __NR_clone: {
        u32 classification = (a[0] & CLONE_THREAD) ? 1 : 2;
        return begin_clone(__NR_clone, K_CLONE, classification, a);
    }
    case __NR_clone3: {
        struct { u64 flags; } clone_args = {};
        u64 flags = 0;
        if (bpf_probe_read_user(&clone_args, sizeof(clone_args), (void *)a[0]) == 0)
            flags = clone_args.flags;
        u32 classification = (flags & CLONE_THREAD) ? 1 : (flags ? 2 : 3);
        return begin_clone(__NR_clone3, K_CLONE, classification, a);
    }
    default: return 0;
    }
}

TRACEPOINT_PROBE(raw_syscalls, sys_exit) {
    long nr = args->id;
    long ret = args->ret;
    u64 id = bpf_get_current_pid_tgid();
    u32 tid = (u32)id;
    struct pending_syscall *pending = pending_syscalls.lookup(&tid);
    if (!pending || pending->syscall_nr != (u32)nr)
        return 0;
    if (pending->kind == K_OPEN)
        finish_open(args, ret);
    else if (pending->kind == K_CLOSE)
        finish_close(args, ret);
    else if (pending->kind == K_FORK || pending->kind == K_CLONE) {
        struct pending_clone *clone = pending_clones.lookup(&tid);
        u64 token = pending->token;
        u32 classification = clone ? clone->classification : 3;
        u32 fork_seen = clone ? clone->fork_seen : 0;
        finish_syscall(args, pending->kind, ret);
        if (ret > 0 && !fork_seen)
            map_child_from_clone(args, (u32)ret, id >> 32, tid, token, classification);
        pending_clones.delete(&tid);
    } else {
        finish_syscall(args, pending->kind, ret);
    }
    return 0;
}

/* sched_process_fork runs at process creation, before the child can issue work. */
struct fork_tracepoint_args {
    u64 common;
    char parent_comm[16];
    s32 parent_pid;
    char child_comm[16];
    s32 child_pid;
};

int on_fork(struct fork_tracepoint_args *args) {
    u64 id = bpf_get_current_pid_tgid();
    u32 parent_tgid = id >> 32;
    u32 parent_tid = (u32)id;
    struct proc_action *parent = proc_actions.lookup(&parent_tgid);
    if (!parent)
        return 0;
    struct action_state *action = active_actions.lookup(&parent->token);
    /* A closing root cannot create a new descendant, while a descendant that
     * was mapped before closure may continue and extend its own lineage. */
    if (!action || (closed_actions.lookup(&parent->token) && parent_tgid == action->root_pid) || args->child_pid <= 0)
        return 0;
    struct pending_clone *clone = pending_clones.lookup(&parent_tid);
    u32 classification = clone ? clone->classification : 3;
    map_child_from_clone(args, (u32)args->child_pid, parent_tgid, parent_tid, parent->token, classification);
    return 0;
}

TRACEPOINT_PROBE(sched, sched_process_exit) {
    u64 id = bpf_get_current_pid_tgid();
    u32 tgid = id >> 32;
    u32 tid = (u32)id;
    struct pending_syscall *pending = pending_syscalls.lookup(&tid);
    if (pending) {
        struct action_aggregate *aggregate = aggregate_for(pending->token);
        struct action_state *action = active_actions.lookup(&pending->token);
        if (aggregate) {
            /* Preserve the individual pending operation at process death.
             * Status 3 carries a censor timestamp, never a fabricated return
             * value or completed duration (decoded as null in userspace). */
            emit_syscall_event_status(args, pending, 0, 3);
            __sync_fetch_and_add(&aggregate->censored_pending_records, 1);
        }
        if (action && action->in_flight > 0)
            __sync_fetch_and_add(&action->in_flight, (u64)-1);
        pending_syscalls.delete(&tid);
    }
    pending_clones.delete(&tid);
    if (tgid == tid)
        proc_actions.delete(&tgid);
    return 0;
}
"""


_TRACEPOINTS = (
    "raw_syscalls:sys_enter",
    "raw_syscalls:sys_exit",
    "sched:sched_process_fork",
    "sched:sched_process_exit",
)
_MANUAL_TRACEPOINTS = (("sched:sched_process_fork", "on_fork"),)


class BpfAttachError(LinuxWorkError):
    """Raised when privileged BCC collection cannot be started safely."""


class BpfProtocolError(LinuxWorkError):
    """Raised for invalid Unix-socket requests or responses."""


class _CActionState(ct.Structure):
    _fields_ = [
        ("root_pid", ct.c_uint32),
        ("closing", ct.c_uint32),
        ("in_flight", ct.c_uint64),
        ("started_ns", ct.c_uint64),
    ]


class _CProcAction(ct.Structure):
    _fields_ = [
        ("token", ct.c_uint64),
        ("birth_ns", ct.c_uint64),
        ("parent_tgid", ct.c_uint32),
    ]


class _CFdKey(ct.Structure):
    _fields_ = [("tgid", ct.c_uint32), ("fd", ct.c_int32)]


class _CFdValue(ct.Structure):
    _fields_ = [
        ("token", ct.c_uint64),
        ("path_status", ct.c_uint32),
        ("path_len", ct.c_uint32),
        ("path", ct.c_char * BPF_PATH_CAP),
    ]


class _CPathKey(ct.Structure):
    _fields_ = [("token", ct.c_uint64), ("sequence", ct.c_uint64)]


class _CPathRecord(ct.Structure):
    _fields_ = [
        ("kernel_ns", ct.c_uint64),
        ("tgid", ct.c_uint32),
        ("tid", ct.c_uint32),
        ("fd", ct.c_int32),
        ("kind", ct.c_uint32),
        ("status", ct.c_uint32),
        ("length", ct.c_uint32),
        ("path", ct.c_char * BPF_PATH_CAP),
    ]


class _CPendingSyscall(ct.Structure):
    _fields_ = [
        ("token", ct.c_uint64),
        ("started_ns", ct.c_uint64),
        ("syscall_nr", ct.c_uint32),
        ("kind", ct.c_uint32),
        ("fd", ct.c_int32),
        ("path_status", ct.c_uint32),
        ("path_len", ct.c_uint32),
        ("fd_path_backed", ct.c_uint32),
        ("parent_tgid", ct.c_uint32),
        ("path", ct.c_char * BPF_PATH_CAP),
        ("path2_status", ct.c_uint32),
        ("path2_len", ct.c_uint32),
        ("path2", ct.c_char * BPF_PATH_CAP),
        ("raw_args", ct.c_uint64 * 6),
    ]


class _CWorkEventLegacy(ct.Structure):
    _fields_ = [
        ("token", ct.c_uint64),
        ("sequence", ct.c_uint64),
        ("kernel_start_ns", ct.c_uint64),
        ("kernel_end_ns", ct.c_uint64),
        ("ret", ct.c_int64),
        ("syscall_nr", ct.c_uint32),
        ("tgid", ct.c_uint32),
        ("tid", ct.c_uint32),
        ("parent_tgid", ct.c_uint32),
        ("kind", ct.c_uint32),
        ("status", ct.c_uint32),
        ("path_status", ct.c_uint32),
        ("path_len", ct.c_uint32),
        ("child_pid", ct.c_uint32),
        ("fd", ct.c_int32),
        ("reserved", ct.c_uint32),
        ("path", ct.c_char * BPF_PATH_CAP),
        ("path2_status", ct.c_uint32),
        ("path2_len", ct.c_uint32),
        ("path2", ct.c_char * BPF_PATH_CAP),
    ]


class _CWorkEvent(ct.Structure):
    """Current v3 packet with six untouched raw syscall scalar arguments."""

    _fields_ = [
        ("token", ct.c_uint64),
        ("sequence", ct.c_uint64),
        ("kernel_start_ns", ct.c_uint64),
        ("kernel_end_ns", ct.c_uint64),
        ("ret", ct.c_int64),
        ("syscall_nr", ct.c_uint32),
        ("tgid", ct.c_uint32),
        ("tid", ct.c_uint32),
        ("parent_tgid", ct.c_uint32),
        ("kind", ct.c_uint32),
        ("status", ct.c_uint32),
        ("path_status", ct.c_uint32),
        ("path_len", ct.c_uint32),
        ("child_pid", ct.c_uint32),
        ("fd", ct.c_int32),
        ("reserved", ct.c_uint32),
        ("path", ct.c_char * BPF_PATH_CAP),
        ("path2_status", ct.c_uint32),
        ("path2_len", ct.c_uint32),
        ("path2", ct.c_char * BPF_PATH_CAP),
        ("raw_args", ct.c_uint64 * 6),
    ]


# Keep the two binary layouts named and exported.  A v2 journal is historical
# evidence and must continue to decode as v2; the v3 stream is the only layout
# emitted by this source after the raw-scalar ABI extension.
BPF_EVENT_RECORD_SIZE_LEGACY = ct.sizeof(_CWorkEventLegacy)
BPF_EVENT_RECORD_SIZE = ct.sizeof(_CWorkEvent)


class _CAggregate(ct.Structure):
    _fields_ = [
        ("tracked_syscall_count", ct.c_uint64),
        ("read_syscall_count", ct.c_uint64),
        ("write_syscall_count", ct.c_uint64),
        ("open_count", ct.c_uint64),
        ("stat_count", ct.c_uint64),
        ("getdents_count", ct.c_uint64),
        ("close_count", ct.c_uint64),
        ("exec_count", ct.c_uint64),
        ("fork_count", ct.c_uint64),
        ("thread_count", ct.c_uint64),
        ("unclassified_fork_count", ct.c_uint64),
        ("failed_syscall_count", ct.c_uint64),
        ("returned_read_bytes", ct.c_uint64),
        ("returned_write_bytes", ct.c_uint64),
        ("path_backed_read_bytes", ct.c_uint64),
        ("path_backed_write_bytes", ct.c_uint64),
        ("unknown_fd_read_bytes", ct.c_uint64),
        ("unknown_fd_write_bytes", ct.c_uint64),
        ("message_read_count", ct.c_uint64),
        ("message_write_count", ct.c_uint64),
        ("getdents_bytes", ct.c_uint64),
        ("syscall_latency_ns", ct.c_uint64),
        ("read_latency_ns", ct.c_uint64),
        ("write_latency_ns", ct.c_uint64),
        ("open_latency_ns", ct.c_uint64),
        ("stat_latency_ns", ct.c_uint64),
        ("getdents_latency_ns", ct.c_uint64),
        ("path_sequence", ct.c_uint64),
        ("event_sequence", ct.c_uint64),
        ("required_event_count", ct.c_uint64),
        ("lost_event_records", ct.c_uint64),
        ("lost_path_records", ct.c_uint64),
        ("lost_pending_records", ct.c_uint64),
        ("lineage_map_failures", ct.c_uint64),
        ("censored_pending_records", ct.c_uint64),
        ("mutation_count", ct.c_uint64),
        ("mmap_count", ct.c_uint64),
        ("msync_count", ct.c_uint64),
        ("mutation_latency_ns", ct.c_uint64),
        ("mmap_latency_ns", ct.c_uint64),
        ("msync_latency_ns", ct.c_uint64),
    ]


def _u64(value: Any) -> int:
    return int(value.value) if hasattr(value, "value") else int(value)


def _raw_path(value: Any, length: int) -> tuple[str | None, str | None]:
    data = bytes(value.path)[: max(0, min(int(length), BPF_PATH_CAP - 1))]
    if not data:
        return None, None
    return data.decode("utf-8", "replace"), data.hex()


def _raw_path_field(
    value: Any, field: str, length: int
) -> tuple[str | None, str | None]:
    data = bytes(getattr(value, field))[: max(0, min(int(length), BPF_PATH_CAP - 1))]
    if not data:
        return None, None
    return data.decode("utf-8", "replace"), data.hex()


def _mapping_values(value: Any, fields: Sequence[str]) -> dict[str, int]:
    return {field: int(getattr(value, field)) for field in fields}


_AGGREGATE_FIELDS = tuple(name for name, _ in _CAggregate._fields_)

_BPF_KIND_NAMES = {
    1: "read",
    2: "write",
    3: "open",
    4: "stat",
    5: "getdents",
    6: "close",
    7: "exec",
    8: "fork",
    9: "clone",
    10: "thread",
    11: "message_read",
    12: "message_write",
    13: "metadata_mutation",
    14: "rename",
    15: "mmap",
    16: "msync",
}
_BPF_EVENT_STATUS_NAMES = {1: "success", 2: "failure", 3: "censored_process_exit"}
_BPF_PATH_STATUS_NAMES = {0: "unknown", 1: "observed", 2: "truncated", 3: "unknown"}

# The raw tracepoint ABI exposes syscall numbers and six machine-word
# arguments.  The acquisition fleet is x86_64; keep the projection explicit
# so a future architecture cannot silently receive a misleading syscall name.
_BPF_X86_64_SYSCALL_NAMES = {
    0: "read", 1: "write", 2: "open", 3: "close", 9: "mmap", 17: "pread64",
    18: "pwrite64", 19: "readv", 20: "writev", 26: "msync", 32: "dup",
    33: "dup2", 44: "sendto", 45: "recvfrom", 46: "sendmsg", 47: "recvmsg",
    56: "clone", 57: "fork", 58: "vfork", 59: "execve", 72: "fcntl",
    76: "truncate", 77: "ftruncate", 78: "getdents", 80: "chdir",
    81: "fchdir", 82: "rename", 89: "readlink", 217: "getdents64",
    257: "openat", 264: "renameat", 267: "readlinkat", 292: "dup3",
    295: "preadv", 296: "pwritev", 299: "recvmmsg", 307: "sendmmsg",
    316: "renameat2", 322: "execveat", 327: "preadv2", 328: "pwritev2",
    437: "openat2",
}


def _signed_u64(value: int, bits: int) -> int:
    value = int(value) & ((1 << bits) - 1)
    sign = 1 << (bits - 1)
    return value - (1 << bits) if value & sign else value


def _scalar_args_projection(syscall_nr: int, raw_args: Sequence[int]) -> dict[str, Any]:
    """Give six raw words stable names without reading pointed-to memory.

    The raw values remain authoritative.  ``known`` is false for an unknown
    syscall number or an architecture outside the x86_64 acquisition ABI.
    Struct-valued arguments (notably ``openat2``) deliberately remain opaque.
    """

    values = [int(value) for value in raw_args]
    name = _BPF_X86_64_SYSCALL_NAMES.get(int(syscall_nr))
    projection: dict[str, Any] = {
        "abi": BPF_EVENT_ABI,
        "architecture": "x86_64",
        "known": name is not None,
        "syscall_name": name,
        "raw": values,
    }
    if name in {
        "read", "write", "pread64", "pwrite64", "readv", "writev",
        "recvfrom", "recvmsg", "recvmmsg", "sendto", "sendmsg", "sendmmsg",
        "getdents", "getdents64", "close", "ftruncate", "fchdir", "dup",
        "dup2", "dup3", "fcntl",
    }:
        projection["fd"] = _signed_u64(values[0], 32)
    if name in {"read", "write", "pread64", "pwrite64", "recvfrom", "sendto", "getdents", "getdents64"}:
        projection["requested_size"] = values[2]
    elif name in {"recvmmsg", "sendmmsg"}:
        projection["requested_message_count"] = values[2]
    elif name in {"readv", "writev", "preadv", "pwritev", "preadv2", "pwritev2"}:
        projection["iov_count"] = values[2]
    if name in {"pread64", "pwrite64", "preadv", "pwritev", "preadv2", "pwritev2"}:
        projection["offset"] = values[3]
    if name in {"truncate", "ftruncate"}:
        projection["length"] = values[1]
    if name == "mmap":
        projection.update(length=values[1], prot=values[2], flags=values[3], fd=_signed_u64(values[4], 32), offset=values[5])
    elif name == "msync":
        projection.update(length=values[1], flags=values[2])
    if name == "open":
        projection["open_flags"] = values[1]
    elif name == "openat":
        projection["dirfd"] = _signed_u64(values[0], 32)
        projection["open_flags"] = values[2]
    elif name == "openat2":
        projection.update(
            dirfd=_signed_u64(values[0], 32),
            open_how_pointer=values[2],
            open_how_size=values[3],
            open_flags=None,
            open_flags_source="open_how_struct_not_dereferenced",
        )
    if name in {"renameat", "renameat2"}:
        projection["old_dirfd"] = _signed_u64(values[0], 32)
        projection["new_dirfd"] = _signed_u64(values[2], 32)
        if name == "renameat2":
            projection["flags"] = values[4]
    if name in {"dup", "dup2", "dup3", "fcntl"}:
        projection["descriptor_transition"] = "new_fd_return_requires_lineage_update"
    elif name in {"chdir", "fchdir"}:
        projection["descriptor_transition"] = "cwd_transition_not_tracked_by_fd_path_map"
    return projection


def _scalar_args_unavailable(reason: str) -> dict[str, Any]:
    """Describe an intentional absence of the v3 scalar-argument payload."""

    return {
        "abi": BPF_EVENT_ABI,
        "architecture": "x86_64",
        "known": False,
        "syscall_name": None,
        "raw": None,
        "status": "unavailable",
        "reason": reason,
    }


def _bcc_version() -> str | None:
    if _BCCBPF is None:
        return None
    try:
        import bcc.version as version

        return str(getattr(version, "__version__", "unknown"))
    except Exception:
        return "unknown"


def _write_durable_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write a collector manifest/summary with file and directory durability."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (_canonical_json(value) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.is_symlink():
        raise BpfAttachError(f"collector temporary path must not be a symlink: {temporary}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        if descriptor >= 0:
            with _suppress_all():
                os.close(descriptor)
        with _suppress_all():
            temporary.unlink()
        raise


def _require_bcc() -> Any:
    if _BCCBPF is None:
        raise BpfAttachError(
            "BCC is unavailable; install python3-bpfcc/libbpfcc and run the collector privileged"
        )
    if os.name != "posix" or sys.platform != "linux":
        raise BpfAttachError("BCC work collector requires Linux")
    return _BCCBPF


def _token_for(session_id: str, event_id: str, command_sha256: str, ordinal: int) -> int:
    digest = hashlib.sha256(
        f"{session_id}\0{ordinal}\0{event_id}\0{command_sha256}".encode("utf-8")
    ).digest()
    token = int.from_bytes(digest[:8], "little")
    return token or 1


def _boundary_clock(explicit_anchor: bool) -> dict[str, str]:
    if explicit_anchor:
        return {
            "clock_id": "caller_supplied",
            "clock_source": "runtime action-boundary anchor",
        }
    fields = clock_fields()
    return {
        "clock_id": str(fields["clock_id"]),
        "clock_source": str(fields["clock_source"]),
    }


def _boundary_anchor(
    supplied_ns: int, *, explicit_anchor: bool
) -> dict[str, Any]:
    """Bracket a kernel MONOTONIC sample with the caller's boundary clock.

    The BPF event clock and runtime boundary clock remain native domains.  The
    bracket is retained only as bounded placement evidence; no cross-clock
    duration is computed from it.
    """

    raw_before = monotonic_ns()
    kernel_mono = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
    raw_after = monotonic_ns()
    return {
        "boundary_clock": clock_fields(),
        "boundary_anchor_ns": int(supplied_ns),
        "boundary_anchor_supplied": bool(explicit_anchor),
        "raw_before_ns": int(raw_before),
        "kernel_monotonic_sample_ns": int(kernel_mono),
        "raw_after_ns": int(raw_after),
        "raw_bracket_uncertainty_ns": max(0, int(raw_after - raw_before)),
        "kernel_clock": {
            "clock_id": "CLOCK_MONOTONIC",
            "clock_source": "time.clock_gettime_ns",
        },
        "cross_clock_subtraction": False,
    }


class BpfWorkCollector:
    """Kernel-map BCC collector attached to one explicit persistent PID."""

    def __init__(
        self,
        *,
        identity: ProcessIdentity,
        trace_dir: Path,
        bpf: Any,
        manifest_path: Path,
        boundary_journal: BoundaryJournal,
        session_id: str,
        startup_wall_ms: float,
        defer_event_derivation: bool = True,
    ) -> None:
        self.identity = identity
        self.trace_dir = Path(trace_dir)
        # Mount provenance is a once-per-bound-collector snapshot.  It is
        # deliberately outside action boundaries: mountinfo describes the
        # target/host filesystem context, not per-command work.  Even an
        # unavailable/failed read is retained as a hashed JSON witness so a
        # later report can distinguish absent evidence from a successful
        # capture instead of silently omitting the field.
        from .container_resources import capture_container_mounts

        self._container_mounts_path = self.trace_dir / "container_mounts.json"
        try:
            container_mounts = capture_container_mounts(self.identity)
        except Exception as exc:  # preserve collector startup on optional context
            container_mounts = {
                "schema_version": "assignment.container-mounts.v1",
                "status": "unavailable",
                "target_pid": self.identity.pid,
                "target_start_ticks": self.identity.start_ticks,
                "boot_id": self.identity.boot_id,
                "files": {},
                "unavailable_reason": f"{type(exc).__name__}: {exc}",
            }
        _write_durable_json(self._container_mounts_path, container_mounts)
        self._container_mounts = container_mounts
        self._container_mounts_artifact = {
            "path": str(self._container_mounts_path),
            "sha256": _sha256_path(self._container_mounts_path),
            "schema_version": container_mounts.get(
                "schema_version", "assignment.container-mounts.v1"
            ),
            "status": container_mounts.get("status", "unavailable"),
        }
        self.bpf = bpf
        self.manifest_path = Path(manifest_path)
        self.boundary_journal = boundary_journal
        self.session_id = session_id
        self.startup_wall_ms = startup_wall_ms
        self._defer_event_derivation = bool(defer_event_derivation)
        self._active: dict[str, int] = {}
        self._active_loss_baseline: dict[str, tuple[int, int]] = {}
        self._active_stream_baseline: dict[str, int] = {}
        self._deferred_tokens: dict[int, dict[str, Any]] = {}
        self._event_seen_by_token: dict[int, int] = {}
        self._completed: list[dict[str, Any]] = []
        self._finalizations: list[dict[str, Any]] = []
        self._ordinal = 0
        self._closed = False
        self._events_by_token: dict[int, list[bytes]] = {}
        self._event_condition = threading.Condition()
        self._event_lock = self._event_condition
        self._perf_lost_total = 0
        self._perf_callback_errors: list[str] = []
        self._perf_generation = 0
        self._raw_event_path = self.trace_dir / "raw_events.bin"
        self._raw_event_stream: io.BufferedWriter | None = None
        self._native_sink = None
        self._native_sink_descriptor = None
        self._perf_buffer_descriptor: dict[str, Any] | None = None
        self._native_error_count = 0
        self._raw_event_offset = 0
        self._raw_event_records = 0
        self._event_table = self._table("work_events")
        try:
            if self._defer_event_derivation:
                from .native_bpf_sink import NativeBpfSink
                self._native_sink = NativeBpfSink(self._raw_event_path)
                self._native_sink_descriptor = self._native_sink.descriptor
                self._native_sink.open_perf_buffers(
                    self._event_table,
                    page_count=BPF_PERF_BUFFER_PAGES_PER_CPU,
                )
                self._native_sink_descriptor = dict(self._native_sink.descriptor)
                self._perf_buffer_descriptor = dict(self._native_sink_descriptor)
            else:
                self._open_python_perf_buffer()
            self._perf_stop = threading.Event()
            self._perf_thread = threading.Thread(
                target=self._perf_poll_loop,
                name="agentic-bpf-perf-poller",
                daemon=True,
            )
            self._perf_thread.start()
        except Exception as exc:
            # A failed perf-buffer setup may have opened CPU readers before
            # raising.  Close those owned readers and the durable stream on
            # every constructor failure so no background FD survives attach.
            with _suppress_all():
                self._close_perf_buffers()
            raise BpfAttachError(f"cannot open required BPF perf buffer: {exc}") from exc

    def _open_python_perf_buffer(self) -> None:
        """Open the compatibility Python BCC callback path at v3 capacity."""

        self._raw_event_stream = self._raw_event_path.open("wb", buffering=1024 * 1024)
        try:
            self._event_table.open_perf_buffer(
                self._on_perf_event,
                page_cnt=BPF_PERF_BUFFER_PAGES_PER_CPU,
                lost_cb=self._on_perf_lost,
            )
        except BaseException:
            with _suppress_all():
                self._raw_event_stream.close()
            self._raw_event_stream = None
            raise

        # BCC keeps one reader per online CPU in this registry.  Older BCC
        # bindings do not expose the registry consistently, so retain the
        # exact page count even when CPU count is unavailable.
        perf_buffers = getattr(getattr(self._event_table, "bpf", None), "perf_buffers", None)
        if isinstance(perf_buffers, Mapping):
            cpu_count: int | None = len(perf_buffers)
        else:
            open_fds = getattr(self._event_table, "_open_key_fds", None)
            cpu_count = len(open_fds) if isinstance(open_fds, Mapping) else None
        self._perf_buffer_descriptor = perf_buffer_descriptor(
            implementation="python_bcc_callback",
            page_count=BPF_PERF_BUFFER_PAGES_PER_CPU,
            cpu_count=cpu_count,
            page_count_source="successful_bcc_open_perf_buffer",
        )

    @classmethod
    def attach(
        cls,
        target: ProcessTarget,
        trace_dir: Path,
        *,
        force: bool = False,
        defer_event_derivation: bool = True,
    ) -> "BpfWorkCollector":
        """Compile/load BCC and attach all tracepoints before any action."""

        BPF = _require_bcc()
        trace_dir = Path(trace_dir).expanduser()
        if trace_dir.is_symlink():
            raise BpfAttachError(f"trace directory must not be a symlink: {trace_dir}")
        trace_dir.mkdir(parents=True, exist_ok=True)
        if not trace_dir.is_dir():
            raise BpfAttachError(f"trace directory is not a directory: {trace_dir}")
        manifest_path = trace_dir / "bpf_collector_manifest.json"
        boundary_path = trace_dir / "action_boundaries.jsonl"
        raw_path = trace_dir / "raw_aggregates.jsonl"
        raw_event_path = trace_dir / "raw_events.bin"
        derived_paths = (
            trace_dir / "derived_event_records.jsonl",
            trace_dir / "derived_path_records.jsonl",
            trace_dir / "derived_action_index.jsonl",
        )
        if not force and any(
            path.exists()
            for path in (manifest_path, boundary_path, raw_path, raw_event_path, *derived_paths)
        ):
            raise BpfAttachError(f"refusing to reuse BPF trace directory without force: {trace_dir}")
        identity = ProcessIdentity.capture(target)
        started = time.perf_counter_ns()
        bpf = None
        try:
            bpf = BPF(text=BPF_PROGRAM)
            for tracepoint, function in _MANUAL_TRACEPOINTS:
                bpf.attach_tracepoint(tp=tracepoint, fn_name=function)
        except Exception as exc:
            if bpf is not None:
                cls._detach_bpf(bpf)
            raise BpfAttachError(f"BCC compile/attach failed: {exc}") from exc
        startup_ms = (time.perf_counter_ns() - started) / 1_000_000
        session_id = hashlib.sha256(
            f"{identity.binding_digest()}\0{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:32]
        try:
            boundary = BoundaryJournal(boundary_path, identity)
            manifest = {
                "schema_version": BPF_COLLECTOR_SCHEMA,
                "session_id": session_id,
                "status": "attached",
                "backend": "bcc",
                "bcc_version": _bcc_version(),
                "kernel_release": platform.release(),
                "program_sha256": hashlib.sha256(BPF_PROGRAM.encode("utf-8")).hexdigest(),
                "tracepoints": list(_TRACEPOINTS),
                "identity": identity.to_mapping(),
                "trace_dir": str(trace_dir),
                "boundary_journal": str(boundary_path),
                "raw_aggregate_journal": str(raw_path),
                "event_decode_api": "agentic_sim.telemetry.bpf_work.iter_bpf_events",
                "raw_event_stream": {
                    "path": str(raw_event_path),
                    "schema_version": BPF_EVENT_SCHEMA,
                    "record_size_bytes": BPF_EVENT_RECORD_SIZE,
                    "event_abi": BPF_EVENT_ABI,
                    "transport": "BPF_PERF_OUTPUT(work_events)",
                    "write_policy": "buffered binary writes; periodic flush; fsync at action/collector boundaries",
                },
                "startup_wall_ms": startup_ms,
                "kernel_clock": {
                    "clock_id": "CLOCK_MONOTONIC",
                    "clock_source": "bpf_ktime_get_ns",
                    "unit": "nanoseconds",
                    "boundary_clock_is_separate": True,
                },
                "map_contract": {
                    "active_action_capacity": 4096,
                    "process_lineage_capacity": 65536,
                    "pending_syscall_capacity": 65536,
                    "path_bytes": BPF_PATH_CAP,
                    "path_records": "derived from full event packets; no duplicate kernel path map",
                    "individual_event_transport": "BPF_PERF_OUTPUT(work_events)",
                    "individual_event_capture": "required; loss or count mismatch marks action unavailable",
                    "perf_buffer_pages_per_cpu": BPF_PERF_BUFFER_PAGES_PER_CPU,
                    "returned_io_semantics": "syscall-facing bytes; physical disk bytes are not measured",
                },
                "perf_buffer": {
                    "status": "pending",
                    "perf_pages_per_cpu_requested": BPF_PERF_BUFFER_PAGES_PER_CPU,
                },
            }
            _write_durable_json(manifest_path, manifest)
        except Exception:
            cls._detach_bpf(bpf)
            raise
        collector = None
        try:
            collector = cls(
                identity=identity,
                trace_dir=trace_dir,
                bpf=bpf,
                manifest_path=manifest_path,
                boundary_journal=boundary,
                session_id=session_id,
                startup_wall_ms=startup_ms,
                defer_event_derivation=defer_event_derivation,
            )
            manifest["native_sink"] = collector._native_sink_descriptor
            manifest["perf_buffer"] = dict(collector._perf_buffer_descriptor or {})
            manifest["container_mounts"] = collector._container_mounts_artifact
            manifest["artifact_reader_gid"] = trace_dir.stat().st_gid
            _write_durable_json(manifest_path, manifest)
            return collector
        except Exception:
            if collector is not None:
                with _suppress_all():
                    collector._close_perf_buffers()
            cls._detach_bpf(bpf)
            raise

    @staticmethod
    def _detach_bpf(bpf: Any) -> None:
        for tracepoint in _TRACEPOINTS:
            try:
                bpf.detach_tracepoint(tp=tracepoint)
            except Exception:
                pass
        with _suppress_all():
            bpf.cleanup()
        with _suppress_all():
            del bpf
        gc.collect()

    def _assert_open(self) -> None:
        if self._closed:
            raise BpfAttachError("BPF collector is closed")
        self.identity.assert_current()

    def _table(self, name: str) -> Any:
        if self.bpf is None:
            raise BpfAttachError("BPF collector is closed")
        return self.bpf[name]

    def _on_perf_lost(self, lost: int) -> None:
        """Record perf-buffer loss; the affected action will fail closed."""

        with self._event_condition:
            try:
                self._perf_lost_total += max(0, int(lost))
            except (TypeError, ValueError):
                self._perf_lost_total += 1
            self._event_condition.notify_all()

    def _sync_native_stats(self, token: int = 0) -> int:
        sink = getattr(self, "_native_sink", None)
        if sink is None:
            return self._event_seen_by_token.get(token, 0)
        stats = sink.stats(token)
        self._raw_event_offset = int(stats.offset_bytes)
        self._raw_event_records = int(stats.total_records)
        self._perf_lost_total = int(stats.lost)
        if stats.errors > self._native_error_count:
            self._perf_callback_errors.append(f"native_sink_errors={stats.errors}")
            self._native_error_count = int(stats.errors)
        return int(stats.token_records)

    def _on_perf_event(self, _cpu: int, data: Any, _size: int) -> None:
        """Copy one compact kernel record to the binary journal and RAM index."""

        try:
            record_size = BPF_EVENT_RECORD_SIZE
            # PERF_SAMPLE_RAW can include alignment bytes after the payload.
            # Preserve exactly the declared record, never the perf padding.
            if not record_size <= _size < record_size + 8:
                raise ValueError(f"unexpected BPF perf packet size {_size}")
            packet = ct.string_at(data, record_size)
            token = struct.unpack_from("=Q", packet)[0]
            with self._event_condition:
                stream = self._raw_event_stream
                if stream is None:
                    raise RuntimeError("raw event stream is closed")
                stream.write(packet)
                self._raw_event_offset += len(packet)
                self._raw_event_records += 1
                self._event_seen_by_token[token] = self._event_seen_by_token.get(token, 0) + 1
                # Keep the hot path binary and buffered.  A bounded flush
                # makes interruption evidence durable without fsync per event.
                if self._raw_event_records % 256 == 0:
                    stream.flush()
                # Closed actions retain their token for already-mapped
                # descendants, but their unbounded event history lives in the
                # durable binary stream rather than RAM.  Active actions keep
                # a bounded-by-action queue for the JSON witness.
                if not getattr(self, "_defer_event_derivation", False) and token not in self._deferred_tokens:
                    self._events_by_token.setdefault(token, []).append(packet)
                self._event_condition.notify_all()
        except Exception as exc:
            # Callback exceptions can terminate BCC's polling loop.  Retain a
            # loss-like diagnostic and let the completeness check fail closed.
            with self._event_condition:
                self._perf_callback_errors.append(type(exc).__name__)
                self._event_condition.notify_all()

    def _perf_poll_loop(self) -> None:
        """Continuously drain all perf CPUs while the target is running."""

        while not self._perf_stop.is_set():
            try:
                # Keep the reader responsive while actions are in flight.  A
                # poll generation is acknowledged after every call so an
                # action boundary can wait for a completed read rather than
                # guessing from a short idle sleep.
                self.bpf.perf_buffer_poll(timeout=5)
                if self._native_sink is not None:
                    self._native_sink.drain_perf_buffers(self._event_table)
            except (InterruptedError, KeyboardInterrupt):
                continue
            except Exception as exc:
                if not self._perf_stop.is_set():
                    with self._event_condition:
                        self._perf_callback_errors.append(type(exc).__name__)
                        self._event_condition.notify_all()
                time.sleep(0.001)
            finally:
                with self._event_condition:
                    self._perf_generation += 1
                    self._event_condition.notify_all()

    def _drain_perf_events(
        self,
        timeout_s: float,
        *,
        token: int | None = None,
        expected: int | None = None,
    ) -> None:
        """Wait for an acknowledged perf poll after the action boundary.

        ``perf_buffer_poll`` runs continuously in a dedicated thread.  The
        generation barrier prevents a boundary from racing a poll call that
        has not yet returned; when the kernel aggregate supplies an expected
        event count, wait for that count as well.  A timeout remains explicit
        and is handled by the action completeness gate.
        """

        if self.bpf is None:
            return
        deadline = time.monotonic() + max(0.001, timeout_s)
        with self._event_condition:
            initial_generation = self._perf_generation
        while True:
            with self._event_condition:
                if token is None:
                    self._sync_native_stats()
                    observed = self._raw_event_records
                else:
                    observed = self._sync_native_stats(token)
                generation_advanced = self._perf_generation > initial_generation
                # An explicit kernel count is already an acknowledgement of
                # every required packet. Waiting for another idle poll adds
                # latency without strengthening that count-based proof.
                if (expected is not None and observed >= expected) or (
                    expected is None and generation_advanced
                ):
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._event_condition.wait(timeout=min(0.010, remaining))

    @staticmethod
    def _event_row(
        packet: bytes, *, schema_version: str | None = None
    ) -> dict[str, Any]:
        """Decode one event while preserving the source layout identity.

        The v2 packet had no scalar-argument payload.  It is deliberately
        decoded with its old ctypes layout and reports that evidence as
        unavailable; it is never silently upgraded to v3.  A caller may pass
        ``schema_version`` when a zero-length/ambiguous range has already been
        identified from its manifest.  Otherwise the exact packet length is
        used as the compatibility discriminator.
        """

        if schema_version is None:
            if len(packet) == BPF_EVENT_RECORD_SIZE:
                schema_version = BPF_EVENT_SCHEMA
            elif len(packet) == BPF_EVENT_RECORD_SIZE_LEGACY:
                schema_version = BPF_EVENT_SCHEMA_LEGACY
            else:
                raise ValueError(
                    f"unsupported BPF event packet length {len(packet)} "
                    f"(expected {BPF_EVENT_RECORD_SIZE} or {BPF_EVENT_RECORD_SIZE_LEGACY})"
                )
        if schema_version == BPF_EVENT_SCHEMA:
            event_type = _CWorkEvent
            expected_size = BPF_EVENT_RECORD_SIZE
        elif schema_version == BPF_EVENT_SCHEMA_LEGACY:
            event_type = _CWorkEventLegacy
            expected_size = BPF_EVENT_RECORD_SIZE_LEGACY
        else:
            raise ValueError(f"unsupported BPF event schema: {schema_version}")
        if len(packet) != expected_size:
            raise ValueError(
                f"BPF event packet length {len(packet)} does not match "
                f"{schema_version} size {expected_size}"
            )
        event = event_type.from_buffer_copy(packet)
        start_ns = int(event.kernel_start_ns)
        end_ns = int(event.kernel_end_ns)
        if end_ns < start_ns:
            raise ValueError("BPF event end precedes start")
        path, path_hex = _raw_path(event, int(event.path_len))
        path2, path2_hex = _raw_path_field(event, "path2", int(event.path2_len))
        raw_scalar_args: list[int] | None
        scalar_args: dict[str, Any]
        event_abi: str | None
        if schema_version == BPF_EVENT_SCHEMA:
            raw_scalar_args = [int(value) for value in event.raw_args]
            scalar_args = _scalar_args_projection(
                int(event.syscall_nr), raw_scalar_args
            )
            event_abi = BPF_EVENT_ABI
        else:
            raw_scalar_args = None
            scalar_args = _scalar_args_unavailable(
                "historical_v2_event_has_no_scalar_argument_payload"
            )
            event_abi = None
        return {
            "schema_version": schema_version,
            "event_abi": event_abi,
            "token": int(event.token),
            "sequence": int(event.sequence),
            "kernel_start_ns": start_ns,
            "kernel_end_ns": None if int(event.status) == 3 else end_ns,
            "duration_ns": None if int(event.status) == 3 else end_ns - start_ns,
            "censor_boundary_ns": end_ns if int(event.status) == 3 else None,
            "syscall_nr": int(event.syscall_nr),
            "tgid": int(event.tgid),
            "tid": int(event.tid),
            "parent_tgid": int(event.parent_tgid),
            "kind": int(event.kind),
            "kind_name": _BPF_KIND_NAMES.get(int(event.kind), "unknown"),
            "status": int(event.status),
            "status_name": _BPF_EVENT_STATUS_NAMES.get(int(event.status), "unknown"),
            "ret": None if int(event.status) == 3 else int(event.ret),
            "fd": int(event.fd),
            "child_pid": int(event.child_pid),
            "path_status": int(event.path_status),
            "path_status_name": _BPF_PATH_STATUS_NAMES.get(int(event.path_status), "unknown"),
            "path_len": int(event.path_len),
            "path": path,
            "path_bytes_hex": path_hex,
            "path2_status": int(event.path2_status),
            "path2_status_name": _BPF_PATH_STATUS_NAMES.get(int(event.path2_status), "unknown"),
            "path2_len": int(event.path2_len),
            "path2": path2,
            "path2_bytes_hex": path2_hex,
            "raw_scalar_args": raw_scalar_args,
            "scalar_args": scalar_args,
        }

    def _events_for_token(self, token: int) -> list[dict[str, Any]]:
        with self._event_lock:
            packets = list(self._events_by_token.pop(token, []))
        rows: list[dict[str, Any]] = []
        for packet in packets:
            try:
                rows.append(self._event_row(packet))
            except (TypeError, ValueError):
                with self._event_lock:
                    self._perf_callback_errors.append("invalid_event_record")
        rows.sort(key=lambda row: (int(row["kernel_start_ns"]), int(row["sequence"])))
        return rows

    def _flush_raw_event_stream(self, *, fsync: bool) -> int:
        with self._event_lock:
            if self._native_sink is not None:
                boundary = self._native_sink.boundary(fsync=fsync)
                self._sync_native_stats()
                return int(boundary.offset_bytes)
            stream = self._raw_event_stream
            if stream is None:
                return self._raw_event_offset
            stream.flush()
            if fsync:
                os.fsync(stream.fileno())
            return self._raw_event_offset

    def _capture_event_boundary(
        self, token: int, *, fsync: bool = True
    ) -> tuple[int, int]:
        with self._event_lock:
            if self._native_sink is not None:
                boundary = self._native_sink.boundary(token, fsync=fsync)
                self._sync_native_stats(token)
                return int(boundary.offset_bytes), int(boundary.token_records)
            return self._flush_raw_event_stream(fsync=fsync), self._event_seen_by_token.get(token, 0)

    def _start_raw_event_sync(self) -> tuple[threading.Thread, list[BaseException]]:
        """Start the one bounded raw-stream sync used by an action boundary.

        The caller has already flushed and snapshotted the stream.  The sync
        itself may therefore overlap the independent boundary-journal fsync,
        while the callback/native-sink locks still serialize it with any
        writes that happen after the snapshot.
        """

        errors: list[BaseException] = []

        def sync() -> None:
            try:
                self._flush_raw_event_stream(fsync=True)
            except BaseException as exc:  # pragma: no cover - raised in worker
                errors.append(exc)

        thread = threading.Thread(
            target=sync,
            name="agentic-bpf-raw-event-sync",
            daemon=True,
        )
        thread.start()
        return thread, errors

    @staticmethod
    def _join_raw_event_sync(
        thread: threading.Thread, errors: Sequence[BaseException]
    ) -> None:
        """Join the bounded sync worker and surface a failed raw fsync."""

        thread.join()
        if errors:
            raise BpfAttachError("raw BPF event stream durability sync failed") from errors[0]

    def _discard_failed_action(self, event_id: str, token: int) -> None:
        """Release action state when its durable end cannot be committed."""

        with _suppress_all():
            self._delete_action_maps(token)
        self._active.pop(event_id, None)
        self._active_loss_baseline.pop(event_id, None)
        self._active_stream_baseline.pop(event_id, None)
        with self._event_lock:
            self._event_seen_by_token.pop(token, None)
            self._events_by_token.pop(token, None)

    def _stop_perf_poller(self) -> None:
        stop = getattr(self, "_perf_stop", None)
        thread = getattr(self, "_perf_thread", None)
        if stop is None or thread is None:
            return
        stop.set()
        thread.join(timeout=2.0)
        if thread.is_alive():
            raise BpfAttachError("perf reader did not stop; refusing to free live callback state")

    def _close_perf_buffers(self) -> None:
        """Close BCC perf readers before BPF maps are destroyed."""

        table = getattr(self, "_event_table", None)
        if table is None:
            return
        self._stop_perf_poller()
        for cpu in list(getattr(table, "_open_key_fds", {})):
            with _suppress_all():
                del table[cpu]
        if self._native_sink is not None:
            self._sync_native_stats()
            sink, self._native_sink = self._native_sink, None
            sink.close()
        with self._event_lock:
            stream = self._raw_event_stream
            self._raw_event_stream = None
            if stream is not None:
                with _suppress_all():
                    stream.flush()
                    os.fsync(stream.fileno())
                with _suppress_all():
                    stream.close()

    def _set_action(self, token: int, *, closing: int, started_ns: int = 0) -> _CActionState:
        value = _CActionState(
            root_pid=self.identity.pid,
            closing=closing,
            in_flight=0,
            started_ns=started_ns,
        )
        self._table("active_actions")[ct.c_ulonglong(token)] = value
        return value

    def _set_root_mapping(self, token: int) -> None:
        value = _CProcAction(token=token, birth_ns=0, parent_tgid=0)
        self._table("proc_actions")[ct.c_uint32(self.identity.pid)] = value

    def _set_zero_aggregate(self, token: int) -> None:
        self._table("aggregates")[ct.c_ulonglong(token)] = _CAggregate()

    def _container_resources(self) -> dict[str, Any]:
        from .container_resources import capture_container_resources

        return capture_container_resources(self.identity)

    def start_action(
        self,
        event_id: str,
        command: str,
        *,
        start_wall_ns: int | None = None,
        start_mono_ns: int | None = None,
    ) -> ActionBoundary:
        self._assert_open()
        if event_id in self._active:
            raise BpfAttachError(f"action is already active: {event_id}")
        # SWE-ReX's empty reset-command hook is a real physical shell call.
        # Preserve it as an action with the SHA-256 of the empty byte string;
        # None, non-text values, and embedded NULs remain invalid identities.
        if not isinstance(command, str) or "\x00" in command:
            raise BpfAttachError("action command must be text without NUL")
        self._ordinal += 1
        command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()
        token = _token_for(self.session_id, event_id, command_sha256, self._ordinal)
        start_wall = int(start_wall_ns or time.time_ns())
        start_mono_explicit = start_mono_ns is not None
        start_mono = int(start_mono_ns if start_mono_ns is not None else monotonic_ns())
        start_anchor = _boundary_anchor(
            start_mono, explicit_anchor=start_mono_explicit
        )
        kernel_started = time.monotonic_ns()
        # Establish the byte/loss baseline BEFORE publishing the root token:
        # the target can execute concurrently as soon as it is mapped.
        with self._event_lock:
            self._sync_native_stats()
            self._active_stream_baseline[event_id] = self._raw_event_offset
            self._active_loss_baseline[event_id] = (
                self._perf_lost_total, len(self._perf_callback_errors)
            )
        try:
            self._set_zero_aggregate(token)
            self._set_action(token, closing=0, started_ns=kernel_started)
            self._set_root_mapping(token)
            boundary = self.boundary_journal.start(
                event_id,
                command,
                start_wall_ns=start_wall,
                start_mono_ns=start_mono,
                snapshot={
                    "source": "bcc_kernel_boundary",
                    "container_resources": self._container_resources(),
                    "boundary_clock": _boundary_clock(start_mono_explicit),
                    "clock_anchor": start_anchor,
                    "start_anchor_ns": start_mono,
                    "kernel_clock": "CLOCK_MONOTONIC",
                    "kernel_start_anchor_not_subtracted": True,
                },
            )
        except Exception:
            self._delete_action_maps(token)
            self._active_stream_baseline.pop(event_id, None)
            self._active_loss_baseline.pop(event_id, None)
            raise
        self._active[event_id] = token
        return boundary

    def _read_action_state(self, token: int) -> _CActionState | None:
        try:
            value = self._table("active_actions")[ct.c_ulonglong(token)]
        except Exception:
            return None
        return _CActionState(
            root_pid=int(value.root_pid),
            closing=int(value.closing),
            in_flight=int(value.in_flight),
            started_ns=int(value.started_ns),
        )

    def _snapshot_aggregate(self, token: int) -> tuple[dict[str, int], bool]:
        try:
            value = self._table("aggregates")[ct.c_ulonglong(token)]
        except Exception:
            return {field: 0 for field in _AGGREGATE_FIELDS}, True
        return _mapping_values(value, _AGGREGATE_FIELDS), False

    def _snapshot_paths(self, token: int) -> list[dict[str, Any]]:
        # Path strings and status are already present in every full-fidelity
        # work-event packet.  The former path map duplicated those bytes and
        # required a full map scan/delete at every boundary.  Keep this method
        # as a compatibility hook for callers that expect a list; derivation
        # now happens once from ``raw_events.bin`` after capture.
        del token
        return []

    def _snapshot_pending(
        self, token: int, *, censor_boundary_ns: int
    ) -> list[dict[str, Any]]:
        """Copy pending syscall state as an explicit censor witness.

        A pending call at collector stop has a known kernel start and a
        userspace censor boundary, but no valid duration or return value.  It
        is therefore retained as ``censored`` rather than counted as a lost
        perf event or assigned an invented completion.
        """

        table = self._table("pending_syscalls")
        rows: list[dict[str, Any]] = []
        for key, value in list(table.items()):
            if int(value.token) != token:
                continue
            path, path_hex = _raw_path(value, int(value.path_len))
            path2, path2_hex = _raw_path_field(value, "path2", int(value.path2_len))
            raw_scalar_args = [int(item) for item in value.raw_args]
            rows.append(
                {
                    "schema_version": BPF_EVENT_SCHEMA,
                    "event_abi": BPF_EVENT_ABI,
                    "token": token,
                    "tid": _u64(key),
                    "tgid": None,
                    "parent_tgid": int(value.parent_tgid),
                    "syscall_nr": int(value.syscall_nr),
                    "kind": int(value.kind),
                    "kind_name": _BPF_KIND_NAMES.get(int(value.kind), "unknown"),
                    "fd": int(value.fd),
                    "kernel_start_ns": int(value.started_ns),
                    "censor_boundary_ns": int(censor_boundary_ns),
                    "status": "censored",
                    "return_value": None,
                    "duration_ns": None,
                    "path_status": int(value.path_status),
                    "path_status_name": _BPF_PATH_STATUS_NAMES.get(
                        int(value.path_status), "unknown"
                    ),
                    "path_len": int(value.path_len),
                    "path": path,
                    "path_bytes_hex": path_hex,
                    "path2_status": int(value.path2_status),
                    "path2_status_name": _BPF_PATH_STATUS_NAMES.get(
                        int(value.path2_status), "unknown"
                    ),
                    "path2_len": int(value.path2_len),
                    "path2": path2,
                    "path2_bytes_hex": path2_hex,
                    "raw_scalar_args": raw_scalar_args,
                    "scalar_args": _scalar_args_projection(
                        int(value.syscall_nr), raw_scalar_args
                    ),
                }
            )
        rows.sort(key=lambda row: (int(row["kernel_start_ns"]), int(row["tid"])))
        return rows

    def _count_token_processes(self, token: int) -> int:
        table = self._table("proc_actions")
        return sum(1 for _key, value in list(table.items()) if int(value.token) == token)

    def _count_token_descendants(self, token: int) -> int:
        table = self._table("proc_actions")
        return sum(
            1
            for key, value in list(table.items())
            if int(value.token) == token and _u64(key) != self.identity.pid
        )

    def _delete_action_maps(self, token: int) -> dict[str, int]:
        removed = {"active": 0, "aggregate": 0, "process": 0, "fd": 0, "pending": 0, "clone": 0}
        for name in ("proc_actions", "fd_paths"):
            table = self._table(name)
            keys: list[Any] = []
            for key, value in list(table.items()):
                if int(value.token) == token:
                    keys.append(key)
            for key in keys:
                with _suppress_all():
                    del table[key]
                    removed["process" if name == "proc_actions" else "fd"] += 1
        pending_table = self._table("pending_syscalls")
        pending_keys: list[Any] = []
        for key, value in list(pending_table.items()):
            if int(value.token) == token:
                pending_keys.append(key)
        for key in pending_keys:
            with _suppress_all():
                del pending_table[key]
                removed["pending"] += 1
        clone_table = self._table("pending_clones")
        clone_keys: list[Any] = []
        for key, value in list(clone_table.items()):
            if int(value.token) == token:
                clone_keys.append(key)
        for key in clone_keys:
            with _suppress_all():
                del clone_table[key]
                removed["clone"] += 1
        for name in ("active_actions", "aggregates"):
            table = self._table(name)
            key = ct.c_ulonglong(token)
            with _suppress_all():
                del table[key]
                removed["active" if name == "active_actions" else "aggregate"] += 1
        with _suppress_all():
            del self._table("closed_actions")[ct.c_ulonglong(token)]
        return removed

    def _write_raw_action(
        self,
        boundary: ActionBoundary,
        token: int,
        aggregate: Mapping[str, int],
        paths: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]],
        *,
        aggregate_missing: bool,
        flush_in_flight: int,
        perf_lost_events: int,
        required_event_count: int,
        event_records_complete: bool,
        callback_errors: Sequence[str],
        event_stream: Mapping[str, Any],
        map_cleanup: Mapping[str, int],
        deferred_quiescence: bool = False,
        censored_pending: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        stored_count = int(event_stream["record_count"])
        raw = {
            "schema_version": BPF_RAW_SCHEMA,
            "backend": "bcc",
            "program_sha256": hashlib.sha256(BPF_PROGRAM.encode("utf-8")).hexdigest(),
            "bcc_version": _bcc_version(),
            "kernel_release": platform.release(),
            "kernel_clock": {
                "clock_id": "CLOCK_MONOTONIC",
                "clock_source": "bpf_ktime_get_ns",
                "timestamps_are_not_boundary_duration_features": True,
            },
            "identity": self.identity.to_mapping(),
            "identity_binding_digest": self.identity.binding_digest(),
            "boundary": boundary.to_mapping(self.identity, phase="complete"),
            "action_token": int(token),
            "command_sha256": boundary.command_sha256,
            "raw_aggregate": dict(aggregate),
            "event_schema_version": BPF_EVENT_SCHEMA,
            "event_abi": BPF_EVENT_ABI,
            "event_record_size_bytes": BPF_EVENT_RECORD_SIZE,
            "events": [dict(row) for row in events],
            "event_storage": "binary" if self._defer_event_derivation else "inline",
            "event_count": stored_count,
            "event_count_at_boundary": (
                min(stored_count, int(required_event_count))
                if deferred_quiescence
                else stored_count
            ),
            "post_boundary_event_count": (
                max(0, stored_count - int(required_event_count))
                if deferred_quiescence
                else 0
            ),
            "required_event_count": int(required_event_count),
            "perf_lost_events": int(perf_lost_events),
            "event_records_complete": bool(event_records_complete),
            "event_callback_errors": list(callback_errors),
            "binary_event_stream": dict(event_stream),
            "path_records": [dict(row) for row in paths],
            "aggregate_missing": aggregate_missing,
            "in_flight_at_flush_timeout": flush_in_flight,
            "map_cleanup": dict(map_cleanup),
            "deferred_quiescence": bool(deferred_quiescence),
            "censored_pending": [dict(row) for row in censored_pending],
            "semantics": {
                "returned_read_write_bytes": "syscall-facing bytes from syscall return values",
                "path_backed_bytes": "descriptor was opened under this action and path observed; inode mode is not claimed",
                "unknown_fd_bytes": "descriptor provenance was unavailable; no regular-file or physical-disk claim",
                "message_counts": "recvmmsg/sendmmsg returns are message counts, not bytes",
                "physical_disk_bytes": "unavailable; this collector does not read /proc/io or infer storage traffic",
                "lineage": "only processes forked by an active mapped process inherit the action token; prior background descendants are not mapped",
                "scalar_arguments": "v3 raw syscall tracepoint words are retained without dereferencing pointed-to buffers; scalar_args is a named x86_64 projection, and v2 historical events report this quantity unavailable",
                "descriptor_transitions": "dup/dup2/dup3/fcntl and chdir/fchdir are not selected tracepoints in this bounded ABI; any resulting descriptor or working-directory provenance remains unknown rather than being silently attributed",
                "boundary_censoring": {
                    "state_marked_closing_before_snapshot": True,
                    "in_flight_at_flush_timeout": int(flush_in_flight),
                    "mapped_processes_removed": int(map_cleanup.get("process", 0)),
                    "mapped_processes_retained_for_continuation": int(
                        map_cleanup.get("retained_processes", 0)
                    ),
                    "post_boundary_work": "already-mapped descendants retain this action token until quiescence or collector stop; pending calls at collector stop are explicit censored witnesses with a start and censor boundary, and are never silently assigned to a later action",
                },
                "lost_events": "perf buffer loss, callback errors, lost_path_records, lost_pending_records, and lineage_map_failures are explicit; censored_pending_records are a separate bounded-stop witness and are not treated as transport loss; required event loss makes the action unavailable",
            },
        }
        _append_jsonl(self.trace_dir / "raw_aggregates.jsonl", raw)
        return raw

    def _finalize_deferred(self, flush_timeout_s: float) -> None:
        """Finalize action tokens retained for descendants until collector stop.

        The normal action boundary is not permission to discard a background
        process.  Deferred tokens remain in the kernel maps and their perf
        records continue into the durable stream.  At collector stop we take
        one acknowledged drain, copy pending calls as bounded censor witnesses,
        then remove only these owned maps.
        """

        for token, info in list(self._deferred_tokens.items()):
            aggregate_before, missing_before = self._snapshot_aggregate(token)
            expected_before = (
                int(aggregate_before.get("required_event_count", 0))
                if not missing_before
                else None
            )
            self._drain_perf_events(
                flush_timeout_s,
                token=token,
                expected=expected_before,
            )
            stream_end = self._flush_raw_event_stream(fsync=True)
            aggregate, aggregate_missing = self._snapshot_aggregate(token)
            censor_boundary_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
            state = self._read_action_state(token)
            in_flight_at_stop = int(state.in_flight) if state is not None else 0
            pending = self._snapshot_pending(
                token, censor_boundary_ns=censor_boundary_ns
            )
            pending_gap = max(0, in_flight_at_stop - len(pending))
            if pending_gap:
                pending.append(
                    {
                        "schema_version": BPF_EVENT_SCHEMA,
                        "event_abi": BPF_EVENT_ABI,
                        "token": token,
                        "tid": None,
                        "tgid": None,
                        "parent_tgid": None,
                        "syscall_nr": None,
                        "kind": None,
                        "kind_name": "unknown",
                        "fd": None,
                        "kernel_start_ns": None,
                        "censor_boundary_ns": censor_boundary_ns,
                        "status": "censored_unresolved_in_flight",
                        "return_value": None,
                        "duration_ns": None,
                        "path_status": 0,
                        "path_status_name": "unknown",
                        "path_len": 0,
                        "path": None,
                        "path_bytes_hex": None,
                        "path2_status": 0,
                        "path2_status_name": "unknown",
                        "path2_len": 0,
                        "path2": None,
                        "path2_bytes_hex": None,
                        "raw_scalar_args": None,
                        "scalar_args": _scalar_args_unavailable(
                            "pending_map_witness_missing"
                        ),
                        "unresolved_count": pending_gap,
                    }
                )
            paths = self._snapshot_paths(token)
            with self._event_lock:
                baseline_lost = int(info.get("baseline_lost", self._perf_lost_total))
                baseline_errors = int(
                    info.get("baseline_errors", len(self._perf_callback_errors))
                )
                perf_lost_events = max(0, self._perf_lost_total - baseline_lost)
                callback_errors = list(self._perf_callback_errors[baseline_errors:])
                seen_events = self._sync_native_stats(token)
            required_event_count = int(aggregate.get("required_event_count", 0))
            kernel_loss = {
                field: int(aggregate.get(field, 0))
                for field in (
                    "lost_event_records",
                    "lost_pending_records",
                    "lineage_map_failures",
                    "lost_path_records",
                )
            }
            complete = (
                not missing_before
                and not aggregate_missing
                and perf_lost_events == 0
                and not callback_errors
                and required_event_count == seen_events
                and all(value == 0 for value in kernel_loss.values())
                and pending_gap == 0
            )
            initial_boundary = dict(info["boundary"])
            final_status = str(initial_boundary.get("status", "unavailable"))
            final_error: str | None = None
            if not complete:
                if final_status == "success":
                    final_status = "unavailable"
                reasons: list[str] = []
                if perf_lost_events:
                    reasons.append(f"{perf_lost_events} perf event(s) lost")
                if callback_errors:
                    reasons.append(f"perf callback errors: {','.join(callback_errors)}")
                if required_event_count != seen_events:
                    reasons.append(
                        f"required event count {required_event_count} differs from decoded {seen_events}"
                    )
                for field, value in kernel_loss.items():
                    if value:
                        reasons.append(f"{field}={value}")
                if pending_gap:
                    reasons.append(
                        f"{pending_gap} in-flight syscall(s) lacked a pending-map witness"
                    )
                if missing_before or aggregate_missing:
                    reasons.append("aggregate map unavailable")
                final_error = "deferred BPF event records incomplete: " + "; ".join(reasons)
            final_stream = {
                "path": str(self._raw_event_path),
                "schema_version": BPF_EVENT_SCHEMA,
                "record_size_bytes": BPF_EVENT_RECORD_SIZE,
                "event_abi": BPF_EVENT_ABI,
                "offset_start": int(info["stream_start"]),
                "offset_end": int(stream_end),
                "byte_length": max(0, int(stream_end - info["stream_start"])),
                "record_count": seen_events,
                "durable_at_boundary": True,
                "finalization": "collector_stop",
            }
            final = {
                "schema_version": BPF_RAW_SCHEMA,
                "record_type": "action_finalization",
                "backend": "bcc",
                "program_sha256": hashlib.sha256(BPF_PROGRAM.encode("utf-8")).hexdigest(),
                "event_schema_version": BPF_EVENT_SCHEMA,
                "identity": self.identity.to_mapping(),
                "identity_binding_digest": self.identity.binding_digest(),
                "boundary": {
                    **initial_boundary,
                    "status": final_status,
                    "error": final_error or initial_boundary.get("error"),
                },
                "action_token": int(token),
                "command_sha256": str(info["command_sha256"]),
                "raw_aggregate": dict(aggregate),
                "event_abi": BPF_EVENT_ABI,
                "event_record_size_bytes": BPF_EVENT_RECORD_SIZE,
                "events": [],
                "event_storage": "binary",
                "event_count": seen_events,
                "required_event_count": required_event_count,
                "perf_lost_events": perf_lost_events,
                "event_records_complete": complete,
                "event_callback_errors": callback_errors,
                "binary_event_stream": final_stream,
                "path_records": paths,
                "aggregate_missing": bool(missing_before or aggregate_missing),
                "in_flight_at_flush_timeout": int(
                    info.get("in_flight_at_boundary", 0)
                ),
                "censor_boundary": {
                    "clock_id": "CLOCK_MONOTONIC",
                    "clock_source": "time.clock_gettime_ns",
                    "censor_boundary_ns": censor_boundary_ns,
                    "pending_count": len(pending),
                    "in_flight_at_stop": in_flight_at_stop,
                    "pending_witness_gap": pending_gap,
                },
                "censored_pending": pending,
                "deferred_quiescence": True,
                "map_cleanup": {},
                "semantics": {
                    "continuation": "already-mapped descendants remained attributed to the original action token through collector stop",
                    "censored_pending": "pending records have a known kernel start and collector-stop censor boundary but no invented end, duration, or return value",
                    "censored_pending_records": int(
                        aggregate.get("censored_pending_records", 0)
                    ),
                    "scalar_arguments": "v3 raw syscall tracepoint words are retained without dereferencing pointed-to buffers; scalar_args is a named x86_64 projection",
                    "pending_witness_gap": pending_gap,
                    "lost_events": "transport/map loss remains distinct from bounded collector-stop censorship",
                },
            }
            cleanup = self._delete_action_maps(token)
            final["map_cleanup"] = cleanup
            _append_jsonl(self.trace_dir / "raw_aggregates.jsonl", final)
            self._finalizations.append(final)
            self._deferred_tokens.pop(token, None)
            self._event_seen_by_token.pop(token, None)
            self._events_by_token.pop(token, None)

    def end_action(
        self,
        event_id: str,
        *,
        status: str,
        end_wall_ns: int | None = None,
        end_mono_ns: int | None = None,
        timeout: bool = False,
        error: str | None = None,
        flush_timeout_s: float = BPF_FLUSH_TIMEOUT_S,
    ) -> ActionBoundary:
        self._assert_open()
        token = self._active.get(event_id)
        if token is None:
            raise BpfAttachError(f"action has no active start: {event_id}")
        if flush_timeout_s <= 0:
            raise BpfAttachError("flush_timeout_s must be positive")
        end_wall = int(end_wall_ns or time.time_ns())
        end_mono_explicit = end_mono_ns is not None
        end_mono = int(end_mono_ns if end_mono_ns is not None else monotonic_ns())
        end_anchor = _boundary_anchor(end_mono, explicit_anchor=end_mono_explicit)
        state = self._read_action_state(token)
        if state is None:
            flush_in_flight = -1
            aggregate_missing = True
        else:
            # Do not rewrite action_state from userspace: its in_flight
            # counter can change concurrently in the kernel.
            self._table("closed_actions")[ct.c_ulonglong(token)] = ct.c_uint32(1)
            # A persistent shell normally has a pending stdin read here.
            # Waiting cannot close it; retain its token and finalize explicitly
            # at collection stop instead of stalling every tool boundary.
            flush_in_flight = int(state.in_flight) if state is not None else -1
            aggregate_missing = False
        # Perf output is delivered asynchronously.  Read the aggregate before
        # waiting so the kernel's required-event counter becomes the barrier
        # target.  The poll-generation acknowledgement below prevents a
        # boundary from racing a reader that has not yet returned from
        # perf_buffer_poll; a short idle sleep is insufficient for that job.
        aggregate_before_drain, aggregate_before_missing = self._snapshot_aggregate(token)
        expected_before_drain = (
            int(aggregate_before_drain.get("required_event_count", 0))
            if not aggregate_before_missing
            else None
        )
        self._drain_perf_events(
            flush_timeout_s,
            token=token,
            expected=expected_before_drain,
        )
        aggregate, map_missing = self._snapshot_aggregate(token)
        aggregate_missing = aggregate_missing or aggregate_before_missing or map_missing
        # Bind count and durable byte boundary under the callback lock. A
        # callback arriving later belongs to the continuation, not this range.
        # The initial flush establishes the exact snapshot; its fsync is
        # started below so it can overlap the independent journal fsync.
        try:
            with self._event_lock:
                stream_end, stored_count = self._capture_event_boundary(token, fsync=False)
                stream_start = self._active_stream_baseline.get(event_id, stream_end)
        except BaseException:
            self._discard_failed_action(event_id, token)
            raise
        baseline_lost, baseline_errors = self._active_loss_baseline.get(
            event_id, (self._perf_lost_total, len(self._perf_callback_errors))
        )
        with self._event_lock:
            perf_lost_events = max(0, self._perf_lost_total - baseline_lost)
            callback_errors = list(self._perf_callback_errors[baseline_errors:])
        required_event_count = int(aggregate.get("required_event_count", 0))
        kernel_loss = {
            field: int(aggregate.get(field, 0))
            for field in (
                "lost_event_records",
                "lost_pending_records",
                "lineage_map_failures",
                "lost_path_records",
            )
        }
        event_records_complete = (
            not aggregate_missing
            and perf_lost_events == 0
            and not callback_errors
            and required_event_count <= stored_count
            and all(value == 0 for value in kernel_loss.values())
        )
        if not event_records_complete:
            status = "unavailable" if status == "success" else status
            reasons: list[str] = []
            if perf_lost_events:
                reasons.append(f"{perf_lost_events} perf event(s) lost")
            if callback_errors:
                reasons.append(f"perf callback errors: {','.join(callback_errors)}")
            if stored_count < required_event_count:
                reasons.append(
                    f"required event count {required_event_count} exceeds captured {stored_count}"
                )
            for field, value in kernel_loss.items():
                if value:
                    reasons.append(f"{field}={value}")
            if aggregate_missing:
                reasons.append("aggregate map unavailable")
            error = error or "required BPF event records incomplete: " + "; ".join(reasons)
        try:
            raw_sync_thread, raw_sync_errors = self._start_raw_event_sync()
        except BaseException:
            self._discard_failed_action(event_id, token)
            raise
        boundary_error: BaseException | None = None
        boundary: ActionBoundary | None = None
        try:
            boundary = self.boundary_journal.end(
                event_id,
                status=status,
                end_wall_ns=end_wall,
                end_mono_ns=end_mono,
                timeout=timeout,
                error=error,
                snapshot={
                    "source": "bcc_kernel_boundary",
                    "container_resources": self._container_resources(),
                    "boundary_clock": _boundary_clock(end_mono_explicit),
                    "clock_anchor": end_anchor,
                    "end_anchor_ns": end_mono,
                    "kernel_clock": "CLOCK_MONOTONIC",
                    "kernel_end_anchor_not_subtracted": True,
                    "in_flight_at_flush": flush_in_flight,
                },
            )
        except BaseException as exc:
            boundary_error = exc
        finally:
            try:
                self._join_raw_event_sync(raw_sync_thread, raw_sync_errors)
            except BaseException as exc:
                if boundary_error is None:
                    boundary_error = exc
                else:
                    with _suppress_all():
                        boundary_error.add_note(
                            f"raw event stream sync also failed: {exc}"
                        )
        if boundary_error is not None:
            self._discard_failed_action(event_id, token)
            raise boundary_error
        assert boundary is not None
        paths = self._snapshot_paths(token)
        events = [] if self._defer_event_derivation else self._events_for_token(token)
        descendant_processes = self._count_token_descendants(token)
        deferred_quiescence = flush_in_flight > 0 or descendant_processes > 0
        if deferred_quiescence:
            # Keep the aggregate and already-mapped descendants alive.  The
            # root mapping is replaced by the next action start; a pending
            # root read still finishes against this token.
            self._deferred_tokens[token] = {
                "event_id": event_id,
                "boundary": boundary.to_mapping(self.identity, phase="complete"),
                "command_sha256": boundary.command_sha256,
                "stream_start": int(stream_start),
                "baseline_lost": int(baseline_lost),
                "baseline_errors": int(baseline_errors),
                "in_flight_at_boundary": int(flush_in_flight),
            }
        if deferred_quiescence:
            cleanup = {
                "active": 0,
                "aggregate": 0,
                "process": 0,
                "fd": 0,
                "pending": 0,
                "clone": 0,
                "retained_processes": int(descendant_processes),
                "retained_token": 1,
            }
        else:
            cleanup = self._delete_action_maps(token)
        self._active.pop(event_id, None)
        self._active_loss_baseline.pop(event_id, None)
        self._active_stream_baseline.pop(event_id, None)
        if not deferred_quiescence:
            self._event_seen_by_token.pop(token, None)
        event_stream = {
            "path": str(self._raw_event_path),
            "schema_version": BPF_EVENT_SCHEMA,
            "record_size_bytes": BPF_EVENT_RECORD_SIZE,
            "event_abi": BPF_EVENT_ABI,
            "offset_start": int(stream_start),
            "offset_end": int(stream_end),
            "byte_length": max(0, int(stream_end - stream_start)),
            "record_count": stored_count,
            "durable_at_boundary": True,
        }
        raw = self._write_raw_action(
            boundary,
            token,
            aggregate,
            paths,
            events,
            aggregate_missing=aggregate_missing,
            flush_in_flight=flush_in_flight,
            perf_lost_events=perf_lost_events,
            required_event_count=required_event_count,
            event_records_complete=event_records_complete,
            callback_errors=callback_errors,
            event_stream=event_stream,
            map_cleanup=cleanup,
            deferred_quiescence=deferred_quiescence,
        )
        self._completed.append(raw)
        return boundary

    def close(self, *, flush_timeout_s: float = BPF_FLUSH_TIMEOUT_S) -> dict[str, Any]:
        if self._closed:
            return self.summary()
        for event_id in list(self._active):
            with _suppress_all():
                self.end_action(
                    event_id,
                    status="incomplete",
                    error="collector closed before action terminal callback",
                    flush_timeout_s=flush_timeout_s,
                )
        # Actions whose shell/descendant work outlived their ordinary
        # boundary remain mapped until this explicit collector-stop witness.
        # Freeze probe producers before snapshotting final counts/pending
        # operations. Otherwise a background child can race the durable range
        # and map cleanup. Keep maps and perf readers alive until drained.
        freeze_started = time.monotonic_ns()
        for tracepoint in _TRACEPOINTS:
            self.bpf.detach_tracepoint(tp=tracepoint)
        self._capture_stop = {
            "clock_id": "CLOCK_MONOTONIC",
            "detach_started_ns": freeze_started,
            "detach_completed_ns": time.monotonic_ns(),
        }
        self._finalize_deferred(flush_timeout_s)
        self._close_perf_buffers()
        raw_event_sha256 = (
            _sha256_path(self._raw_event_path)
            if self._raw_event_path.is_file()
            else None
        )
        self._detach_bpf(self.bpf)
        self.bpf = None
        self._closed = True
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest.update(
                {
                    "status": "closed",
                    "closed_wall_ns": time.time_ns(),
                    "completed_action_count": len(self._completed),
                    "finalized_action_count": len(self._finalizations),
                    "raw_event_stream_sha256": raw_event_sha256,
                }
            )
            _write_durable_json(self.manifest_path, manifest)
        except (OSError, json.JSONDecodeError):
            pass
        summary = self.summary()
        summary["raw_event_stream"]["sha256"] = raw_event_sha256
        _write_durable_json(self.trace_dir / "work_summary.json", summary)
        return summary

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": BPF_SUMMARY_SCHEMA,
            "collector_schema": BPF_COLLECTOR_SCHEMA,
            "backend": "bcc",
            "identity": self.identity.to_mapping(),
            "identity_binding_digest": self.identity.binding_digest(),
            "program_sha256": hashlib.sha256(BPF_PROGRAM.encode("utf-8")).hexdigest(),
            "startup_wall_ms": self.startup_wall_ms,
            "kernel_clock": "CLOCK_MONOTONIC via bpf_ktime_get_ns; not subtracted from boundary CLOCK_MONOTONIC_RAW",
            "actions": [
                {
                    "event_id": row["boundary"]["event_id"],
                    "action_token": row["action_token"],
                    "command_sha256": row["command_sha256"],
                    "status": row["boundary"]["status"],
                    "duration_ms": (
                        row["boundary"].get("end_wall_ns", 0)
                        - row["boundary"].get("start_wall_ns", 0)
                    )
                    / 1_000_000,
                    "raw": row,
                }
                for row in self._completed
            ],
            "action_finalizations": [dict(row) for row in self._finalizations],
            "capture_stop": getattr(self, "_capture_stop", None),
            "native_sink": self._native_sink_descriptor,
            "perf_buffer": dict(getattr(self, "_perf_buffer_descriptor", None) or {}),
            "container_mounts": dict(getattr(self, "_container_mounts_artifact", {})),
            "raw_aggregate_journal": str(self.trace_dir / "raw_aggregates.jsonl"),
            "raw_event_stream": {
                "path": str(self.trace_dir / "raw_events.bin"),
                "schema_version": BPF_EVENT_SCHEMA,
                "record_size_bytes": BPF_EVENT_RECORD_SIZE,
                "event_abi": BPF_EVENT_ABI,
                "records_written": self._raw_event_records,
                "bytes_written": self._raw_event_offset,
                "durability": "periodic buffered flush and fsync at action/collector boundaries",
            },
            "limitations": [
                "BCC raw syscall tracepoints emit one perf-buffer record for each selected syscall and lineage event; unsupported syscalls are not represented as measured work.",
                "Path records are bounded and report map loss, unknown, or truncated status explicitly.",
                "Read/write byte fields are syscall-facing return values and are not physical storage traffic.",
                "A child exiting before userspace cleanup can leave an incomplete lineage map; the action token is never reused.",
                "Kernel CLOCK_MONOTONIC latency timestamps are separate from caller CLOCK_MONOTONIC_RAW action anchors.",
                "Any perf-buffer loss, callback error, pending-record loss, or event-count mismatch marks the action unavailable.",
                "The binary event stream is the interruption-tolerant individual evidence source; compact action rows reference token-filtered byte ranges and detailed JSON can be derived after capture.",
                "When an already-mapped descendant outlives an action boundary, its events remain on the original token until quiescence or collector stop; finalization rows carry any bounded censored-pending witnesses.",
                "The bounded syscall selection does not trace dup-family/fcntl descriptor duplication or chdir/fchdir working-directory transitions; subsequent unknown descriptors/cwd-dependent paths are disclosed rather than reconstructed by guesswork.",
            ],
        }

    def raw_trace_files(self) -> list[Path]:
        return [
            path
            for path in (
                self.trace_dir / "raw_events.bin",
                self.trace_dir / "raw_aggregates.jsonl",
            )
            if path.is_file()
        ]


def iter_bpf_events(
    path: Path,
    *,
    offset_start: int = 0,
    offset_end: int | None = None,
    token: int | None = None,
    schema_version: str | None = None,
    record_size_bytes: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Decode retained individual records without loading a trajectory in RAM.

    Ranges can contain other tokens when background actions overlap. Filter
    only after validating packet structure; never interpret range byte count
    as a per-action event count. Integrity hashes are verified by the caller.
    ``schema_version``/``record_size_bytes`` should be copied from the
    collector manifest or action row whenever available.  For standalone
    historical binaries, an unambiguous file/range length is accepted; a
    length divisible by both layouts is rejected rather than guessed.
    """

    if schema_version is not None and schema_version not in {
        BPF_EVENT_SCHEMA,
        BPF_EVENT_SCHEMA_LEGACY,
    }:
        raise ValueError(f"unsupported BPF event schema: {schema_version}")
    if record_size_bytes is not None:
        if isinstance(record_size_bytes, bool) or not isinstance(record_size_bytes, int):
            raise ValueError("record_size_bytes must be an integer")
        if record_size_bytes not in {
            BPF_EVENT_RECORD_SIZE,
            BPF_EVENT_RECORD_SIZE_LEGACY,
        }:
            raise ValueError(f"unsupported BPF event record size: {record_size_bytes}")
    if schema_version is not None:
        schema_size = (
            BPF_EVENT_RECORD_SIZE
            if schema_version == BPF_EVENT_SCHEMA
            else BPF_EVENT_RECORD_SIZE_LEGACY
        )
        if record_size_bytes is not None and record_size_bytes != schema_size:
            raise ValueError("BPF event schema and record size disagree")
        record_size_bytes = schema_size

    with Path(path).open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        end = size if offset_end is None else offset_end
        if (
            isinstance(offset_start, bool) or not isinstance(offset_start, int)
            or isinstance(end, bool) or not isinstance(end, int)
            or not 0 <= offset_start <= end <= size
        ):
            raise ValueError("invalid BPF binary event range")
        if record_size_bytes is None:
            if end == offset_start == 0:
                # There is no packet with which to infer a historical ABI;
                # empty streams use the current manifest default.  A caller
                # with a historical empty stream can still pass v2 explicitly.
                record_size_bytes = BPF_EVENT_RECORD_SIZE
            candidates = [
                candidate
                for candidate in (
                    BPF_EVENT_RECORD_SIZE,
                    BPF_EVENT_RECORD_SIZE_LEGACY,
                )
                if offset_start % candidate == 0
                and end % candidate == 0
                and (end - offset_start) % candidate == 0
            ]
            if record_size_bytes is not None:
                candidates = [record_size_bytes]
            if not candidates:
                raise ValueError("invalid or unaligned BPF binary event range")
            if len(candidates) > 1:
                raise ValueError(
                    "ambiguous BPF binary event layout; provide schema_version "
                    "or record_size_bytes from the manifest"
                )
            record_size_bytes = candidates[0]
        record_size = int(record_size_bytes)
        if offset_start % record_size or end % record_size:
            raise ValueError("invalid or unaligned BPF binary event range")
        decoded_schema = schema_version or (
            BPF_EVENT_SCHEMA
            if record_size == BPF_EVENT_RECORD_SIZE
            else BPF_EVENT_SCHEMA_LEGACY
        )
        stream.seek(offset_start)
        remaining = end - offset_start
        while remaining:
            packet = stream.read(record_size)
            if len(packet) != record_size:
                raise ValueError("truncated BPF binary event record")
            row = BpfWorkCollector._event_row(packet, schema_version=decoded_schema)
            if row["kind"] not in _BPF_KIND_NAMES or row["status"] not in {1, 2, 3}:
                raise ValueError("invalid BPF binary event kind or status")
            for prefix in ("path", "path2"):
                if row[prefix + "_status"] not in {0, 1, 2, 3} or not (
                    0 <= row[prefix + "_len"] <= BPF_PATH_CAP
                ):
                    raise ValueError("invalid BPF binary path descriptor")
            if token is None or row["token"] == token:
                yield row
            remaining -= record_size


class _suppress_all:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: Any) -> bool:
        return True


def _validate_socket_path(path: Path) -> Path:
    path = Path(path).expanduser()
    if "\x00" in str(path) or len(str(path).encode("utf-8")) >= 108:
        raise BpfProtocolError("Unix socket path is invalid or exceeds AF_UNIX length")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise BpfProtocolError(f"Unix socket path must not be a symlink: {path}")
    return path


def _assert_request_binding(request: Mapping[str, Any], identity: ProcessIdentity) -> None:
    supplied = request.get("identity")
    if supplied is not None:
        if not isinstance(supplied, Mapping):
            raise IdentityBindingError("socket identity must be a mapping")
        for field in (
            "pid",
            "start_ticks",
            "boot_id",
            "pid_namespace_inode",
            "run_id",
            "attempt_id",
            "case_id",
        ):
            if field in supplied and supplied[field] != identity.to_mapping()[field]:
                raise IdentityBindingError(f"socket identity field {field} does not match collector")
    for field in ("run_id", "attempt_id", "case_id"):
        if field in request and request[field] != identity.to_mapping()[field]:
            raise IdentityBindingError(f"socket request field {field} does not match collector")


class BpfWorkService:
    """Small root-owned Unix-socket server around :class:`BpfWorkCollector`."""

    def __init__(self, collector: BpfWorkCollector, socket_path: Path, *, mode: int = 0o660, client_gid: int | None = None):
        self.collector = collector
        self.socket_path = _validate_socket_path(Path(socket_path))
        self.mode = mode
        self.client_gid = client_gid
        self._server: socket.socket | None = None

    def _bind(self) -> socket.socket:
        if self.socket_path.exists() or self.socket_path.is_symlink():
            info = self.socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode):
                raise BpfProtocolError(f"refusing to replace non-socket path: {self.socket_path}")
            self.socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, self.mode)
            if os.geteuid() == 0:
                os.chown(self.socket_path, 0, self.client_gid if self.client_gid is not None else 0)
            server.listen(8)
        except Exception:
            server.close()
            with _suppress_all():
                self.socket_path.unlink()
            raise
        self._server = server
        return server

    @staticmethod
    def _read_request(connection: socket.socket) -> dict[str, Any]:
        chunks: list[bytes] = []
        size = 0
        while size <= 2 * 1024 * 1024:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if b"\n" in chunk:
                break
        line = b"".join(chunks).split(b"\n", 1)[0]
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BpfProtocolError("socket request is not one JSON object") from exc
        if not isinstance(value, Mapping):
            raise BpfProtocolError("socket request must be a JSON object")
        return dict(value)

    def _dispatch(self, request: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        _assert_request_binding(request, self.collector.identity)
        operation = request.get("op")
        if operation == "cwd_snapshot":
            if set(request) != {"op", "identity"}:
                raise BpfProtocolError("cwd snapshot accepts only the already bound service identity")
            if request["identity"] != self.collector.identity.to_mapping():
                raise IdentityBindingError("cwd snapshot requires the complete service identity")
            return {
                "schema_version": BPF_SOCKET_SCHEMA,
                "ok": True,
                "cwd_snapshot": _capture_persistent_shell_cwd(self.collector.identity),
            }, False
        if operation == "ping":
            return {
                "schema_version": BPF_SOCKET_SCHEMA,
                "ok": True,
                "identity": self.collector.identity.to_mapping(),
                "startup_wall_ms": self.collector.startup_wall_ms,
            }, False
        if operation == "start_action":
            event_id = request.get("event_id")
            command = request.get("command")
            if not isinstance(event_id, str) or not isinstance(command, str):
                raise BpfProtocolError("start_action requires event_id and command text")
            supplied_hash = request.get("command_sha256")
            actual_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()
            if supplied_hash is not None and supplied_hash != actual_hash:
                raise IdentityBindingError("socket command hash does not match command")
            boundary = self.collector.start_action(
                event_id,
                command,
                start_wall_ns=request.get("start_wall_ns"),
                start_mono_ns=request.get("start_mono_ns"),
            )
            return {
                "schema_version": BPF_SOCKET_SCHEMA,
                "ok": True,
                "boundary": boundary.to_mapping(self.collector.identity, phase="start"),
            }, False
        if operation == "end_action":
            event_id = request.get("event_id")
            status = request.get("status")
            if not isinstance(event_id, str) or not isinstance(status, str):
                raise BpfProtocolError("end_action requires event_id and status text")
            boundary = self.collector.end_action(
                event_id,
                status=status,
                end_wall_ns=request.get("end_wall_ns"),
                end_mono_ns=request.get("end_mono_ns"),
                timeout=bool(request.get("timeout", False)),
                error=request.get("error"),
            )
            completed = getattr(self.collector, "_completed", None)
            raw = completed[-1] if isinstance(completed, list) and completed else None
            raw_descriptor = None
            if raw is not None:
                raw_descriptor = {
                    "schema_version": raw.get("schema_version"),
                    "action_token": raw.get("action_token"),
                    "event_count": raw.get("event_count"),
                    "required_event_count": raw.get("required_event_count"),
                    "event_records_complete": raw.get("event_records_complete"),
                    "perf_lost_events": raw.get("perf_lost_events"),
                    "deferred_quiescence": raw.get("deferred_quiescence"),
                    "censored_pending_count": len(raw.get("censored_pending", [])),
                    "binary_event_stream": raw.get("binary_event_stream"),
                    "raw_aggregate": raw.get("raw_aggregate"),
                }
            return {
                "schema_version": BPF_SOCKET_SCHEMA,
                "ok": True,
                "boundary": boundary.to_mapping(self.collector.identity, phase="end"),
                "raw": raw_descriptor,
            }, False
        if operation == "stop":
            summary = self.collector.close()
            # Do not send the action event arrays back over the control socket;
            # they are already durable in the binary stream/JSON journal.
            compact_summary = {
                "schema_version": summary.get("schema_version"),
                "backend": summary.get("backend"),
                "action_count": len(summary.get("actions", [])),
                "finalization_count": len(summary.get("action_finalizations", [])),
                "raw_aggregate_journal": summary.get("raw_aggregate_journal"),
                "raw_event_stream": summary.get("raw_event_stream"),
            }
            return {
                "schema_version": BPF_SOCKET_SCHEMA,
                "ok": True,
                "summary": compact_summary,
            }, True
        raise BpfProtocolError(f"unsupported socket operation: {operation!r}")

    def serve_forever(self) -> None:
        server = self._bind()
        try:
            while True:
                connection, _ = server.accept()
                with connection:
                    try:
                        response, should_stop = self._dispatch(self._read_request(connection))
                    except Exception as exc:
                        response = {
                            "schema_version": BPF_SOCKET_SCHEMA,
                            "ok": False,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                        should_stop = False
                    connection.sendall((_canonical_json(response) + "\n").encode("utf-8"))
                if should_stop:
                    break
        finally:
            server.close()
            self._server = None
            with _suppress_all():
                self.socket_path.unlink()


def _capture_persistent_shell_cwd(
    identity: ProcessIdentity, *, proc_root: Path = Path("/proc")
) -> dict[str, Any]:
    """Read the live shell cwd and prove its path in that process's root.

    A host-visible procfs link is not by itself a container path witness. Open
    the candidate through the target root, rejecting symlink/parent traversal,
    and compare it to the pinned cwd descriptor. This deliberately falls back
    when a namespace path cannot be proved; it never guesses a host prefix.
    """

    snapshot: dict[str, Any] = {
        "schema_version": BPF_CWD_SCHEMA,
        "status": "unavailable",
        "container_cwd": None,
        "identity": identity.to_mapping(),
        "clock_id": "CLOCK_MONOTONIC",
        "started_mono_ns": time.monotonic_ns(),
        "namespace_proof": None,
    }
    descriptors: list[int] = []

    def inode(value: os.stat_result) -> dict[str, int]:
        return {"device": value.st_dev, "inode": value.st_ino}

    try:
        identity.assert_current()
        if identity.container_pid is None:
            raise IdentityBindingError("cwd target has no container PID binding")
        base = proc_root / str(identity.pid)
        expected_pid_ns = f"pid:[{identity.pid_namespace_inode}]"
        pid_ns = os.readlink(base / "ns/pid")
        mount_ns = os.readlink(base / "ns/mnt")
        if pid_ns != expected_pid_ns or (
            identity.pid_namespace is not None and pid_ns != identity.pid_namespace
        ):
            raise IdentityBindingError("cwd target PID namespace differs from service identity")
        if not re.fullmatch(r"mnt:\[[0-9]+\]", mount_ns):
            raise IdentityBindingError("cwd target mount namespace is unavailable")
        flags = os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC
        root_fd = os.open(base / "root", flags)
        descriptors.append(root_fd)
        cwd_fd = os.open(base / "cwd", flags)
        descriptors.append(cwd_fd)
        root_before, cwd_before = inode(os.fstat(root_fd)), inode(os.fstat(cwd_fd))
        cwd = os.readlink(base / "cwd")
        cwd.encode("utf-8", errors="strict")
        if (
            not cwd.startswith("/") or cwd.startswith("//")
            or cwd.endswith(" (deleted)") or "\n" in cwd or "\r" in cwd
            or any(part in {".", ".."} for part in cwd.split("/"))
        ):
            raise IdentityBindingError("cwd link is deleted, unreachable or not a canonical absolute path")
        current_fd = root_fd
        for component in cwd.split("/"):
            if component:
                current_fd = os.open(component, flags | os.O_NOFOLLOW, dir_fd=current_fd)
                descriptors.append(current_fd)
        resolved = inode(os.fstat(current_fd))
        if resolved != cwd_before:
            raise IdentityBindingError("cwd candidate does not resolve to the target cwd through its root")
        root_after, cwd_after = inode(os.stat(base / "root")), inode(os.stat(base / "cwd"))
        pid_ns_after = os.readlink(base / "ns/pid")
        mount_ns_after = os.readlink(base / "ns/mnt")
        if (root_before != root_after or cwd_before != cwd_after
                or os.readlink(base / "cwd") != cwd or pid_ns_after != pid_ns
                or mount_ns_after != mount_ns):
            raise IdentityBindingError("process root, cwd or namespace changed during cwd snapshot")
        identity.assert_current()
        snapshot.update(
            status="measured", container_cwd=cwd,
            namespace_proof={
                "pid_namespace_before": pid_ns, "pid_namespace_after": pid_ns_after,
                "mount_namespace_before": mount_ns, "mount_namespace_after": mount_ns_after,
                "root_before": root_before, "root_after": root_after,
                "cwd_before": cwd_before, "cwd_after": cwd_after,
                "resolved_from_process_root": resolved,
                "resolution": "O_PATH directory walk from target root; O_NOFOLLOW per component",
            },
        )
    except (OSError, ValueError, LinuxWorkError) as exc:
        snapshot.update(error_type=type(exc).__name__, reason=str(exc)[:512])
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        snapshot["ended_mono_ns"] = time.monotonic_ns()
    return snapshot


class BpfWorkClient:
    """Unprivileged action-boundary client implementing the hook collector API."""

    def __init__(self, socket_path: Path, *, identity: Mapping[str, Any] | None = None):
        self.socket_path = _validate_socket_path(Path(socket_path))
        self.identity = dict(identity) if identity is not None else None

    def _call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(request)
        if self.identity is not None:
            payload.setdefault("identity", self.identity)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10.0)
            connection.connect(str(self.socket_path))
            connection.sendall((_canonical_json(payload) + "\n").encode("utf-8"))
            chunks: list[bytes] = []
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
        try:
            response = json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BpfProtocolError("socket response is not valid JSON") from exc
        if not isinstance(response, Mapping):
            raise BpfProtocolError("BPF service response must be a JSON object")
        if response.get("schema_version") != BPF_SOCKET_SCHEMA:
            raise BpfProtocolError("BPF service response has an unsupported schema")
        if not response.get("ok"):
            raise BpfProtocolError(str(response.get("error", "BPF service request failed")))
        return dict(response)

    def ping(self) -> dict[str, Any]:
        """Return and validate the collector identity before accepting work."""

        response = self._call({"op": "ping"})
        identity = response.get("identity")
        if not isinstance(identity, Mapping):
            raise BpfProtocolError("BPF service ping did not return an identity")
        if self.identity is None:
            self.identity = dict(identity)
        else:
            # The server checks the supplied identity too, but compare the
            # complete mapping here so a caller cannot accidentally continue
            # with a client bound to a different process/run.
            expected = dict(self.identity)
            if dict(identity) != expected:
                raise IdentityBindingError("BPF service ping identity differs from the client binding")
        return response

    def cwd_snapshot(self) -> dict[str, Any]:
        """Return a fresh, identity-bound cwd proof, or explicit unavailable."""

        if self.identity is None:
            raise IdentityBindingError("cwd snapshot requires a bound service identity")
        started = time.monotonic_ns()
        response = self._call({"op": "cwd_snapshot"})
        ended = time.monotonic_ns()
        snapshot = response.get("cwd_snapshot")
        if not isinstance(snapshot, Mapping) or snapshot.get("schema_version") != BPF_CWD_SCHEMA:
            raise BpfProtocolError("cwd snapshot is missing or has an unsupported schema")
        if snapshot.get("identity") != self.identity:
            raise IdentityBindingError("cwd snapshot process identity differs from client binding")
        left, right = snapshot.get("started_mono_ns"), snapshot.get("ended_mono_ns")
        if (snapshot.get("clock_id") != "CLOCK_MONOTONIC"
                or type(left) is not int or type(right) is not int
                or not started <= left <= right <= ended):
            raise BpfProtocolError("cwd snapshot is stale or has invalid clock brackets")
        retained = dict(snapshot)
        retained["client_roundtrip"] = {
            "clock_id": "CLOCK_MONOTONIC", "started_mono_ns": started, "ended_mono_ns": ended,
        }
        if snapshot.get("status") == "unavailable":
            if snapshot.get("container_cwd") is not None or not snapshot.get("reason"):
                raise BpfProtocolError("unavailable cwd snapshot lacks a reason or supplies a path")
            return retained
        if snapshot.get("status") != "measured":
            raise BpfProtocolError("cwd snapshot status is invalid")
        path, proof = snapshot.get("container_cwd"), snapshot.get("namespace_proof")
        if (not isinstance(path, str) or not path.startswith("/")
                or path.startswith("//") or path.endswith(" (deleted)")
                or "\n" in path or "\r" in path or "\x00" in path
                or any(part in {".", ".."} for part in path.split("/"))
                or not isinstance(proof, Mapping)):
            raise BpfProtocolError("measured cwd snapshot has no valid path/namespace proof")
        expected_ns = f"pid:[{self.identity['pid_namespace_inode']}]"
        if (proof.get("pid_namespace_before") != expected_ns
                or proof.get("pid_namespace_after") != expected_ns
                or not re.fullmatch(r"mnt:\[[0-9]+\]", str(proof.get("mount_namespace_before")))
                or proof.get("mount_namespace_before") != proof.get("mount_namespace_after")):
            raise IdentityBindingError("cwd snapshot namespace proof changed or mismatches identity")
        for field in ("root_before", "root_after", "cwd_before", "cwd_after", "resolved_from_process_root"):
            value = proof.get(field)
            if (not isinstance(value, Mapping) or set(value) != {"device", "inode"}
                    or type(value["device"]) is not int or value["device"] < 0
                    or type(value["inode"]) is not int or value["inode"] <= 0):
                raise BpfProtocolError("cwd snapshot has an invalid device/inode witness")
        if (proof["root_before"] != proof["root_after"]
                or proof["cwd_before"] != proof["cwd_after"]
                or proof["cwd_before"] != proof["resolved_from_process_root"]):
            raise IdentityBindingError("cwd snapshot path/root/cwd identity proof disagrees")
        return retained

    def stop(self) -> dict[str, Any]:
        """Request an orderly collector close and return its durable summary."""

        response = self._call({"op": "stop"})
        summary = response.get("summary")
        if not isinstance(summary, Mapping):
            raise BpfProtocolError("BPF service stop did not return a summary")
        return dict(summary)

    def start_action(
        self,
        event_id: str,
        command: str,
        *,
        start_wall_ns: int | None = None,
        start_mono_ns: int | None = None,
    ) -> ActionBoundary:
        response = self._call(
            {
                "op": "start_action",
                "event_id": event_id,
                "command": command,
                "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
                "start_wall_ns": start_wall_ns,
                "start_mono_ns": start_mono_ns,
            }
        )
        return ActionBoundary.from_mapping(response["boundary"])

    def end_action(
        self,
        event_id: str,
        *,
        status: str,
        end_wall_ns: int | None = None,
        end_mono_ns: int | None = None,
        timeout: bool = False,
        error: str | None = None,
    ) -> ActionBoundary:
        response = self._call(
            {
                "op": "end_action",
                "event_id": event_id,
                "status": status,
                "end_wall_ns": end_wall_ns,
                "end_mono_ns": end_mono_ns,
                "timeout": timeout,
                "error": error,
            }
        )
        return ActionBoundary.from_mapping(response["boundary"])


@dataclass
class BpfWorkServiceProcess:
    """Handle for the one collector service owned by an instrumentation run.

    The service is a separately launched process which attaches to the exact
    already-running persistent shell named by ``target``.  The handle owns
    only that process; stopping it never signals the target runtime.
    """

    process: subprocess.Popen[Any]
    client: BpfWorkClient
    target: ProcessTarget
    identity: ProcessIdentity
    socket_path: Path
    trace_dir: Path
    lifecycle_path: Path
    stdout_handle: Any | None = None
    stderr_handle: Any | None = None
    _summary: dict[str, Any] | None = None

    def stop(self, *, timeout_s: float = 10.0) -> dict[str, Any]:
        """Close the collector, reap only the owned service, and persist state."""

        if self._summary is not None:
            return dict(self._summary)
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            raise BpfProtocolError("service stop timeout must be positive")
        summary: dict[str, Any] | None = None
        stop_error: BaseException | None = None
        if self.process.poll() is None:
            try:
                summary = self.client.stop()
            except BaseException as exc:
                stop_error = exc
        deadline = time.monotonic() + float(timeout_s)
        try:
            remaining = max(0.05, deadline - time.monotonic())
            self.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            # The process is ours and was launched with start_new_session, so
            # signal only its PID.  Never use a process-group kill here: the
            # collector must not own or terminate the SWE-ReX target.
            with _suppress_all():
                _signal_owned_service(self.process, signal.SIGTERM)
            try:
                self.process.wait(timeout=max(0.05, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                with _suppress_all():
                    _signal_owned_service(self.process, signal.SIGKILL)
                self.process.wait(timeout=1.0)
        finally:
            for handle in (self.stdout_handle, self.stderr_handle):
                if handle is not None:
                    with _suppress_all():
                        handle.close()
            with _suppress_all():
                if self.socket_path.exists() and self.socket_path.is_socket():
                    self.socket_path.unlink()
        if summary is None:
            summary = {
                "schema_version": BPF_SUMMARY_SCHEMA,
                "status": "unavailable",
                "reason": "collector service exited before returning a stop summary",
            }
        summary = dict(summary)
        self._summary = summary
        lifecycle = {
            "schema_version": BPF_SERVICE_LIFECYCLE_SCHEMA,
            "status": "stopped" if stop_error is None and self.process.returncode == 0 else "unavailable",
            "target": self.target.to_mapping(),
            "identity": self.identity.to_mapping(),
            "socket_path": str(self.socket_path),
            "trace_dir": str(self.trace_dir),
            "service_pid": self.process.pid,
            "service_returncode": self.process.returncode,
            "summary": summary,
            "error": f"{type(stop_error).__name__}: {stop_error}" if stop_error else None,
        }
        _write_durable_json(self.lifecycle_path, lifecycle)
        if stop_error is not None:
            raise BpfProtocolError(f"BPF service did not close cleanly: {stop_error}") from stop_error
        return dict(summary)


def _signal_owned_service(process: subprocess.Popen[Any], signum: int) -> None:
    """Signal only our unreaped supervisor, including a sudo-owned process."""
    if process.poll() is not None:
        return
    try:
        process.send_signal(signum)
    except PermissionError:
        subprocess.run(
            ["sudo", "-n", "kill", f"-{int(signum)}", "--", str(process.pid)],
            check=True, capture_output=True, timeout=5,
        )


def _capture_service_identity(target: ProcessTarget) -> ProcessIdentity:
    """Build the identity that the privileged service must independently prove.

    Linux may hide a root-owned container's namespace link from its runner.
    In that specific case use the live runtime's namespace observation, plus
    host-readable start ticks and boot ID. The service still captures the real
    namespace itself; its exact identity must match at the startup handshake.
    """
    try:
        return ProcessIdentity.capture(target)
    except CollectorAttachError as exc:
        if not isinstance(exc.__cause__, PermissionError):
            raise
        namespace = re.fullmatch(r"pid:\[(\d+)\]", target.pid_namespace or "")
        if namespace is None or target.container_pid is None:
            raise
        process_stat = _read_proc_stat(target.pid)
        if process_stat["pid"] != target.pid:
            raise IdentityBindingError("/proc stat PID disagrees with service target") from exc
        return ProcessIdentity(
            pid=target.pid, start_ticks=process_stat["start_ticks"], boot_id=_read_boot_id(),
            pid_namespace_inode=int(namespace.group(1)), run_id=target.run_id,
            attempt_id=target.attempt_id, case_id=target.case_id,
            instance_id=target.instance_id, container_pid=target.container_pid,
            pid_namespace=target.pid_namespace, mapping_source=target.mapping_source,
        )


def launch_bpf_work_service(
    target: ProcessTarget,
    *,
    socket_path: Path,
    trace_dir: Path,
    python_executable: str | None = None,
    cwd: Path | None = None,
    startup_timeout_s: float = 15.0,
    force: bool = False,
    environment: Mapping[str, str] | None = None,
) -> BpfWorkServiceProcess:
    """Launch the owned BCC service and verify its target identity.

    This launcher is intentionally explicit: it never starts a shell and it
    refuses to infer a target from the parent or from a Docker CLI helper PID.
    The caller must supply the PID mapping obtained from the runtime adapter.
    """

    if not isinstance(target, ProcessTarget):
        raise BpfAttachError("BPF service requires an explicit ProcessTarget")
    if isinstance(startup_timeout_s, bool) or not isinstance(startup_timeout_s, (int, float)) or startup_timeout_s <= 0:
        raise BpfAttachError("BPF service startup timeout must be positive")
    identity = _capture_service_identity(target)
    socket_path = _validate_socket_path(Path(socket_path))
    trace_dir = Path(trace_dir).expanduser()
    if trace_dir.is_symlink():
        raise BpfAttachError(f"BPF trace directory must not be a symlink: {trace_dir}")
    trace_dir.mkdir(parents=True, exist_ok=True)
    if not trace_dir.is_dir():
        raise BpfAttachError(f"BPF trace directory is not a directory: {trace_dir}")
    lifecycle_path = trace_dir / "service_lifecycle.json"
    if lifecycle_path.exists() and not force:
        raise BpfAttachError(f"refusing to reuse BPF service lifecycle path: {lifecycle_path}")
    if socket_path.exists() and not socket_path.is_socket():
        raise BpfAttachError(f"refusing to replace non-socket service path: {socket_path}")
    if socket_path.is_socket():
        with _suppress_all():
            socket_path.unlink()

    stdout_path = trace_dir / "service.stdout.log"
    stderr_path = trace_dir / "service.stderr.log"
    if not force and (stdout_path.exists() or stderr_path.exists()):
        raise BpfAttachError(f"refusing to reuse BPF service logs: {trace_dir}")
    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")
    executable = python_executable or "/usr/bin/python3"
    if not isinstance(executable, str) or not executable:
        raise BpfAttachError("BPF service Python executable is invalid")
    argv = [
        executable,
        "-m",
        "agentic_sim.telemetry.bpf_work",
        "serve",
        "--pid",
        str(target.pid),
        "--socket",
        str(socket_path),
        "--trace-dir",
        str(trace_dir),
        "--run-id",
        target.run_id,
        "--attempt-id",
        target.attempt_id,
        "--case-id",
        target.case_id,
        "--mapping-source",
        target.mapping_source,
        "--client-gid",
        str(os.getgid()),
    ]
    if target.instance_id is not None:
        argv.extend(("--instance-id", target.instance_id))
    if target.container_pid is not None:
        argv.extend(("--container-pid", str(target.container_pid)))
    if target.pid_namespace is not None:
        argv.extend(("--pid-namespace", target.pid_namespace))
    if force:
        argv.append("--force")
    child_env = dict(os.environ)
    # The service is a supervisor, not the SWE-agent workload.  Prevent the
    # inherited sitecustomize AUTO flag from installing a second outer hook or
    # overwriting the workload activation marker.
    for key in tuple(child_env):
        if key.startswith("ASSIGNMENT_TELEMETRY_V2_"):
            child_env.pop(key, None)
    child_env["ASSIGNMENT_TELEMETRY_V2_AUTO"] = "0"
    child_env["ASSIGNMENT_TELEMETRY_V2_SUPERVISOR"] = "1"
    if os.geteuid() != 0:
        # Keep the workload unprivileged. Only this owned BCC supervisor needs
        # kernel privileges; BatchMode-style sudo fails rather than prompting.
        source_root = str(Path(__file__).resolve().parents[2])
        argv = [
            "sudo", "-n", "env", f"PYTHONPATH={source_root}",
            "ASSIGNMENT_TELEMETRY_V2_AUTO=0",
            "ASSIGNMENT_TELEMETRY_V2_SUPERVISOR=1", *argv,
        ]
    if environment is not None:
        for key, value in environment.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise BpfAttachError("BPF service environment keys and values must be text")
            if key.startswith("ASSIGNMENT_TELEMETRY_V2_"):
                raise BpfAttachError("BPF service environment cannot re-enable workload telemetry activation")
            child_env[key] = value
    child_env["ASSIGNMENT_TELEMETRY_V2_AUTO"] = "0"
    child_env["ASSIGNMENT_TELEMETRY_V2_SUPERVISOR"] = "1"
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            env=child_env,
            cwd=str(cwd) if cwd is not None else None,
            close_fds=True,
            start_new_session=True,
        )
    except BaseException:
        with _suppress_all():
            stdout_handle.close()
        with _suppress_all():
            stderr_handle.close()
        raise
    client = BpfWorkClient(socket_path, identity=identity.to_mapping())
    handle = BpfWorkServiceProcess(
        process=process,
        client=client,
        target=target,
        identity=identity,
        socket_path=socket_path,
        trace_dir=trace_dir,
        lifecycle_path=lifecycle_path,
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
    )
    deadline = time.monotonic() + float(startup_timeout_s)
    last_error: BaseException | None = None
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                tail = ""
                with _suppress_all():
                    tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                raise BpfAttachError(
                    f"BPF service exited during startup with code {process.returncode}: {tail.strip()}"
                )
            if socket_path.is_socket():
                try:
                    response = client.ping()
                    server_identity = response.get("identity")
                    if not isinstance(server_identity, Mapping) or dict(server_identity) != identity.to_mapping():
                        raise IdentityBindingError("BPF service attached to a different process identity")
                    _write_durable_json(
                        lifecycle_path,
                        {
                            "schema_version": BPF_SERVICE_LIFECYCLE_SCHEMA,
                            "status": "running",
                            "target": target.to_mapping(),
                            "identity": identity.to_mapping(),
                            "socket_path": str(socket_path),
                            "trace_dir": str(trace_dir),
                            "service_pid": process.pid,
                            "argv": argv,
                        },
                    )
                    return handle
                except (OSError, BpfProtocolError, IdentityBindingError) as exc:
                    last_error = exc
            time.sleep(0.02)
        raise BpfAttachError(f"BPF service did not become ready: {last_error or 'timeout'}")
    except BaseException:
        with _suppress_all():
            _signal_owned_service(process, signal.SIGTERM)
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            with _suppress_all():
                _signal_owned_service(process, signal.SIGKILL)
            with _suppress_all():
                process.wait(timeout=1.0)
        with _suppress_all():
            stdout_handle.close()
        with _suppress_all():
            stderr_handle.close()
        raise


def _run_bash_action(
    process: subprocess.Popen[str],
    command: str,
    marker: str,
    timeout_s: float,
    *,
    holder_pid: list[int] | None = None,
) -> float:
    if process.stdin is None or process.stdout is None:
        raise BpfProtocolError("persistent bash fixture pipes are unavailable")
    start = time.perf_counter_ns()
    # Keep the persistent shell in an unsupported wait4() while the caller
    # closes the action boundary.  The marker is emitted by a child that then
    # sleeps; the owner can terminate that exact child after the boundary,
    # before the shell returns to its next blocking stdin read.
    process.stdin.write(
        command
        + "\n(printf '"
        + marker
        + ":%s\\n' \"$BASHPID\"; kill -STOP \"$BASHPID\") & wait $!\n"
    )
    process.stdin.flush()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        remaining = max(0.001, min(0.1, deadline - time.monotonic()))
        ready, _, _ = select.select([process.stdout], [], [], remaining)
        if not ready:
            continue
        line = process.stdout.readline()
        if not line:
            break
        value = line.rstrip("\n")
        if value == marker or value.startswith(marker + ":"):
            if holder_pid is not None and value.startswith(marker + ":"):
                try:
                    holder_pid.append(int(value.rsplit(":", 1)[1]))
                except (IndexError, ValueError):
                    raise BpfProtocolError(f"persistent bash fixture emitted invalid holder PID: {value}")
            return (time.perf_counter_ns() - start) / 1_000_000
    raise BpfProtocolError(f"persistent bash fixture did not reach marker {marker}")


def _release_holder(process: subprocess.Popen[str], holder_pid: Sequence[int]) -> None:
    """Release only a marker child owned by the persistent fixture."""

    for pid in holder_pid:
        if pid <= 0:
            continue
        with _suppress_all():
            os.kill(pid, signal.SIGCONT)
            os.kill(pid, signal.SIGTERM)


def _run_bash_action_unmodified(
    process: subprocess.Popen[str],
    command: str,
    marker: str,
    timeout_s: float,
) -> float:
    """Run a shell action without stopping a helper child or holding ``wait``.

    This fixture deliberately lets the persistent shell return to its normal
    stdin read after printing the completion marker.  It is used only to
    exercise the action-close race: any read or descendant work that remains
    active at the boundary must make the action explicitly unavailable or
    appear in the boundary-censoring witness.
    """

    if process.stdin is None or process.stdout is None:
        raise BpfProtocolError("persistent bash fixture pipes are unavailable")
    start = time.perf_counter_ns()
    process.stdin.write(
        "{ " + command + "; } >/dev/null\n"
        + "printf '"
        + marker
        + ":%s\\n' \"$?\"\n"
    )
    process.stdin.flush()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        remaining = max(0.001, min(0.1, deadline - time.monotonic()))
        ready, _, _ = select.select([process.stdout], [], [], remaining)
        if not ready:
            continue
        line = process.stdout.readline()
        if not line:
            break
        value = line.rstrip("\n")
        if value.startswith(marker + ":"):
            if value != marker + ":0":
                raise BpfProtocolError(f"fixture failed: {value}")
            return (time.perf_counter_ns() - start) / 1_000_000
    raise BpfProtocolError(f"persistent bash fixture did not reach marker {marker}")


def _spawn_bash_fixture() -> subprocess.Popen[str]:
    return subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _stop_bash_fixture(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        with _suppress_all():
            if process.stdin is not None:
                process.stdin.write("exit\n")
                process.stdin.flush()
            process.wait(timeout=2.0)
    if process.poll() is None:
        with _suppress_all():
            process.kill()
            process.wait(timeout=2.0)
    for stream in (process.stdin, process.stdout, process.stderr):
        with _suppress_all():
            if stream is not None:
                stream.close()


def measure_bpf_overhead(
    command: Sequence[str] | str,
    *,
    repeats: int = 3,
    output_dir: Path,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Measure persistent-BCC action overhead with startup reported separately."""

    if isinstance(command, str):
        fixture_command = command
    elif command and all(isinstance(part, str) for part in command):
        fixture_command = shlex.join(command)
    else:
        raise LinuxWorkError("BPF overhead command must be text or argv")
    if repeats <= 0 or timeout_s <= 0:
        raise LinuxWorkError("repeats and timeout_s must be positive")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    controls: list[float] = []
    controls_after: list[float] = []
    controls_probes_attached: list[float] = []
    instrumented: list[float] = []
    boundary_start_ms: list[float] = []
    boundary_end_ms: list[float] = []
    instrumented_total: list[float] = []
    collector: BpfWorkCollector | None = None
    service: BpfWorkService | None = None
    service_thread: threading.Thread | None = None
    client: BpfWorkClient | None = None
    service_errors: list[str] = []
    service_summary: dict[str, Any] | None = None
    service_setup_wall_ms: float | None = None
    service_stop_wall_ms: float | None = None
    service_thread_alive_after_stop: bool | None = None
    service_stopped = False
    collector_cleanup: dict[str, Any] | None = None
    startup_wall_ms: float | None = None
    control_process = _spawn_bash_fixture()
    instrumented_process = _spawn_bash_fixture()
    try:
        _run_bash_action_unmodified(
            control_process, ":", "__READY_CONTROL__", timeout_s
        )
        _run_bash_action_unmodified(
            instrumented_process, ":", "__READY_INSTRUMENTED__", timeout_s
        )
        # Raw tracepoints execute even for an unmapped control PID. Measure
        # genuinely probe-free controls before attach and after detach, and
        # retain the attached-but-unmapped timings as a separate diagnostic.
        for repeat in range(1, repeats + 1):
            controls.append(_run_bash_action_unmodified(
                control_process, fixture_command, f"__PRE_{repeat}__", timeout_s
            ))
        target = ProcessTarget(
            pid=instrumented_process.pid,
            run_id="bpf-overhead",
            attempt_id="instrumented",
            case_id="bpf-overhead-fixture",
            mapping_source="bpf_overhead_persistent_bash",
        )
        startup_started = time.perf_counter_ns()
        collector = BpfWorkCollector.attach(target, output_dir / "instrumented")
        startup_wall_ms = (time.perf_counter_ns() - startup_started) / 1_000_000
        # Exercise the same boundary transport used by an unprivileged hook.
        # The BCC collector remains in this process for the fixture, while the
        # small service thread owns the listening socket.  Socket round trips,
        # kernel-map snapshots, perf draining, and durable boundary writes are
        # therefore included in the measured start/end costs.
        socket_path = output_dir / "bpf.sock"
        service = BpfWorkService(collector, socket_path)

        def serve_service() -> None:
            try:
                service.serve_forever()  # type: ignore[union-attr]
            except BaseException as exc:
                service_errors.append(f"{type(exc).__name__}: {exc}")

        service_started = time.perf_counter_ns()
        service_thread = threading.Thread(
            target=serve_service,
            name="agentic-bpf-overhead-service",
            daemon=True,
        )
        service_thread.start()
        service_deadline = time.monotonic() + min(10.0, timeout_s)
        while time.monotonic() < service_deadline and not socket_path.is_socket():
            if not service_thread.is_alive():
                break
            time.sleep(0.002)
        if not socket_path.is_socket():
            raise BpfProtocolError(
                "BPF overhead service did not create its Unix socket"
                + (f": {service_errors[-1]}" if service_errors else "")
            )
        client = BpfWorkClient(socket_path, identity=collector.identity.to_mapping())
        client.ping()
        service_setup_wall_ms = (time.perf_counter_ns() - service_started) / 1_000_000
        for repeat in range(1, repeats + 1):
            control_elapsed = _run_bash_action_unmodified(
                control_process,
                fixture_command,
                f"__C_{repeat}__",
                timeout_s,
            )
            controls_probes_attached.append(control_elapsed)
            event_id = f"bpf-fixture-{repeat}"
            total_started = time.perf_counter_ns()
            boundary_started = time.perf_counter_ns()
            client.start_action(event_id, fixture_command)
            boundary_start_ms.append((time.perf_counter_ns() - boundary_started) / 1_000_000)
            instrumented.append(
                _run_bash_action_unmodified(
                    instrumented_process,
                    fixture_command,
                    f"__I_{repeat}__",
                    timeout_s,
                )
            )
            boundary_started = time.perf_counter_ns()
            client.end_action(event_id, status="success")
            boundary_end_ms.append((time.perf_counter_ns() - boundary_started) / 1_000_000)
            instrumented_total.append((time.perf_counter_ns() - total_started) / 1_000_000)
    finally:
        # Stop the service before tearing down the target fixture.  This keeps
        # the normal stop response on the same socket path and lets close()
        # produce its final raw-stream hash while the target identity is still
        # observable.  Any exception here is recorded in the result; cleanup
        # below still closes the collector and reaps only the fixture PIDs.
        if service_thread is not None and service_thread.is_alive():
            if client is not None:
                stop_started = time.perf_counter_ns()
                try:
                    service_summary = client.stop()
                    service_stopped = True
                except BaseException as exc:
                    service_errors.append(f"{type(exc).__name__}: {exc}")
                service_stop_wall_ms = (time.perf_counter_ns() - stop_started) / 1_000_000
            service_thread.join(timeout=max(1.0, min(10.0, timeout_s)))
        if service_thread is not None:
            service_thread_alive_after_stop = service_thread.is_alive()
        if service_stopped and service_thread_alive_after_stop is False:
            for repeat in range(1, repeats + 1):
                controls_after.append(_run_bash_action_unmodified(
                    control_process, fixture_command, f"__POST_{repeat}__", timeout_s
                ))
        _stop_bash_fixture(control_process)
        _stop_bash_fixture(instrumented_process)
        if collector is not None:
            with _suppress_all():
                collector.close()
            collector_cleanup = {
                "closed": bool(getattr(collector, "_closed", False)),
                "bpf_handle_present": getattr(collector, "bpf", None) is not None,
                "raw_event_stream_open": getattr(collector, "_raw_event_stream", None) is not None,
                "perf_thread_alive": bool(
                    getattr(getattr(collector, "_perf_thread", None), "is_alive", lambda: False)()
                ),
                "socket_exists": (output_dir / "bpf.sock").exists(),
            }
    pairs = [
        {
            "repeat": index,
            "control_execution_wall_ms": controls[index - 1],
            "instrumented_execution_wall_ms": instrumented[index - 1],
            "execution_wall_delta_ms": instrumented[index - 1] - controls[index - 1],
            "instrumented_total_wall_ms": instrumented_total[index - 1],
            "total_wall_delta_ms": instrumented_total[index - 1] - controls[index - 1],
            "control_probes_attached_wall_ms": controls_probes_attached[index - 1],
            "control_after_detach_wall_ms": controls_after[index - 1] if len(controls_after) >= index else None,
        }
        for index in range(1, min(len(controls), len(instrumented)) + 1)
    ]
    cleanup_ok = collector_cleanup is not None and all(
        (
            collector_cleanup.get("closed") is True,
            collector_cleanup.get("bpf_handle_present") is False,
            collector_cleanup.get("raw_event_stream_open") is False,
            collector_cleanup.get("perf_thread_alive") is False,
            collector_cleanup.get("socket_exists") is False,
        )
    )
    measurement_complete = (
        len(pairs) == repeats
        and len(controls_after) == repeats
        and service_stopped
        and service_thread_alive_after_stop is False
        and not service_errors
        and cleanup_ok
        and collector is not None
        and len(collector._completed) == repeats
        and all(row.get("event_records_complete") is True for row in collector._completed)
        and all(row.get("event_records_complete") is True for row in collector._finalizations)
    )
    return {
        "schema_version": "assignment.linux-bpf-work-overhead.v1",
        "backend": "bcc",
        "fixture_command": fixture_command,
        "repeats": repeats,
        "startup_wall_ms": startup_wall_ms if collector is not None else None,
        "service_setup_wall_ms": service_setup_wall_ms,
        "service_stop_wall_ms": service_stop_wall_ms,
        "service_socket_path": str(output_dir / "bpf.sock"),
        "service_stopped": service_stopped,
        "service_thread_alive_after_stop": service_thread_alive_after_stop,
        "service_errors": service_errors,
        "service_stop_summary": service_summary,
        "collector_cleanup": collector_cleanup,
        "control_execution_wall_ms": controls,
        "control_after_detach_wall_ms": controls_after,
        "control_probes_attached_wall_ms": controls_probes_attached,
        "instrumented_execution_wall_ms": instrumented,
        "instrumented_start_boundary_wall_ms": boundary_start_ms,
        "instrumented_end_boundary_wall_ms": boundary_end_ms,
        "instrumented_total_wall_ms": instrumented_total,
        "paired": pairs,
        "status": "measured" if measurement_complete else "unavailable",
        "limitations": [
            "The collector is compiled and attached once before all instrumented actions; startup is reported separately.",
            "Control and instrumented actions run in separate persistent bash processes and include the same completion marker protocol.",
            "Controls used in paired deltas run before attach; post-detach controls expose order/cache drift. Attached-but-unmapped controls are diagnostic only and do not hide global probe overhead.",
            "The persistent shell is unmodified and returns to its normal stdin read; there is no stopped helper. Fixture exit status must be zero.",
            "Instrumented start/end measurements include the BCC Unix-socket client round trips, kernel-map snapshots, continuous perf draining, binary-stream flush/fsync, and durable boundary writes.",
            "Kernel aggregate map loss and unknown descriptors remain in each raw action record.",
        ],
    }


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="subcommand", required=True)
    serve = sub.add_parser("serve", help="attach BCC and serve action boundaries over a Unix socket")
    serve.add_argument("--pid", type=int, required=True)
    serve.add_argument("--socket", type=Path, required=True)
    serve.add_argument("--trace-dir", type=Path, required=True)
    serve.add_argument("--run-id", required=True)
    serve.add_argument("--attempt-id", required=True)
    serve.add_argument("--case-id", required=True)
    serve.add_argument("--instance-id")
    serve.add_argument("--container-pid", type=int)
    serve.add_argument("--pid-namespace")
    serve.add_argument("--mapping-source", default="explicit_bpf_service_pid_mapping")
    serve.add_argument("--force", action="store_true")
    serve.add_argument("--client-gid", type=int)
    overhead = sub.add_parser("overhead", help="run paired persistent-bash BCC overhead fixture")
    overhead.add_argument("--output-dir", type=Path, required=True)
    overhead.add_argument("--repeats", type=int, default=3)
    overhead.add_argument("--timeout-s", type=float, default=60.0)
    overhead.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _cli_parser().parse_args(argv)
    try:
        if args.subcommand == "serve":
            target = ProcessTarget(
                pid=args.pid,
                run_id=args.run_id,
                attempt_id=args.attempt_id,
                case_id=args.case_id,
                instance_id=args.instance_id,
                container_pid=args.container_pid,
                pid_namespace=args.pid_namespace,
                mapping_source=args.mapping_source,
            )
            if args.client_gid is not None:
                if args.client_gid < 0:
                    raise BpfAttachError("client group must be nonnegative")
                if args.trace_dir.is_symlink():
                    raise BpfAttachError("trace directory must not be a symlink")
                args.trace_dir.mkdir(parents=True, exist_ok=True)
                # The launcher creates this caller-owned directory first.
                # Inherit its reader group for new 0640 journals and atomic
                # replacements without making them globally readable.
                os.chown(args.trace_dir, -1, args.client_gid)
                os.chmod(args.trace_dir, stat.S_IMODE(args.trace_dir.stat().st_mode) | stat.S_ISGID)
            collector = BpfWorkCollector.attach(target, args.trace_dir, force=args.force)
            BpfWorkService(collector, args.socket, client_gid=args.client_gid).serve_forever()
            return 0
        if args.subcommand == "overhead":
            command = list(args.command)
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                _cli_parser().error("overhead requires a fixture command after --")
            result = measure_bpf_overhead(
                command,
                repeats=args.repeats,
                output_dir=args.output_dir,
                timeout_s=args.timeout_s,
            )
            _write_durable_json(args.output_dir / "overhead-result.json", result)
            print(_canonical_json(result))
            return 0
    except (LinuxWorkError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


__all__ = [
    "BPF_COLLECTOR_SCHEMA",
    "BPF_PATH_CAP",
    "BPF_PERF_BUFFER_PAGES_PER_CPU",
    "BPF_PROGRAM",
    "BPF_RAW_SCHEMA",
    "BPF_SERVICE_LIFECYCLE_SCHEMA",
    "BPF_SOCKET_SCHEMA",
    "BPF_SUMMARY_SCHEMA",
    "BpfAttachError",
    "BpfProtocolError",
    "BpfWorkClient",
    "BpfWorkCollector",
    "BpfWorkService",
    "BpfWorkServiceProcess",
    "launch_bpf_work_service",
    "measure_bpf_overhead",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
