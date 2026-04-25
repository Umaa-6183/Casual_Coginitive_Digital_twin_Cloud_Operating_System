// SPDX-License-Identifier: GPL-2.0
// CCDT Layer-1 Nervous System — Security Syscall Filter Probe
//
// Monitors high-risk syscalls used in container escapes and lateral movement:
//   execve / execveat  — process execution
//   setuid / setgid    — privilege change
//   ptrace             — process inspection / injection
//   mount              — filesystem manipulation
//   pivot_root         — container root change
//   unshare            — namespace isolation bypass
//
// Each hook emits a structured event with process ancestry information to
// the ring buffer. Severity is classified per syscall type.
//
// Compile:
//   clang -g -O2 -target bpf -D__TARGET_ARCH_x86 \
//         -I./include -c syscall.bpf.c -o syscall.bpf.o

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

// ─── Constants ────────────────────────────────────────────────────────────────
#define TASK_COMM_LEN   16
#define PATH_LEN        128
#define ARG_LEN         64

// Severity levels (mirrored in normalizer.go)
#define SEV_INFO        0
#define SEV_WARNING     1
#define SEV_CRITICAL    2

// Syscall type tags
#define SC_EXECVE       1
#define SC_SETUID       2
#define SC_PTRACE       3
#define SC_MOUNT        4
#define SC_PIVOT_ROOT   5
#define SC_UNSHARE      6

// ─── Event struct ─────────────────────────────────────────────────────────────
struct syscall_event {
    __u64 timestamp_ns;
    __u64 uid_gid;              // packed: uid(32) | gid(32)
    __u32 pid;
    __u32 tgid;
    __u32 ppid;                 // parent PID
    __u8  comm[TASK_COMM_LEN];
    __u8  parent_comm[TASK_COMM_LEN];
    __u8  syscall_type;         // SC_* constant
    __u8  severity;             // SEV_* constant
    __u8  pad[2];
    // syscall-specific payload: path for execve/mount, target pid for ptrace
    __u8  path[PATH_LEN];
    __s64 arg0;                 // first syscall argument (generic)
};

// ─── Maps ─────────────────────────────────────────────────────────────────────

struct {
    __uint(type,        BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 16 * 1024 * 1024);
} events SEC(".maps");

// Per-PID allowlist: pids we have already decided are known-safe
// (populated from user-space — 0 = not in list, 1 = allowlisted)
struct {
    __uint(type,        BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key,   __u32);
    __type(value, __u8);
} pid_allowlist SEC(".maps");

// ─── Helper: fill common fields ───────────────────────────────────────────────
static __always_inline void fill_common(
    struct syscall_event *evt,
    __u8 syscall_type,
    __u8 severity)
{
    struct task_struct *task = (struct task_struct *)bpf_get_current_task_btf();

    evt->timestamp_ns = bpf_ktime_get_ns();
    evt->syscall_type = syscall_type;
    evt->severity     = severity;
    evt->pad[0]       = 0;
    evt->pad[1]       = 0;

    __u64 pid_tgid  = bpf_get_current_pid_tgid();
    evt->pid        = (__u32)pid_tgid;
    evt->tgid       = (__u32)(pid_tgid >> 32);
    evt->uid_gid    = bpf_get_current_uid_gid();
    evt->ppid       = BPF_CORE_READ(task, real_parent, tgid);

    bpf_get_current_comm(evt->comm, TASK_COMM_LEN);
    bpf_probe_read_kernel_str(evt->parent_comm, TASK_COMM_LEN,
                               BPF_CORE_READ(task, real_parent, comm));
}

static __always_inline int is_allowlisted(__u32 pid)
{
    __u8 *v = bpf_map_lookup_elem(&pid_allowlist, &pid);
    return (v && *v == 1) ? 1 : 0;
}

