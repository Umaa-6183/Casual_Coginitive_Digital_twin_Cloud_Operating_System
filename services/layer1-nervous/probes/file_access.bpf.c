// SPDX-License-Identifier: GPL-2.0
// CCDT Layer-1 Nervous System — File Access Probe
//
// Uses the BPF LSM hook (lsm/file_open) to intercept file open calls.
// Monitors access to sensitive paths:
//   /etc/shadow, /etc/passwd    — credential files
//   /proc/*/mem, /proc/sysrq-trigger, /proc/kcore — process memory / kernel
//   /var/run/docker.sock        — Docker socket escape
//   /run/containerd/*           — container runtime sockets
//
// Non-blocking: always returns 0 (allow) — pure observation mode.
// Blocking policy is enforced by OPA in Layer-3.
//
// Compile:
//   clang -g -O2 -target bpf -D__TARGET_ARCH_x86 \
//         -I./include -c file_access.bpf.c -o file_access.bpf.o

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

// ─── Constants ────────────────────────────────────────────────────────────────
#define TASK_COMM_LEN   16
#define PATH_LEN        256
#define DNAME_LEN       32

#define SEV_INFO        0
#define SEV_WARNING     1
#define SEV_CRITICAL    2

// ─── Event struct ─────────────────────────────────────────────────────────────
struct file_event {
    __u64 timestamp_ns;
    __u64 uid_gid;
    __u32 pid;
    __u32 tgid;
    __u32 ppid;
    __u32 inode;                // file inode number
    __u8  comm[TASK_COMM_LEN];
    __u8  parent_comm[TASK_COMM_LEN];
    __u8  severity;
    __u8  flags;                // O_RDONLY=0, O_WRONLY=1, O_RDWR=2
    __u8  pad[2];
    __u8  filename[PATH_LEN];   // dentry name (last component)
    __u8  full_path[PATH_LEN];  // best-effort parent path prefix
};

// ─── Maps ─────────────────────────────────────────────────────────────────────

struct {
    __uint(type,        BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 4 * 1024 * 1024);   // 4 MB
} events SEC(".maps");

// Inode allowlist: inodes we have pre-approved (populated from user-space)
struct {
    __uint(type,        BPF_MAP_TYPE_HASH);
    __uint(max_entries, 8192);
    __type(key,   __u32);   // inode
    __type(value, __u8);    // 1 = allowed
} inode_allowlist SEC(".maps");

// ─── Sensitive path prefixes ─────────────────────────────────────────────────
// We match by checking the dentry name (last component only, fast check)
// and optionally the parent dentry name.

// Check if two strings match (up to 'n' bytes, fixed for verifier)
static __always_inline int str_eq_16(const char *a, const char *b)
{
    // Compare up to 16 bytes
    #pragma unroll
    for (int i = 0; i < 16; i++) {
        if (a[i] != b[i]) return 0;
        if (a[i] == '\0') return 1;
    }
    return 1;
}

static __always_inline __u8 classify_file(
    const char *name,   // dentry d_name.name  (16-byte check)
    const char *parent  // parent dentry name   (16-byte check)
) {
    // Critical: credential and crypto files
    if (str_eq_16(name, "shadow"))   return SEV_CRITICAL;
    if (str_eq_16(name, "gshadow"))  return SEV_CRITICAL;
    if (str_eq_16(name, "passwd"))   return SEV_WARNING;

    // Critical: kernel / process memory interfaces
    if (str_eq_16(name, "mem"))      return SEV_CRITICAL;  // /proc/<pid>/mem
    if (str_eq_16(name, "kcore"))    return SEV_CRITICAL;
    if (str_eq_16(name, "sysrq-tri"))return SEV_CRITICAL;  // sysrq-trigger (truncated)
    if (str_eq_16(name, "kallsyms")) return SEV_WARNING;

    // Critical: container runtime sockets (socket name "docker.sock", "containerd.sock")
    if (str_eq_16(name, "docker.so")) return SEV_CRITICAL; // docker.sock (truncated)
    if (str_eq_16(name, "container")) return SEV_CRITICAL; // containerd.sock (truncated)

    // Sensitive: private keys
    if (str_eq_16(name, "id_rsa"))   return SEV_CRITICAL;
    if (str_eq_16(name, "id_ecdsa")) return SEV_CRITICAL;
    if (str_eq_16(name, "id_ed2551"))return SEV_CRITICAL;

    // Warning: env files, cloud credential files
    if (str_eq_16(name, ".env"))     return SEV_WARNING;
    if (str_eq_16(name, "credential"))return SEV_WARNING;

    return 0xFF;  // not sensitive — do not emit
}

// ─── LSM hook: file_open ─────────────────────────────────────────────────────
SEC("lsm/file_open")
int BPF_PROG(handle_file_open, struct file *file)
{
    // Get dentry names
    struct dentry *dentry = BPF_CORE_READ(file, f_path.dentry);
    struct dentry *parent = BPF_CORE_READ(dentry, d_parent);

    char name[DNAME_LEN]   = {};
    char pname[DNAME_LEN]  = {};

    bpf_probe_read_kernel_str(name,  DNAME_LEN, BPF_CORE_READ(dentry, d_name.name));
    bpf_probe_read_kernel_str(pname, DNAME_LEN, BPF_CORE_READ(parent, d_name.name));

    __u8 sev = classify_file(name, pname);
    if (sev == 0xFF)
        return 0;   // not a sensitive file — allow silently

    __u32 inode_no = BPF_CORE_READ(dentry, d_inode, i_ino);

    // Check inode allowlist
    __u8 *allowed = bpf_map_lookup_elem(&inode_allowlist, &inode_no);
    if (allowed && *allowed == 1)
        return 0;

    // Reserve and fill event
    struct file_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));

    struct task_struct *task = (struct task_struct *)bpf_get_current_task_btf();

    evt->timestamp_ns = bpf_ktime_get_ns();
    evt->uid_gid      = bpf_get_current_uid_gid();
    evt->severity     = sev;
    evt->inode        = inode_no;
    evt->flags        = (__u8)(BPF_CORE_READ(file, f_flags) & 0x3);

    __u64 pid_tgid = bpf_get_current_pid_tgid();
    evt->pid   = (__u32)pid_tgid;
    evt->tgid  = (__u32)(pid_tgid >> 32);
    evt->ppid  = BPF_CORE_READ(task, real_parent, tgid);

    bpf_get_current_comm(evt->comm, TASK_COMM_LEN);
    bpf_probe_read_kernel_str(evt->parent_comm, TASK_COMM_LEN,
                               BPF_CORE_READ(task, real_parent, comm));

    // Filename = dentry name (last component)
    bpf_probe_read_kernel_str(evt->filename, PATH_LEN,
                               BPF_CORE_READ(dentry, d_name.name));

    // Full path: parent/name (best effort, 2 levels)
    bpf_probe_read_kernel_str(evt->full_path, PATH_LEN, pname);

    bpf_ringbuf_submit(evt, 0);
    return 0;   // non-blocking: always allow
}

char LICENSE[] SEC("license") = "GPL";
