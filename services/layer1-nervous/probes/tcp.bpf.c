// SPDX-License-Identifier: GPL-2.0
// CCDT Layer-1 Nervous System — TCP Retransmit + RTT Probe
//
// Hooks:
//   kprobe/tcp_retransmit_skb   — per-retransmit events with socket tuple
//   kprobe/tcp_rcv_established  — RTT samples from ACK processing
//   tp/sock/inet_sock_set_state — connection lifecycle (ESTABLISHED → CLOSE_WAIT)
//
// Compile:
//   clang -g -O2 -target bpf -D__TARGET_ARCH_x86 \
//         -I./include -c tcp.bpf.c -o tcp.bpf.o

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_endian.h>

// ─── Constants ────────────────────────────────────────────────────────────────
#define TASK_COMM_LEN   16
#define AF_INET         2
#define AF_INET6        10
#define TCP_ESTABLISHED 1
#define TCP_CLOSE       7
#define TCP_CLOSE_WAIT  8
#define MAX_SOCKETS     65536

// ─── Event types ──────────────────────────────────────────────────────────────
#define EVT_RETRANSMIT  1
#define EVT_RTT_SAMPLE  2
#define EVT_CONN_STATE  3

// ─── Socket 4-tuple key ───────────────────────────────────────────────────────
struct sock_key {
    __u32 saddr;
    __u32 daddr;
    __u16 sport;
    __u16 dport;
    __u16 family;
    __u8  pad[2];
};

// ─── Per-socket retransmit counter ────────────────────────────────────────────
struct sock_stats {
    __u64 retransmits;
    __u64 bytes_retransmitted;
    __u64 last_retransmit_ns;
    __u64 rtt_us;               // last RTT sample (microseconds)
    __u64 rtt_min_us;
    __u64 rtt_max_us;
    __u64 rtt_samples;
};

// ─── Ring buffer event ────────────────────────────────────────────────────────
// Must stay in sync with normalizer.go:TCPEvent
struct tcp_event {
    __u64 timestamp_ns;
    __u64 rtt_us;               // RTT in microseconds (0 for retransmit events)
    __u32 saddr;
    __u32 daddr;
    __u32 pid;
    __u32 retransmits_total;    // cumulative retransmit count for this socket
    __u16 sport;
    __u16 dport;
    __u16 family;
    __u8  event_type;           // EVT_RETRANSMIT | EVT_RTT_SAMPLE | EVT_CONN_STATE
    __u8  new_state;            // TCP state (for EVT_CONN_STATE)
    __u8  comm[TASK_COMM_LEN];
};

// ─── Maps ─────────────────────────────────────────────────────────────────────

struct {
    __uint(type,        BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 16 * 1024 * 1024);   // 16 MB
} events SEC(".maps");

// Per-socket cumulative stats
struct {
    __uint(type,        BPF_MAP_TYPE_LRU_HASH);
    __uint(max_entries, MAX_SOCKETS);
    __type(key,   struct sock_key);
    __type(value, struct sock_stats);
} sock_stats_map SEC(".maps");

// ─── Helper: fill sock_key from struct sock ───────────────────────────────────
static __always_inline int fill_sock_key(struct sock *sk, struct sock_key *key)
{
    __u16 family = BPF_CORE_READ(sk, __sk_common.skc_family);
    if (family != AF_INET && family != AF_INET6)
        return -1;

    key->family = family;
    key->saddr  = BPF_CORE_READ(sk, __sk_common.skc_rcv_saddr);
    key->daddr  = BPF_CORE_READ(sk, __sk_common.skc_daddr);
    key->sport  = bpf_ntohs(BPF_CORE_READ(sk, __sk_common.skc_num));
    key->dport  = bpf_ntohs(BPF_CORE_READ(sk, __sk_common.skc_dport));
    key->pad[0] = 0;
    key->pad[1] = 0;
    return 0;
}

