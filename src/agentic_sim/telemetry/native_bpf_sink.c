#define _GNU_SOURCE
#define _POSIX_C_SOURCE 200809L

/*
 * Small native sink for BCC's raw perf callback.
 *
 * The callback accepts one fixed 400-byte v3 work_event packet and may receive
 * up to seven bytes of perf record padding.  Only the packet is persisted;
 * padding is transport metadata and is deliberately excluded from the raw
 * stream offset.  All counters and file operations are protected by the
 * same mutex.  In particular, sink_boundary() keeps the mutex held across
 * fflush/fsync and the returned counter snapshot so a later callback cannot
 * be mistaken for part of the durable range.
 */

#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <linux/perf_event.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#define SINK_RECORD_BYTES 400U
#define SINK_PERF_PADDING_BYTES 7U
#define SINK_TOKEN_CAPACITY 4096U
#define SINK_BUFFER_BYTES (1024U * 1024U)
#define SINK_FLUSH_RECORDS (SINK_BUFFER_BYTES / SINK_RECORD_BYTES)

#if defined(__GNUC__)
#define SINK_API __attribute__((visibility("default")))
#else
#define SINK_API
#endif

struct stats {
    uint64_t offset_bytes;
    uint64_t total_records;
    uint64_t token_records;
    uint64_t lost;
    uint64_t errors;
};

struct token_counter {
    uint64_t token;
    uint64_t count;
    unsigned char used;
};

struct sink_context {
    FILE *stream;
    int fd;
    pthread_mutex_t mutex;
    struct token_counter token_counters[SINK_TOKEN_CAPACITY];
    uint64_t offset_bytes;
    uint64_t total_records;
    uint64_t lost;
    uint64_t errors;
};

_Static_assert(SINK_TOKEN_CAPACITY >= 4096U, "token table must remain bounded and at least 4096 entries");
_Static_assert((SINK_TOKEN_CAPACITY & (SINK_TOKEN_CAPACITY - 1U)) == 0U, "token table capacity must be a power of two");
_Static_assert(SINK_FLUSH_RECORDS > 0U, "periodic flush interval must be non-zero");

static void increment_counter(uint64_t *value)
{
    if (*value != UINT64_MAX) {
        ++*value;
    }
}

static void add_offset(struct sink_context *context, size_t amount)
{
    uint64_t value = (uint64_t)amount;
    if (UINT64_MAX - context->offset_bytes < value) {
        context->offset_bytes = UINT64_MAX;
        increment_counter(&context->errors);
        return;
    }
    context->offset_bytes += value;
}

