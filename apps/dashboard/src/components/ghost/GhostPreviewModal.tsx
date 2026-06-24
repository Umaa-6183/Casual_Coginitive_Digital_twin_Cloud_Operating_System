import React, { useEffect, useState, useCallback } from 'react';
import type { GhostAction, SimulationResult } from '@/types';
import { showToast } from '@/components/shared/Toast';
import { previewAction } from '@/api/client';

interface Props {
  action:  GhostAction;
  onClose: () => void;
}

type Phase = 'init' | 'clone' | 'simulate' | 'validate' | 'done';

const PHASES: { id: Phase; label: string; ms: number }[] = [
  { id: 'init',     label: 'Initialising simulation sandbox',   ms: 600  },
  { id: 'clone',    label: 'Cloning cluster state snapshot',     ms: 900  },
  { id: 'simulate', label: 'Running action in isolated twin',    ms: 1100 },
  { id: 'validate', label: 'Evaluating OPA policies',           ms: 700  },
  { id: 'done',     label: 'Simulation complete',               ms: 0    },
];

const S = {
  overlay: {
    position:       'fixed' as const,
    inset:          0,
    background:     '#000000CC',
    backdropFilter: 'blur(4px)',
    zIndex:         1000,
    display:        'flex',
    alignItems:     'center',
    justifyContent: 'center',
  },
  modal: {
    width:        560,
    maxWidth:     '95vw',
    background:   '#06111F',
    border:       '1px solid #9B5DE544',
    borderRadius: 16,
    overflow:     'hidden',
    boxShadow:    '0 24px 64px #00000088, 0 0 40px #9B5DE522',
  },
  header: {
    background:  'linear-gradient(135deg, #0D1F3A, #150A2A)',
    padding:     '20px 24px',
    borderBottom:'1px solid #0D2244',
  },
  body: { padding: 24 },
  footer: {
    padding:     '16px 24px',
    borderTop:   '1px solid #0D2244',
    display:     'flex',
    gap:         10,
    justifyContent: 'flex-end',
  },
} as const;

