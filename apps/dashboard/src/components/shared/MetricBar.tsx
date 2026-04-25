import React from 'react';

interface MetricBarProps {
  label:   string;
  value:   number;   // 0-100
  color?:  string;
  height?: number;
}

export const MetricBar: React.FC<MetricBarProps> = ({
  label,
  value,
  color,
  height = 4,
}) => {
  const clamped = Math.max(0, Math.min(100, value));
  const barColor =
    color ??
    (clamped > 88 ? '#FF3B5C' : clamped > 65 ? '#FFB800' : '#00FF9F');

  return (
    <div style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 2 }}>
        <span style={{ fontSize: 10, color: '#8899AA', fontFamily: 'JetBrains Mono, monospace' }}>
          {label}
        </span>
        <span style={{ fontSize: 10, color: barColor, fontFamily: 'JetBrains Mono, monospace' }}>
          {clamped}%
        </span>
      </div>
      <div
        style={{
          width: '100%',
          height,
          background: '#0D1F3A',
          borderRadius: height,
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            width:         `${clamped}%`,
            height:        '100%',
            background:    barColor,
            borderRadius:  height,
            boxShadow:     `0 0 6px ${barColor}88`,
            transition:    'width 0.6s ease',
          }}
        />
      </div>
    </div>
  );
};

export default MetricBar;