// ─── execve / execveat ───────────────────────────────────────────────────────
SEC("tp/syscalls/sys_enter_execve")
int handle_execve(struct trace_event_raw_sys_enter *ctx)
{
    __u32 pid = bpf_get_current_pid_tgid() & 0xFFFFFFFF;
    if (is_allowlisted(pid))
        return 0;

    struct syscall_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));
    fill_common(evt, SC_EXECVE, SEV_WARNING);

    // arg0 = const char __user *filename
    bpf_probe_read_user_str(evt->path, PATH_LEN, (void *)ctx->args[0]);
    evt->arg0 = ctx->args[0];

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

SEC("tp/syscalls/sys_enter_execveat")
int handle_execveat(struct trace_event_raw_sys_enter *ctx)
{
    __u32 pid = bpf_get_current_pid_tgid() & 0xFFFFFFFF;
    if (is_allowlisted(pid))
        return 0;

    struct syscall_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));
    fill_common(evt, SC_EXECVE, SEV_WARNING);

    // arg1 = const char __user *pathname (execveat)
    bpf_probe_read_user_str(evt->path, PATH_LEN, (void *)ctx->args[1]);
    evt->arg0 = ctx->args[0];

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

// ─── setuid / setgid ─────────────────────────────────────────────────────────
// CRITICAL: dropping to uid=0 or changing uid from non-zero is suspicious
SEC("tp/syscalls/sys_enter_setuid")
int handle_setuid(struct trace_event_raw_sys_enter *ctx)
{
    __u64 ugid    = bpf_get_current_uid_gid();
    __u32 cur_uid = (__u32)ugid;
    __u32 new_uid = (__u32)ctx->args[0];

    // Only alert when: gaining root OR changing from root to another uid (evasion)
    if (cur_uid == new_uid)
        return 0;

    struct syscall_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));
    __u8 sev = (new_uid == 0 || cur_uid == 0) ? SEV_CRITICAL : SEV_WARNING;
    fill_common(evt, SC_SETUID, sev);
    evt->arg0 = new_uid;

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

SEC("tp/syscalls/sys_enter_setgid")
int handle_setgid(struct trace_event_raw_sys_enter *ctx)
{
    struct syscall_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));
    fill_common(evt, SC_SETUID, SEV_WARNING);
    evt->arg0 = ctx->args[0];   // new gid

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

// ─── ptrace ──────────────────────────────────────────────────────────────────
SEC("tp/syscalls/sys_enter_ptrace")
int handle_ptrace(struct trace_event_raw_sys_enter *ctx)
{
    struct syscall_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));
    fill_common(evt, SC_PTRACE, SEV_CRITICAL);

    evt->arg0 = ctx->args[0];   // ptrace request (PTRACE_ATTACH = 16)
    // arg1 = target pid
    __s64 target_pid = ctx->args[1];
    bpf_snprintf((char *)evt->path, PATH_LEN, "target_pid=%d", target_pid);

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

// ─── mount ───────────────────────────────────────────────────────────────────
SEC("tp/syscalls/sys_enter_mount")
int handle_mount(struct trace_event_raw_sys_enter *ctx)
{
    struct syscall_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));
    fill_common(evt, SC_MOUNT, SEV_CRITICAL);

    // arg1 = target directory
    bpf_probe_read_user_str(evt->path, PATH_LEN, (void *)ctx->args[1]);
    evt->arg0 = ctx->args[3];   // mount flags

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

// ─── pivot_root ───────────────────────────────────────────────────────────────
SEC("tp/syscalls/sys_enter_pivot_root")
int handle_pivot_root(struct trace_event_raw_sys_enter *ctx)
{
    struct syscall_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));
    fill_common(evt, SC_PIVOT_ROOT, SEV_CRITICAL);

    // arg0 = new_root path
    bpf_probe_read_user_str(evt->path, PATH_LEN, (void *)ctx->args[0]);

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

// ─── unshare ──────────────────────────────────────────────────────────────────
SEC("tp/syscalls/sys_enter_unshare")
int handle_unshare(struct trace_event_raw_sys_enter *ctx)
{
    struct syscall_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));
    fill_common(evt, SC_UNSHARE, SEV_CRITICAL);

    evt->arg0 = ctx->args[0];   // clone flags (CLONE_NEWUSER=0x10000000 is dangerous)

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
