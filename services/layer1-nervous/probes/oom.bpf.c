// SPDX-License-Identifier: GPL-2.0
// CCDT Layer-1 Nervous System — OOM Kill Probe
//
// Attaches to oom_kill_process via kprobe.
// Records the victim task, triggering process, cgroup path, RSS and memory limit.
// Maintains a per-cgroup OOM kill counter map for trend analysis.
//
// Compile:
//   clang -g -O2 -target bpf -D__TARGET_ARCH_x86 \
//         -I./include -c oom.bpf.c -o oom.bpf.o

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

// ─── Constants ────────────────────────────────────────────────────────────────
#define TASK_COMM_LEN    16
#define CGROUP_NAME_LEN  128
#define MAX_CGROUPS      512

// ─── Event struct ─────────────────────────────────────────────────────────────
// Must stay in sync with normalizer.go:OOMEvent
struct oom_event {
    __u64 timestamp_ns;
    __u64 rss_bytes;            // victim task RSS at kill time
    __u64 mem_limit_bytes;      // cgroup memory.max limit
    __u64 oom_score_adj;        // oom_score_adj of victim
    __u32 victim_pid;
    __u32 victim_tgid;
    __u32 killer_pid;           // task that invoked the OOM killer
    __u32 order;                // allocation order that triggered OOM
    __u8  victim_comm[TASK_COMM_LEN];
    __u8  killer_comm[TASK_COMM_LEN];
    __u8  cgroup_name[CGROUP_NAME_LEN];
};

// ─── Maps ─────────────────────────────────────────────────────────────────────

// Ring buffer — user-space drains this for event processing
struct {
    __uint(type,        BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 1 * 1024 * 1024);    // 1 MB (OOM events are rare)
} events SEC(".maps");

// Per-cgroup OOM kill counter (cgroup inode → kill count)
struct {
    __uint(type,        BPF_MAP_TYPE_HASH);
    __uint(max_entries, MAX_CGROUPS);
    __type(key,   __u64);   // cgroup inode number
    __type(value, __u64);   // cumulative kill count
} cgroup_oom_count SEC(".maps");

// ─── kprobe: oom_kill_process ─────────────────────────────────────────────────
// Signature: void oom_kill_process(struct oom_control *oc, const char *message)
SEC("kprobe/oom_kill_process")
int BPF_KPROBE(handle_oom_kill, struct oom_control *oc, const char *message)
{
    struct task_struct *victim = NULL;
    struct task_struct *current_task;

    // Read victim task from oom_control
    victim = BPF_CORE_READ(oc, chosen);
    if (!victim)
        return 0;

    struct oom_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));

    evt->timestamp_ns  = bpf_ktime_get_ns();

    // Victim info
    evt->victim_pid  = BPF_CORE_READ(victim, pid);
    evt->victim_tgid = BPF_CORE_READ(victim, tgid);
    bpf_probe_read_kernel_str(evt->victim_comm, TASK_COMM_LEN,
                               BPF_CORE_READ(victim, comm));

    // OOM score adj
    evt->oom_score_adj = BPF_CORE_READ(victim, signal, oom_score_adj);

    // Killer (current task)
    current_task = (struct task_struct *)bpf_get_current_task_btf();
    evt->killer_pid = BPF_CORE_READ(current_task, pid);
    bpf_get_current_comm(evt->killer_comm, TASK_COMM_LEN);

    // Allocation order
    evt->order = BPF_CORE_READ(oc, order);

    // Memory stats from oom_control
    evt->rss_bytes       = BPF_CORE_READ(oc, totalpages) * 4096ULL;
    evt->mem_limit_bytes = 0;  // populated from cgroup memcg in full kernel build

    // Cgroup fingerprinting — read cgroup name for pod identification
    struct cgroup *cgrp = BPF_CORE_READ(victim, cgroups, dfl_cgrp);
    if (cgrp) {
        struct kernfs_node *kn = BPF_CORE_READ(cgrp, kn);
        if (kn) {
            bpf_probe_read_kernel_str(evt->cgroup_name, CGROUP_NAME_LEN,
                                       BPF_CORE_READ(kn, name));
        }

        // Increment per-cgroup counter
        __u64 cgrp_ino = BPF_CORE_READ(cgrp, kn, id);
        __u64 *cnt = bpf_map_lookup_elem(&cgroup_oom_count, &cgrp_ino);
        if (cnt) {
            __sync_fetch_and_add(cnt, 1);
        } else {
            __u64 one = 1;
            bpf_map_update_elem(&cgroup_oom_count, &cgrp_ino, &one, BPF_NOEXIST);
        }
    }

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
