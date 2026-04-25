// SPDX-License-Identifier: GPL-2.0
// CCDT Layer-1 Nervous System — Linux Capability Probe
//
// Attaches to cap_capable() kernel function via kprobe.
// Monitors requests for elevated capabilities that are commonly abused in
// container escape scenarios:
//   CAP_SYS_ADMIN     — most dangerous; allows many privileged operations
//   CAP_SYS_PTRACE    — process inspection / memory reading
//   CAP_NET_ADMIN     — network configuration (iptables, interface manipulation)
//   CAP_SYS_MODULE    — kernel module loading
//   CAP_SYS_RAWIO     — raw disk access
//   CAP_SYS_BOOT      — system reboot
//   CAP_SETUID        — arbitrary uid changes
//
// Maintains a per-pid elevated capability tracking map.
// Emits CRITICAL-severity events when dangerous caps are acquired.
//
// Compile:
//   clang -g -O2 -target bpf -D__TARGET_ARCH_x86 \
//         -I./include -c capability.bpf.c -o capability.bpf.o

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

// ─── Constants ────────────────────────────────────────────────────────────────
#define TASK_COMM_LEN   16

// Linux capability numbers (from linux/capability.h)
#define CAP_SETUID      7
#define CAP_NET_ADMIN   12
#define CAP_SYS_RAWIO   17
#define CAP_SYS_PTRACE  19
#define CAP_SYS_ADMIN   21
#define CAP_SYS_BOOT    22
#define CAP_SYS_MODULE  16

#define SEV_WARNING     1
#define SEV_CRITICAL    2

#define MAX_TRACKED_PIDS 4096

// ─── Event struct ─────────────────────────────────────────────────────────────
struct cap_event {
    __u64 timestamp_ns;
    __u64 uid_gid;
    __u32 pid;
    __u32 tgid;
    __u32 ppid;
    __u32 cap;                      // capability number requested
    __s32 audit;                    // audit flag from cap_capable()
    __u8  severity;
    __u8  comm[TASK_COMM_LEN];
    __u8  parent_comm[TASK_COMM_LEN];
    __u8  pad[3];
    // Capability name string (max 20 chars)
    __u8  cap_name[20];
};

// ─── Maps ─────────────────────────────────────────────────────────────────────

struct {
    __uint(type,        BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 1 * 1024 * 1024);    // 1 MB (cap events are rare)
} events SEC(".maps");

// Track PIDs that have acquired elevated capabilities (for blast radius)
// pid → bitmask of acquired caps
struct {
    __uint(type,        BPF_MAP_TYPE_HASH);
    __uint(max_entries, MAX_TRACKED_PIDS);
    __type(key,   __u32);   // pid
    __type(value, __u64);   // bitmask: bit N set = CAP_N acquired
} elevated_pids SEC(".maps");

// ─── Helper: is this a dangerous capability? ──────────────────────────────────
static __always_inline __u8 cap_severity(__u32 cap)
{
    // Critical: these directly enable container escape or full system compromise
    if (cap == CAP_SYS_ADMIN)   return SEV_CRITICAL;
    if (cap == CAP_SYS_PTRACE)  return SEV_CRITICAL;
    if (cap == CAP_SYS_MODULE)  return SEV_CRITICAL;
    if (cap == CAP_SYS_RAWIO)   return SEV_CRITICAL;
    if (cap == CAP_SYS_BOOT)    return SEV_CRITICAL;

    // Warning: elevated but more commonly used in legitimate scenarios
    if (cap == CAP_NET_ADMIN)   return SEV_WARNING;
    if (cap == CAP_SETUID)      return SEV_WARNING;

    return 0;   // not interesting
}

// ─── Helper: cap number → name string ────────────────────────────────────────
static __always_inline void cap_to_name(__u32 cap, __u8 *buf)
{
    // Manually map the 7 monitored caps to name strings
    // (verifier-safe: no loops, no dynamic dispatch)
    if (cap == CAP_SYS_ADMIN)  { __builtin_memcpy(buf, "CAP_SYS_ADMIN\0      ", 20); return; }
    if (cap == CAP_SYS_PTRACE) { __builtin_memcpy(buf, "CAP_SYS_PTRACE\0     ", 20); return; }
    if (cap == CAP_SYS_MODULE) { __builtin_memcpy(buf, "CAP_SYS_MODULE\0     ", 20); return; }
    if (cap == CAP_SYS_RAWIO)  { __builtin_memcpy(buf, "CAP_SYS_RAWIO\0      ", 20); return; }
    if (cap == CAP_SYS_BOOT)   { __builtin_memcpy(buf, "CAP_SYS_BOOT\0       ", 20); return; }
    if (cap == CAP_NET_ADMIN)  { __builtin_memcpy(buf, "CAP_NET_ADMIN\0      ", 20); return; }
    if (cap == CAP_SETUID)     { __builtin_memcpy(buf, "CAP_SETUID\0         ", 20); return; }
    __builtin_memcpy(buf, "CAP_UNKNOWN\0        ", 20);
}

// ─── kprobe: cap_capable(const struct cred *cred, struct user_namespace *tns,
//                          int cap, unsigned int opts) ─────────────────────────
SEC("kprobe/cap_capable")
int BPF_KPROBE(handle_cap_capable,
               const struct cred *cred,
               struct user_namespace *tns,
               int cap,
               unsigned int opts)
{
    __u8 sev = cap_severity((__u32)cap);
    if (!sev)
        return 0;

    struct task_struct *task = (struct task_struct *)bpf_get_current_task_btf();

    __u64 pid_tgid = bpf_get_current_pid_tgid();
    __u32 pid      = (__u32)pid_tgid;

    // Track this pid as having acquired an elevated capability
    __u64 *mask = bpf_map_lookup_elem(&elevated_pids, &pid);
    if (mask) {
        *mask |= (1ULL << (cap & 63));
    } else {
        __u64 m = (1ULL << (cap & 63));
        bpf_map_update_elem(&elevated_pids, &pid, &m, BPF_ANY);
    }

    // Emit to ring buffer
    struct cap_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));

    evt->timestamp_ns = bpf_ktime_get_ns();
    evt->uid_gid      = bpf_get_current_uid_gid();
    evt->pid          = pid;
    evt->tgid         = (__u32)(pid_tgid >> 32);
    evt->ppid         = BPF_CORE_READ(task, real_parent, tgid);
    evt->cap          = (__u32)cap;
    evt->audit        = (opts & 1);   // SECURITY_CAP_AUDIT = 1
    evt->severity     = sev;

    bpf_get_current_comm(evt->comm, TASK_COMM_LEN);
    bpf_probe_read_kernel_str(evt->parent_comm, TASK_COMM_LEN,
                               BPF_CORE_READ(task, real_parent, comm));

    cap_to_name((__u32)cap, evt->cap_name);

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
