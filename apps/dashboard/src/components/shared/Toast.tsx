import React, { useEffect, useState } from 'react';
import type { Severity } from '@/types';

interface ToastProps {
  message:   string;
  severity?: Severity;
  onClose:   () => void;
  duration?: number;
}

const COLORS: Record<Severity, string> = {
  critical: '#FF3B5C',
  warning:  '#FFB800',
  info:     '#00D4FF',
};

export const Toast: React.FC<ToastProps> = ({
  message,
  severity = 'info',
  onClose,
  duration = 3500,
}) => {
  const [visible, setVisible] = useState(true);
  const color = COLORS[severity];

  useEffect(() => {
    const t = setTimeout(() => {
      setVisible(false);
      setTimeout(onClose, 300);
    }, duration);
    return () => clearTimeout(t);
  }, [duration, onClose]);

  return (
    <div
      style={{
        position:     'fixed',
        bottom:       24,
        right:        24,
        zIndex:       9999,
        background:   '#0A1628',
        border:       `1px solid ${color}66`,
        borderLeft:   `3px solid ${color}`,
        borderRadius: 8,
        padding:      '12px 16px',
        color:        '#C8D8E8',
        fontSize:     13,
        fontFamily:   'Inter, sans-serif',
        boxShadow:    `0 4px 24px ${color}33`,
        opacity:      visible ? 1 : 0,
        transform:    visible ? 'translateY(0)' : 'translateY(8px)',
        transition:   'opacity 0.3s, transform 0.3s',
        maxWidth:     360,
      }}
    >
      <span style={{ color, fontWeight: 700, marginRight: 8, textTransform: 'uppercase', fontSize: 11 }}>
        {severity}
      </span>
      {message}
    </div>
  );
};

// ─── Toast Manager ─────────────────────────────────────────────────────────────
interface ToastItem {
  id:       number;
  message:  string;
  severity: Severity;
}

let toastId   = 0;
let addToast_: ((item: ToastItem) => void) | null = null;

export function showToast(message: string, severity: Severity = 'info') {
  if (addToast_) addToast_({ id: ++toastId, message, severity });
}

export const ToastContainer: React.FC = () => {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  useEffect(() => {
    addToast_ = (item) => setToasts(prev => [...prev, item]);
    return () => { addToast_ = null; };
  }, []);

  return (
    <div style={{ position: 'fixed', bottom: 24, right: 24, zIndex: 9999, display: 'flex', flexDirection: 'column', gap: 8 }}>
      {toasts.map(t => (
        <Toast
          key={t.id}
          message={t.message}
          severity={t.severity}
          onClose={() => setToasts(prev => prev.filter(x => x.id !== t.id))}
        />
      ))}
    </div>
  );
};

export default Toast;