// ─── kprobe: tcp_retransmit_skb ───────────────────────────────────────────────
SEC("kprobe/tcp_retransmit_skb")
int BPF_KPROBE(handle_tcp_retransmit, struct sock *sk, struct sk_buff *skb)
{
    struct sock_key key = {};
    if (fill_sock_key(sk, &key) < 0)
        return 0;

    // Update or create per-socket stats
    struct sock_stats zero = {};
    struct sock_stats *stats = bpf_map_lookup_elem(&sock_stats_map, &key);
    if (!stats) {
        bpf_map_update_elem(&sock_stats_map, &key, &zero, BPF_NOEXIST);
        stats = bpf_map_lookup_elem(&sock_stats_map, &key);
        if (!stats)
            return 0;
    }

    __u64 now = bpf_ktime_get_ns();
    __sync_fetch_and_add(&stats->retransmits, 1);
    stats->last_retransmit_ns = now;

    // Emit event
    struct tcp_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));
    evt->timestamp_ns      = now;
    evt->event_type        = EVT_RETRANSMIT;
    evt->saddr             = key.saddr;
    evt->daddr             = key.daddr;
    evt->sport             = key.sport;
    evt->dport             = key.dport;
    evt->family            = key.family;
    evt->retransmits_total = (__u32)stats->retransmits;
    evt->pid               = bpf_get_current_pid_tgid() >> 32;
    bpf_get_current_comm(evt->comm, TASK_COMM_LEN);

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

// ─── kprobe: tcp_rcv_established — RTT measurement from ACK processing ─────────
SEC("kprobe/tcp_rcv_established")
int BPF_KPROBE(handle_tcp_rcv_established, struct sock *sk)
{
    struct tcp_sock *tp = (struct tcp_sock *)sk;

    // srtt_us is stored as 8x the actual RTT (Linux kernel convention)
    __u32 srtt_us_8x = BPF_CORE_READ(tp, srtt_us);
    if (!srtt_us_8x)
        return 0;

    __u64 rtt_us = srtt_us_8x >> 3;   // divide by 8

    struct sock_key key = {};
    if (fill_sock_key(sk, &key) < 0)
        return 0;

    // Update stats
    struct sock_stats zero = {};
    struct sock_stats *stats = bpf_map_lookup_elem(&sock_stats_map, &key);
    if (!stats) {
        bpf_map_update_elem(&sock_stats_map, &key, &zero, BPF_NOEXIST);
        stats = bpf_map_lookup_elem(&sock_stats_map, &key);
        if (!stats)
            return 0;
    }
    stats->rtt_us     = rtt_us;
    stats->rtt_samples++;
    if (!stats->rtt_min_us || rtt_us < stats->rtt_min_us)
        stats->rtt_min_us = rtt_us;
    if (rtt_us > stats->rtt_max_us)
        stats->rtt_max_us = rtt_us;

    // Only emit to ring buffer if RTT is notably elevated (> 50 ms)
    if (rtt_us < 50000)
        return 0;

    struct tcp_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));
    evt->timestamp_ns = bpf_ktime_get_ns();
    evt->event_type   = EVT_RTT_SAMPLE;
    evt->rtt_us       = rtt_us;
    evt->saddr        = key.saddr;
    evt->daddr        = key.daddr;
    evt->sport        = key.sport;
    evt->dport        = key.dport;
    evt->family       = key.family;
    evt->pid          = bpf_get_current_pid_tgid() >> 32;
    bpf_get_current_comm(evt->comm, TASK_COMM_LEN);

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

// ─── Tracepoint: inet_sock_set_state — connection lifecycle events ─────────────
SEC("tp/sock/inet_sock_set_state")
int handle_sock_state(struct trace_event_raw_inet_sock_set_state *ctx)
{
    // Only track transitions to ESTABLISHED (new connections) or CLOSE_WAIT (termination)
    __u8 new_state = ctx->newstate;
    if (new_state != TCP_ESTABLISHED && new_state != TCP_CLOSE_WAIT)
        return 0;

    struct tcp_event *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    __builtin_memset(evt, 0, sizeof(*evt));
    evt->timestamp_ns = bpf_ktime_get_ns();
    evt->event_type   = EVT_CONN_STATE;
    evt->new_state    = new_state;
    evt->saddr        = ctx->saddr;
    evt->daddr        = ctx->daddr;
    evt->sport        = ctx->sport;
    evt->dport        = ctx->dport;
    evt->family       = ctx->family;
    evt->pid          = bpf_get_current_pid_tgid() >> 32;
    bpf_get_current_comm(evt->comm, TASK_COMM_LEN);

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