export const GhostPreviewModal: React.FC<Props> = ({ action, onClose }) => {
  const [phase,  setPhase]  = useState<Phase>('init');
  const [result, setResult] = useState<SimulationResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Run phases sequentially and fetch preview from backend
  useEffect(() => {
    let cancelled = false;

    async function run() {
      try {
        for (const p of PHASES) {
          if (cancelled) return;

          if (p.id === 'done') {
            // Fetch actual preview from backend
            try {
              const previewResult = await previewAction(
                action.actionName,
                action.targetNode,
                'default',
                action.parameters || {}
              );
              if (!cancelled) {
                setPhase('done');
                setResult(previewResult);
              }
            } catch (err) {
              if (!cancelled) {
                console.error('Ghost Preview API error:', err);
                setError(err instanceof Error ? err.message : 'Preview failed');
                setPhase('done');
              }
            }
            break;
          }

          if (!cancelled) setPhase(p.id);
          await new Promise<void>(res => setTimeout(res, p.ms));
        }
      } catch (err) {
        if (!cancelled) {
          console.error('Ghost Preview error:', err);
          setError(err instanceof Error ? err.message : 'Preview failed');
        }
      }
    }
    run();
    return () => { cancelled = true; };
  }, [action]);

  const phaseIndex = PHASES.findIndex(p => p.id === phase);

  const [executing, setExecuting] = useState(false);

  const handleApprove = useCallback(async () => {
    setExecuting(true);
    try {
      showToast(`Executing action: ${action.label}`, 'info');

      // Execute the action via API
      const { executeAction } = await import('@/api/client');
      const executeResult = await executeAction(
        action.actionName,
        action.targetNode,
        'default',
        action.parameters || {}
      );

      // Show success notification
      showToast(`✅ ${action.label} executed successfully on ${action.targetNode}`, 'success');

      // Log the result for debugging
      console.log('Action execution result:', executeResult);

      // Close modal after short delay to show success toast
      setTimeout(() => {
        onClose();
      }, 1000);
    } catch (err) {
      console.error('Action execution failed:', err);
      showToast(`❌ Failed to execute ${action.label}: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
      setExecuting(false);
    }
  }, [action, onClose]);

  const handleCancel = useCallback(() => {
    onClose();
  }, [onClose]);

  return (
    <div style={S.overlay} onClick={e => e.target === e.currentTarget && onClose()}>
      <div style={S.modal}>

        {/* Header */}
        <div style={S.header}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div>
              <div style={{ fontSize: 11, color: '#9B5DE5', fontFamily: 'JetBrains Mono, monospace', marginBottom: 4, textTransform: 'uppercase', letterSpacing: 1 }}>
                👻 Ghost Preview Simulation
              </div>
              <div style={{ fontSize: 16, fontWeight: 700, color: '#C8D8E8' }}>
                {action.icon} {action.label}
              </div>
              <div style={{ fontSize: 12, color: '#4A6A8A', marginTop: 2 }}>
                Target: <span style={{ color: '#00D4FF', fontFamily: 'JetBrains Mono, monospace' }}>{action.targetNode}</span>
              </div>
            </div>
            <button
              onClick={onClose}
              style={{ background: 'transparent', border: 'none', color: '#4A6A8A', cursor: 'pointer', fontSize: 18, lineHeight: 1 }}
            >
              ✕
            </button>
          </div>
        </div>

        {/* Body */}
        <div style={S.body}>
          {/* Phase stepper */}
          <div style={{ marginBottom: 24 }}>
            {PHASES.filter(p => p.id !== 'done').map((p, i) => {
              const done    = i < phaseIndex;
              const active  = i === phaseIndex && phase !== 'done';
              const pending = i > phaseIndex;
              return (
                <div key={p.id} style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 10 }}>
                  <div style={{
                    width: 24, height: 24, borderRadius: '50%', flexShrink: 0,
                    background:  done ? '#00FF9F22' : active ? '#9B5DE522' : '#0D1F3A',
                    border:      `2px solid ${done ? '#00FF9F' : active ? '#9B5DE5' : '#0D2244'}`,
                    display:     'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize:    done ? 12 : 10,
                    boxShadow:   active ? '0 0 12px #9B5DE566' : 'none',
                  }}>
                    {done ? '✓' : active ? (
                      <div style={{
                        width: 10, height: 10, borderRadius: '50%',
                        border: '2px solid #9B5DE5', borderTopColor: 'transparent',
                        animation: 'spin 0.8s linear infinite',
                      }} />
                    ) : i + 1}
                  </div>
                  <div style={{ fontSize: 12, color: done ? '#00FF9F' : active ? '#C8D8E8' : '#4A6A8A', flex: 1 }}>
                    {p.label}
                  </div>
                  {done && <span style={{ fontSize: 10, color: '#00FF9F', fontFamily: 'JetBrains Mono, monospace' }}>DONE</span>}
                  {active && <span style={{ fontSize: 10, color: '#9B5DE5', fontFamily: 'JetBrains Mono, monospace', animation: 'blink 1s infinite' }}>RUNNING</span>}
                </div>
              );
            })}
          </div>

          {/* Results — shown when done */}
          {phase === 'done' && error && (
            <div style={{
              padding: '10px 14px', borderRadius: 8, marginBottom: 16,
              background: '#FF3B5C11', border: '1px solid #FF3B5C44',
            }}>
              <span style={{ fontSize: 12, fontWeight: 700, color: '#FF3B5C' }}>
                Error: {error}
              </span>
            </div>
          )}
          {result && phase === 'done' && (
            <div style={{ animation: 'fadeSlideIn 0.3s ease' }}>
              {/* OPA status banner */}
              <div style={{
                padding: '10px 14px', borderRadius: 8, marginBottom: 16,
                background: result.opaStatus === 'PASS' ? '#00FF9F11' : '#FF3B5C11',
                border: `1px solid ${result.opaStatus === 'PASS' ? '#00FF9F44' : '#FF3B5C44'}`,
              }}>
                <span style={{ fontSize: 12, fontWeight: 700, color: result.opaStatus === 'PASS' ? '#00FF9F' : '#FF3B5C', fontFamily: 'JetBrains Mono, monospace' }}>
                  OPA {result.opaStatus} — {result.opaStatus === 'PASS' ? `${5 - result.opaViolations.length}/5 policies satisfied` : result.opaViolations[0]}
                </span>
              </div>

              {/* Metrics grid */}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 16 }}>
                {[
                  { label: 'MTTR Impact',     value: `${result.mttrImpactPct > 0 ? '+' : ''}${result.mttrImpactPct}%`,   color: result.mttrImpactPct < 0 ? '#00FF9F' : '#FF3B5C' },
                  { label: 'Traffic Impact',  value: `${result.trafficImpactPct > 0 ? '+' : ''}${result.trafficImpactPct}%`, color: result.trafficImpactPct > 5 ? '#FF3B5C' : '#00D4FF' },
                  { label: 'Risk Score',      value: `${result.riskScore}/100`,  color: result.riskScore < 30 ? '#00FF9F' : result.riskScore < 60 ? '#FFB800' : '#FF3B5C' },
                  { label: 'Confidence',      value: `${(result.confidence * 100).toFixed(0)}%`, color: '#9B5DE5' },
                ].map(m => (
                  <div key={m.label} style={{ background: '#040C1A', border: '1px solid #0D2244', borderRadius: 8, padding: '12px 14px' }}>
                    <div style={{ fontSize: 10, color: '#4A6A8A', marginBottom: 4, textTransform: 'uppercase', letterSpacing: 0.5 }}>{m.label}</div>
                    <div style={{ fontSize: 22, fontWeight: 700, color: m.color, fontFamily: 'JetBrains Mono, monospace' }}>{m.value}</div>
                  </div>
                ))}
              </div>

              {/* Recommendation */}
              <div style={{
                padding: '10px 14px', borderRadius: 8, marginBottom: 4,
                background: '#040C1A', border: '1px solid #0D2244',
              }}>
                <div style={{ fontSize: 10, color: '#4A6A8A', marginBottom: 4, textTransform: 'uppercase', letterSpacing: 0.5 }}>Recommendation</div>
                <div style={{ fontSize: 12, color: '#C8D8E8', lineHeight: 1.5 }}>{result.recommendation}</div>
              </div>

              <div style={{ fontSize: 10, color: '#4A6A8A', textAlign: 'right', fontFamily: 'JetBrains Mono, monospace' }}>
                sim_time={result.simDurationMs.toFixed(0)}ms
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div style={S.footer}>
          <button
            onClick={handleCancel}
            style={{
              background: 'transparent', border: '1px solid #0D2244',
              color: '#8899AA', borderRadius: 8, padding: '8px 20px',
              cursor: 'pointer', fontSize: 13,
            }}
          >
            Cancel
          </button>
          {result && (
            <button
              onClick={handleApprove}
              disabled={result.opaStatus === 'FAIL' || executing}
              style={{
                background: result.opaStatus === 'FAIL' ? '#1A3A6A' : executing ? '#9B5DE5' : '#00D4FF',
                border:     'none',
                color:      result.opaStatus === 'FAIL' ? '#4A6A8A' : '#030810',
                borderRadius: 8, padding: '8px 24px',
                cursor: result.opaStatus === 'FAIL' || executing ? 'not-allowed' : 'pointer',
                fontSize: 13, fontWeight: 700,
                opacity: executing ? 0.8 : 1,
              }}
            >
              {result.opaStatus === 'FAIL' ? 'Blocked by OPA' : executing ? '⏳ Executing...' : 'Approve & Execute'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

export default GhostPreviewModal;
