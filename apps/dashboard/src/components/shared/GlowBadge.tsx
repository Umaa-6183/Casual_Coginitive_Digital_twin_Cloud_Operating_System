import React from 'react';
import type { Severity } from '@/types';

interface GlowBadgeProps {
  severity?: Severity;
  label:     string;
  size?:     'sm' | 'md';
}

const COLORS: Record<Severity, { text: string; glow: string; bg: string }> = {
  critical: { text: '#FF3B5C', glow: '#FF3B5C66', bg: '#FF3B5C22' },
  warning:  { text: '#FFB800', glow: '#FFB80066', bg: '#FFB80022' },
  info:     { text: '#00D4FF', glow: '#00D4FF44', bg: '#00D4FF11' },
};

export const GlowBadge: React.FC<GlowBadgeProps> = ({
  severity = 'info',
  label,
  size = 'sm',
}) => {
  const c  = COLORS[severity] || COLORS.info;
  const fs = size === 'sm' ? 9 : 11;
  const px = size === 'sm' ? '5px 8px' : '4px 12px';

  return (
    <span
      style={{
        display:      'inline-block',
        fontSize:     fs,
        fontFamily:   'JetBrains Mono, monospace',
        fontWeight:   700,
        color:        c.text,
        background:   c.bg,
        border:       `1px solid ${c.text}44`,
        borderRadius: 4,
        padding:      px,
        letterSpacing: 0.5,
        boxShadow:    `0 0 6px ${c.glow}`,
        textTransform: 'uppercase',
        whiteSpace:   'nowrap',
      }}
    >
      {label}
    </span>
  );
};

export default GlowBadge;