static uint64_t hash_token(uint64_t token)
{
    /* SplitMix64 gives stable distribution for sequential action tokens. */
    uint64_t value = token;
    value += UINT64_C(0x9e3779b97f4a7c15);
    value = (value ^ (value >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    value = (value ^ (value >> 27)) * UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}

static const struct token_counter *find_token_locked(const struct sink_context *context, uint64_t token)
{
    size_t start;
    size_t offset;

    if (token == 0U) {
        return NULL;
    }
    start = (size_t)(hash_token(token) & (SINK_TOKEN_CAPACITY - 1U));
    for (offset = 0U; offset < SINK_TOKEN_CAPACITY; ++offset) {
        const struct token_counter *entry = &context->token_counters[(start + offset) & (SINK_TOKEN_CAPACITY - 1U)];
        if (!entry->used) {
            return NULL;
        }
        if (entry->token == token) {
            return entry;
        }
    }
    return NULL;
}

static int increment_token_locked(struct sink_context *context, uint64_t token)
{
    size_t start;
    size_t offset;

    /* Zero is the public "no token filter" sentinel, never a token key. */
    if (token == 0U) {
        return 0;
    }
    start = (size_t)(hash_token(token) & (SINK_TOKEN_CAPACITY - 1U));
    for (offset = 0U; offset < SINK_TOKEN_CAPACITY; ++offset) {
        struct token_counter *entry = &context->token_counters[(start + offset) & (SINK_TOKEN_CAPACITY - 1U)];
        if (!entry->used) {
            entry->used = 1U;
            entry->token = token;
            entry->count = 1U;
            return 0;
        }
        if (entry->token == token) {
            if (entry->count == UINT64_MAX) {
                return -1;
            }
            ++entry->count;
            return 0;
        }
    }
    return -1;
}

static int lock_context(struct sink_context *context)
{
    int result = pthread_mutex_lock(&context->mutex);
    if (result != 0) {
        errno = result;
        return -1;
    }
    return 0;
}

static int unlock_context(struct sink_context *context)
{
    int result = pthread_mutex_unlock(&context->mutex);
    if (result != 0) {
        errno = result;
        return -1;
    }
    return 0;
}

static int flush_locked(struct sink_context *context, int fsync_flag)
{
    int result = 0;

    if (context->stream == NULL || context->fd < 0) {
        errno = EBADF;
        increment_counter(&context->errors);
        return -1;
    }
    if (fflush(context->stream) != 0) {
        increment_counter(&context->errors);
        result = -1;
    }
    if (fsync_flag != 0 && fsync(context->fd) != 0) {
        increment_counter(&context->errors);
        result = -1;
    }
    return result;
}

static void snapshot_locked(const struct sink_context *context, uint64_t token, struct stats *out)
{
    const struct token_counter *entry;

    out->offset_bytes = context->offset_bytes;
    out->total_records = context->total_records;
    out->lost = context->lost;
    out->errors = context->errors;
    /* token==0 is the explicit no-token query and must return zero. */
    entry = find_token_locked(context, token);
    out->token_records = entry == NULL ? 0U : entry->count;
}

SINK_API void *sink_open(const char *path)
{
    struct sink_context *context;
    FILE *stream;
    int mutex_result;

    if (path == NULL || *path == '\0') {
        errno = EINVAL;
        return NULL;
    }
    context = (struct sink_context *)calloc(1U, sizeof(*context));
    if (context == NULL) {
        return NULL;
    }
    context->fd = -1;
    /* x makes the raw stream creation atomic and refuses both overwrite and
     * a symlink at the requested path.  The Python wrapper also refuses to
     * replace its .so; the sink must enforce the same boundary itself. */
    stream = fopen(path, "wbx");
    if (stream == NULL) {
        free(context);
        return NULL;
    }
    if (setvbuf(stream, NULL, _IOFBF, SINK_BUFFER_BYTES) != 0) {
        int saved_errno = errno == 0 ? EIO : errno;
        fclose(stream);
        free(context);
        errno = saved_errno;
        return NULL;
    }
    context->fd = fileno(stream);
    if (context->fd < 0) {
        int saved_errno = errno == 0 ? EBADF : errno;
        fclose(stream);
        free(context);
        errno = saved_errno;
        return NULL;
    }
    mutex_result = pthread_mutex_init(&context->mutex, NULL);
    if (mutex_result != 0) {
        fclose(stream);
        free(context);
        errno = mutex_result;
        return NULL;
    }
    context->stream = stream;
    return context;
}

SINK_API int sink_perf_event_open(int cpu, unsigned int wakeup_events)
{
    struct perf_event_attr attr;
    long result;

    memset(&attr, 0, sizeof(attr));
    attr.size = sizeof(attr);
    attr.type = PERF_TYPE_SOFTWARE;
    attr.config = PERF_COUNT_SW_BPF_OUTPUT;
    attr.sample_type = PERF_SAMPLE_RAW;
    attr.sample_period = 1U;
    attr.wakeup_events = wakeup_events;
    /* The reader is attached and mmap'd by the caller before it is enabled. */
    attr.disabled = 1U;
    result = syscall(SYS_perf_event_open, &attr, -1, cpu, -1, PERF_FLAG_FD_CLOEXEC);
    if (result < 0) {
        return -1;
    }
    return (int)result;
}

SINK_API int sink_perf_event_enable(int fd)
{
    if (fd < 0) {
        errno = EINVAL;
        return -1;
    }
    if (ioctl(fd, PERF_EVENT_IOC_ENABLE, 0) < 0) {
        return -1;
    }
    return 0;
}

SINK_API void sink_event(void *opaque, void *data, int size)
{
    struct sink_context *context = (struct sink_context *)opaque;
    size_t written;
    uint64_t token;

    if (context == NULL || lock_context(context) != 0) {
        return;
    }
    if (data == NULL || size < (int)SINK_RECORD_BYTES ||
        size > (int)(SINK_RECORD_BYTES + SINK_PERF_PADDING_BYTES)) {
        increment_counter(&context->errors);
        (void)unlock_context(context);
        return;
    }

    /* The BPF work_event ABI begins with the little-endian u64 action token. */
    memcpy(&token, data, sizeof(token));
    written = fwrite(data, 1U, SINK_RECORD_BYTES, context->stream);
    if (written != SINK_RECORD_BYTES) {
        add_offset(context, written);
        increment_counter(&context->errors);
        (void)unlock_context(context);
        return;
    }
    add_offset(context, SINK_RECORD_BYTES);
    increment_counter(&context->total_records);
    if (increment_token_locked(context, token) != 0) {
        /* The packet is present, but its token count is not complete. */
        increment_counter(&context->errors);
    }
    /* libc also flushes when this 1 MiB buffer fills.  The explicit record
     * interval makes the periodic policy deterministic and keeps the maximum
     * buffered raw range below one buffer between callbacks. */
    if (context->total_records % SINK_FLUSH_RECORDS == 0U && fflush(context->stream) != 0) {
        increment_counter(&context->errors);
    }
    (void)unlock_context(context);
}

SINK_API void sink_lost(void *opaque, uint64_t count)
{
    struct sink_context *context = (struct sink_context *)opaque;

    if (context == NULL || lock_context(context) != 0) {
        return;
    }
    if (UINT64_MAX - context->lost < count) {
        context->lost = UINT64_MAX;
        increment_counter(&context->errors);
    } else {
        context->lost += count;
    }
    (void)unlock_context(context);
}

SINK_API int sink_stats(void *opaque, uint64_t token, struct stats *out)
{
    struct sink_context *context = (struct sink_context *)opaque;
    int result;

    if (context == NULL || out == NULL) {
        errno = EINVAL;
        return -1;
    }
    if (lock_context(context) != 0) {
        return -1;
    }
    snapshot_locked(context, token, out);
    result = unlock_context(context);
    return result;
}

SINK_API int sink_flush(void *opaque, int fsync_flag)
{
    struct sink_context *context = (struct sink_context *)opaque;
    int result;
    int unlock_result;

    if (context == NULL) {
        errno = EINVAL;
        return -1;
    }
    if (lock_context(context) != 0) {
        return -1;
    }
    result = flush_locked(context, fsync_flag);
    unlock_result = unlock_context(context);
    return result != 0 ? result : unlock_result;
}

SINK_API int sink_boundary(void *opaque, uint64_t token, int fsync_flag, struct stats *out)
{
    struct sink_context *context = (struct sink_context *)opaque;
    int result;
    int unlock_result;

    if (context == NULL || out == NULL) {
        errno = EINVAL;
        return -1;
    }
    if (lock_context(context) != 0) {
        return -1;
    }
    /* Keep this critical section intact: no callback can advance the range
     * between the flush/fsync and the snapshot that Python consumes. */
    result = flush_locked(context, fsync_flag);
    snapshot_locked(context, token, out);
    unlock_result = unlock_context(context);
    return result != 0 ? result : unlock_result;
}

SINK_API int sink_close(void *opaque)
{
    struct sink_context *context = (struct sink_context *)opaque;
    int result = 0;
    int unlock_result;

    if (context == NULL) {
        errno = EINVAL;
        return -1;
    }
    if (lock_context(context) != 0) {
        return -1;
    }
    if (flush_locked(context, 1) != 0) {
        result = -1;
    }
    if (fclose(context->stream) != 0) {
        increment_counter(&context->errors);
        result = -1;
    }
    context->stream = NULL;
    context->fd = -1;
    unlock_result = unlock_context(context);
    if (unlock_result != 0) {
        result = -1;
    }
    if (pthread_mutex_destroy(&context->mutex) != 0) {
        result = -1;
    }
    free(context);
    return result;
}
