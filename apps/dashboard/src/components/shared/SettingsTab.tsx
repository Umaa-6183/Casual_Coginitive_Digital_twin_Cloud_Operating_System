import React from 'react';
import { useSettingsStore } from '@/stores/useSettingsStore';
import type { AutonomyMode } from '@/types';

const S = {
  root:    { height: '100%', overflowY: 'auto' as const, padding: 24 },
  section: {
    background: '#06111F', border: '1px solid #0D2244',
    borderRadius: 12, padding: 20, marginBottom: 16,
  },
  heading: { fontSize: 13, fontWeight: 600, color: '#00D4FF', marginBottom: 16, letterSpacing: 0.5, textTransform: 'uppercase' as const },
  row:     { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 },
  label:   { fontSize: 13, color: '#C8D8E8' },
  sub:     { fontSize: 11, color: '#4A6A8A', marginTop: 2 },
  toggle:  (on: boolean): React.CSSProperties => ({
    width: 42, height: 22, borderRadius: 11, cursor: 'pointer', border: 'none',
    background: on ? '#00D4FF' : '#0D2244', position: 'relative', transition: 'background 0.2s',
    flexShrink: 0,
  }),
  thumb:   (on: boolean): React.CSSProperties => ({
    position: 'absolute', top: 3, left: on ? 22 : 3, width: 16, height: 16,
    borderRadius: '50%', background: on ? '#030810' : '#4A6A8A', transition: 'left 0.2s',
  }),
} as const;

function Toggle({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button onClick={() => onChange(!on)} style={S.toggle(on)} aria-label="toggle">
      <div style={S.thumb(on)} />
    </button>
  );
}

function Slider({
  value, min, max, onChange, unit,
}: { value: number; min: number; max: number; onChange: (v: number) => void; unit?: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <input
        type="range"
        min={min}
        max={max}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ width: 120, accentColor: "#00D4FF" }}
        title="Adjust value"
      />
      <span
        style={{
          fontSize: 12,
          color: "#00D4FF",
          fontFamily: "JetBrains Mono, monospace",
          minWidth: 40,
        }}
      >
        {value}
        {unit}
      </span>
    </div>
  );
}

const MODES: { id: AutonomyMode; label: string; desc: string; color: string }[] = [
  { id: 'human-in-loop', label: 'Human-in-Loop',  desc: 'All actions require SRE approval',  color: '#00D4FF' },
  { id: 'supervised',    label: 'Supervised',      desc: 'Low-risk actions run automatically', color: '#9B5DE5' },
  { id: 'full-auto',     label: 'Full-Auto (L4)',  desc: 'Full autonomous remediation',        color: '#FF3B5C' },
];

const SYSINFO = [
  ['CCDT Version',     '1.0.0'],
  ['GNN Model',        'CausalGNN v2 (GAT+Causal)'],
  ['RL Agent',         'PPO — Stable-Baselines3'],
  ['eBPF Runtime',     'libbpf 1.4 / CO-RE'],
  ['LLM',              'claude-sonnet-4-20250514'],
  ['OPA Version',      '0.65.0'],
  ['Kafka',            'Confluent 7.6'],
  ['VictoriaMetrics',  '1.99.0'],
];

export const SettingsTab: React.FC = () => {
  const { settings, update } = useSettingsStore();

  return (
    <div style={S.root}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>

        {/* Autonomy Mode */}
        <div style={{ ...S.section, gridColumn: '1 / -1' }}>
          <div style={S.heading}>Autonomy Mode</div>
          <div style={{ display: 'flex', gap: 12 }}>
            {MODES.map(m => (
              <button
                key={m.id}
                onClick={() => update({ autonomyMode: m.id })}
                style={{
                  flex: 1, padding: 16, borderRadius: 10, cursor: 'pointer',
                  border: settings.autonomyMode === m.id ? `2px solid ${m.color}` : '1px solid #0D2244',
                  background: settings.autonomyMode === m.id ? `${m.color}15` : '#040C1A',
                  textAlign: 'left', transition: 'all 0.2s',
                }}
              >
                <div style={{ fontSize: 13, fontWeight: 600, color: settings.autonomyMode === m.id ? m.color : '#8899AA', marginBottom: 4 }}>
                  {m.label}
                </div>
                <div style={{ fontSize: 11, color: '#4A6A8A' }}>{m.desc}</div>
              </button>
            ))}
          </div>
        </div>

        {/* Safety Controls */}
        <div style={S.section}>
          <div style={S.heading}>Safety Controls</div>
          {[
            { key: 'ghostPreviewRequired', label: 'Ghost Preview Required',    sub: 'Simulate before every action' },
            { key: 'opaEnforce',           label: 'OPA Hard Enforcement',       sub: 'Block OPA-violating actions' },
            { key: 'llmStreaming',          label: 'LLM Streaming',              sub: 'Stream Co-Pilot responses' },
          ].map(item => (
            <div key={item.key} style={S.row}>
              <div>
                <div style={S.label}>{item.label}</div>
                <div style={S.sub}>{item.sub}</div>
              </div>
              <Toggle
                on={settings[item.key as keyof typeof settings] as boolean}
                onChange={v => update({ [item.key]: v })}
              />
            </div>
          ))}
        </div>

        {/* Alert Levels */}
        <div style={S.section}>
          <div style={S.heading}>Alert Notifications</div>
          {[
            { key: 'notifsCritical', label: 'Critical Alerts', color: '#FF3B5C' },
            { key: 'notifsWarning',  label: 'Warning Alerts',  color: '#FFB800' },
            { key: 'notifsInfo',     label: 'Info Events',     color: '#00D4FF' },
          ].map(item => (
            <div key={item.key} style={S.row}>
              <div style={{ ...S.label, color: item.color }}>{item.label}</div>
              <Toggle
                on={settings[item.key as keyof typeof settings] as boolean}
                onChange={v => update({ [item.key]: v })}
              />
            </div>
          ))}
        </div>

        {/* Thresholds */}
        <div style={S.section}>
          <div style={S.heading}>Thresholds</div>
          {[
            { key: 'ebpfSampleRate',         label: 'eBPF Sample Rate',         min: 10,  max: 100, unit: '%' },
            { key: 'gnnConfidenceThreshold',  label: 'GNN Confidence Threshold', min: 50,  max: 99,  unit: '%' },
            { key: 'mttrTarget',              label: 'MTTR Target',              min: 5,   max: 60,  unit: 'm' },
            { key: 'logRetentionDays',        label: 'Log Retention',            min: 7,   max: 90,  unit: 'd' },
            { key: 'replicaLimit',            label: 'Replica Limit',            min: 1,   max: 20,  unit: ''  },
          ].map(item => (
            <div key={item.key} style={{ ...S.row, marginBottom: 10 }}>
              <div style={S.label}>{item.label}</div>
              <Slider
                value={settings[item.key as keyof typeof settings] as number}
                min={item.min} max={item.max} unit={item.unit}
                onChange={v => update({ [item.key]: v })}
              />
            </div>
          ))}
        </div>

        {/* System Info */}
        <div style={S.section}>
          <div style={S.heading}>System Information</div>
          {SYSINFO.map(([k, v]) => (
            <div key={k} style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
              <span style={{ fontSize: 12, color: '#4A6A8A' }}>{k}</span>
              <span style={{ fontSize: 12, color: '#00D4FF', fontFamily: 'JetBrains Mono, monospace' }}>{v}</span>
            </div>
          ))}
        </div>

      </div>
    </div>
  );
};

export default SettingsTab;
