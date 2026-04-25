// SPDX-License-Identifier: GPL-2.0
// CCDT Layer-1 Nervous System — Scheduler Latency Probe
//
// Measures per-task run-queue latency using sched_wakeup + sched_switch
// tracepoints. Emits events to ring buffer when latency > LATENCY_THRESH_NS (1ms).
// Maintains a per-CPU log2 histogram for percentile computation in user-space.
//
// Compile (from Makefile):
//   clang -g -O2 -target bpf -D__TARGET_ARCH_x86 \
//         -I./include -c scheduler.bpf.c -o scheduler.bpf.o

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

// ─── Configuration ────────────────────────────────────────────────────────────
#define LATENCY_THRESH_NS  1000000ULL   // 1 ms — only ring-buffer emit above this
#define TASK_COMM_LEN      16
#define HIST_BUCKETS       26           // log2 0..25 covers 1 ns → ~33 s
#define MAX_PIDS           10240

// ─── Event struct (emitted to ring buffer) ────────────────────────────────────
// Must stay in sync with normalizer.go:SchedEvent
struct sched_event {
    __u64 latency_ns;           // measured run-queue latency (nanoseconds)
    __u64 wakeup_ts;            // ktime at task wakeup
    __u32 pid;
    __u32 tgid;
    __u32 cpu;
    __s32 prio;                 // Linux scheduler priority
    __u8  comm[TASK_COMM_LEN];
    __u8  pad[4];
};

// ─── Maps ─────────────────────────────────────────────────────────────────────

// wakeup_ts: pid → ktime_get_ns() when task was woken
struct {
    __uint(type,        BPF_MAP_TYPE_HASH);
    __uint(max_entries, MAX_PIDS);
    __type(key,   __u32);
    __type(value, __u64);
} wakeup_ts SEC(".maps");

// latency_hist: per-CPU log2 histogram (bucket index → count)
struct {
    __uint(type,        BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, HIST_BUCKETS);
    __type(key,   __u32);
    __type(value, __u64);
} latency_hist SEC(".maps");

// events: ring buffer — user-space drains this
struct {
    __uint(type,        BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 16 * 1024 * 1024);   // 16 MB
} events SEC(".maps");

// ─── Helper: log2 bucket (verifier-safe unrolled) ────────────────────────────
static __always_inline __u32 log2_bucket(__u64 v)
{
    __u32 b = 0;
    if (v >= (1ULL << 1))  b = 1;  if (v >= (1ULL << 2))  b = 2;
    if (v >= (1ULL << 3))  b = 3;  if (v >= (1ULL << 4))  b = 4;
    if (v >= (1ULL << 5))  b = 5;  if (v >= (1ULL << 6))  b = 6;
    if (v >= (1ULL << 7))  b = 7;  if (v >= (1ULL << 8))  b = 8;
    if (v >= (1ULL << 9))  b = 9;  if (v >= (1ULL << 10)) b = 10;
    if (v >= (1ULL << 11)) b = 11; if (v >= (1ULL << 12)) b = 12;
    if (v >= (1ULL << 13)) b = 13; if (v >= (1ULL << 14)) b = 14;
    if (v >= (1ULL << 15)) b = 15; if (v >= (1ULL << 16)) b = 16;
    if (v >= (1ULL << 17)) b = 17; if (v >= (1ULL << 18)) b = 18;
    if (v >= (1ULL << 19)) b = 19; if (v >= (1ULL << 20)) b = 20;
    if (v >= (1ULL << 21)) b = 21; if (v >= (1ULL << 22)) b = 22;
    if (v >= (1ULL << 23)) b = 23; if (v >= (1ULL << 24)) b = 24;
    return (b < HIST_BUCKETS) ? b : (HIST_BUCKETS - 1);
}

// ─── Tracepoint: sched_wakeup — record wakeup timestamp ──────────────────────
SEC("tp/sched/sched_wakeup")
int handle_sched_wakeup(struct trace_event_raw_sched_wakeup *ctx)
{
    __u32 pid = ctx->pid;
    __u64 now = bpf_ktime_get_ns();
    bpf_map_update_elem(&wakeup_ts, &pid, &now, BPF_ANY);
    return 0;
}

// Newly-forked tasks use sched_wakeup_new
SEC("tp/sched/sched_wakeup_new")
int handle_sched_wakeup_new(struct trace_event_raw_sched_wakeup *ctx)
{
    __u32 pid = ctx->pid;
    __u64 now = bpf_ktime_get_ns();
    bpf_map_update_elem(&wakeup_ts, &pid, &now, BPF_ANY);
    return 0;
}

// ─── Tracepoint: sched_switch — measure latency when task is scheduled ────────
SEC("tp/sched/sched_switch")
int handle_sched_switch(struct trace_event_raw_sched_switch *ctx)
{
    __u32 next_pid = ctx->next_pid;

    __u64 *wakeup = bpf_map_lookup_elem(&wakeup_ts, &next_pid);
    if (!wakeup)
        return 0;

    __u64 now        = bpf_ktime_get_ns();
    __u64 latency_ns = now - *wakeup;
    __u64 wakeup_ts_val = *wakeup;

    bpf_map_delete_elem(&wakeup_ts, &next_pid);

    // Update histogram in all cases (not just threshold crossings)
    __u32  bucket = log2_bucket(latency_ns);
    __u64 *cnt    = bpf_map_lookup_elem(&latency_hist, &bucket);
    if (cnt)
        __sync_fetch_and_add(cnt, 1);

    // Only emit event when above threshold to bound ring-buffer bandwidth
    if (latency_ns < LATENCY_THRESH_NS)
        return 0;

    struct sched_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    evt->latency_ns  = latency_ns;
    evt->wakeup_ts   = wakeup_ts_val;
    evt->pid         = next_pid;
    evt->tgid        = bpf_get_current_pid_tgid() >> 32;
    evt->cpu         = bpf_get_smp_processor_id();
    evt->prio        = ctx->next_prio;
    bpf_probe_read_kernel_str(evt->comm, TASK_COMM_LEN, ctx->next_comm);
    __builtin_memset(evt->pad, 0, sizeof(evt->pad));

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
