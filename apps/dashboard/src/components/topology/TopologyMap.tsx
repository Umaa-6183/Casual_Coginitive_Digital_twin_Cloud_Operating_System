import React, { useCallback } from 'react';
import type { ServiceNode, ServiceEdge } from '@/types';

interface Props {
  nodes:         ServiceNode[];
  edges:         ServiceEdge[];
  selectedId?:   string;
  onSelectNode:  (node: ServiceNode) => void;
  width?:        number;
  height?:       number;
}

const STATUS_COLORS = {
  healthy:  '#00FF9F',
  warning:  '#FFB800',
  critical: '#FF3B5C',
};

const LAYER_COLORS = {
  network: '#00D4FF',
  service: '#9B5DE5',
  data:    '#FFB800',
  system:  '#4A8A6A',
};

export const TopologyMap: React.FC<Props> = ({
  nodes,
  edges,
  selectedId,
  onSelectNode,
  width  = 760,
  height = 480,
}) => {
  const nodeMap = new Map(nodes.map(n => [n.id, n]));

  const getEdgeColor = (edge: ServiceEdge) => {
    if (edge.causal) return '#FF3B5C';
    const typeColors: Record<string, string> = {
      http:  '#00D4FF55',
      grpc:  '#9B5DE555',
      tcp:   '#FFB80055',
      kafka: '#00FF9F55',
      probe: '#4A6A8A55',
    };
    return typeColors[edge.type] ?? '#1A3A6A55';
  };

  const renderEdge = (edge: ServiceEdge, idx: number) => {
    const src = nodeMap.get(edge.from);
    const dst = nodeMap.get(edge.to);
    if (!src || !dst) return null;

    // Validate coordinates exist and are valid numbers (not NaN)
    if (typeof src.x !== 'number' || typeof src.y !== 'number' ||
        typeof dst.x !== 'number' || typeof dst.y !== 'number' ||
        !isFinite(src.x) || !isFinite(src.y) ||
        !isFinite(dst.x) || !isFinite(dst.y)) {
      return null;
    }

    // Map from data coords to SVG
    const sx = (src.x / 800) * width;
    const sy = (src.y / 500) * height;
    const dx = (dst.x / 800) * width;
    const dy = (dst.y / 500) * height;

    // Final validation after calculation
    if (!isFinite(sx) || !isFinite(sy) || !isFinite(dx) || !isFinite(dy)) {
      return null;
    }

    // Bezier control point
    const mx = (sx + dx) / 2;
    const my = Math.min(sy, dy) - 30;
    const path = `M ${sx},${sy} Q ${mx},${my} ${dx},${dy}`;
    const color = getEdgeColor(edge);

    return (
      <g key={`edge-${idx}`}>
        {edge.causal && (
          <path d={path} fill="none" stroke="#FF3B5C" strokeWidth={3}
            strokeOpacity={0.15} filter="url(#glow-red)" />
        )}
        <path
          d={path}
          fill="none"
          stroke={color}
          strokeWidth={edge.causal ? 2 : 1}
          strokeDasharray={edge.causal ? undefined : '4 4'}
        />
        {/* Arrow head */}
        <defs>
          <marker id={`arrow-${idx}`} markerWidth="6" markerHeight="6"
            refX="5" refY="3" orient="auto">
            <path d="M0,0 L0,6 L6,3 z" fill={color} />
          </marker>
        </defs>
        <path d={path} fill="none" stroke={color}
          strokeWidth={edge.causal ? 2 : 1}
          markerEnd={`url(#arrow-${idx})`}
          strokeDasharray={edge.causal ? undefined : '4 4'}
        />
        {/* Animated dot on causal edges */}
        {edge.causal && (
          <circle r={3} fill="#FF3B5C">
            <animateMotion dur="2s" repeatCount="indefinite" path={path} />
          </circle>
        )}
      </g>
    );
  };

  const renderNode = useCallback((node: ServiceNode) => {
    // Validate coordinates exist and are valid numbers (not NaN)
    if (typeof node.x !== 'number' || typeof node.y !== 'number' ||
        !isFinite(node.x) || !isFinite(node.y)) {
      return null;
    }

    const x = (node.x / 800) * width;
    const y = (node.y / 500) * height;

    // Final validation after calculation
    if (!isFinite(x) || !isFinite(y)) {
      return null;
    }

    const sc = STATUS_COLORS[node.status] || STATUS_COLORS.healthy;
    const lc = LAYER_COLORS[node.layer] || LAYER_COLORS.system;
    const selected = node.id === selectedId;
    const R = 28;

    return (
      <g
        key={node.id}
        transform={`translate(${x},${y})`}
        onClick={() => onSelectNode(node)}
        style={{ cursor: 'pointer' }}
      >
        {/* Pulse ring for non-healthy */}
        {node.status !== 'healthy' && (
          <circle r={R + 8} fill="none" stroke={sc} strokeWidth={1} strokeOpacity={0.4}>
            <animate attributeName="r" values={`${R+4};${R+14};${R+4}`} dur="2.5s" repeatCount="indefinite" />
            <animate attributeName="stroke-opacity" values="0.5;0;0.5" dur="2.5s" repeatCount="indefinite" />
          </circle>
        )}

        {/* Selected ring */}
        {selected && (
          <circle r={R + 4} fill="none" stroke="#00D4FF" strokeWidth={2}
            strokeDasharray="4 2" strokeOpacity={0.8}>
            <animateTransform attributeName="transform" type="rotate"
              from="0" to="360" dur="6s" repeatCount="indefinite" />
          </circle>
        )}

        {/* Node background */}
        <circle r={R} fill="#06111F" stroke={selected ? '#00D4FF' : sc}
          strokeWidth={selected ? 2 : 1.5}
          filter={`url(#glow-${node.status})`}
        />

        {/* Layer color inner ring */}
        <circle r={R - 6} fill="none" stroke={lc} strokeWidth={1} strokeOpacity={0.4} />

        {/* CPU bar arc */}
        {(() => {
          // Validate CPU value
          const cpuValue = typeof node.cpu === 'number' && isFinite(node.cpu) ? node.cpu : 0;
          const pct    = cpuValue / 100;
          const angle  = pct * 2 * Math.PI;
          const startX = 0;
          const startY = -(R - 2);
          const endX   = Math.sin(angle) * (R - 2);
          const endY   = -Math.cos(angle) * (R - 2);
          const large  = angle > Math.PI ? 1 : 0;

          // CPU-based color (matches MetricBar logic)
          const cpuColor = cpuValue > 85 ? '#FF3B5C' : cpuValue > 65 ? '#FFB800' : '#00FF9F';

          // Final validation
          if (!isFinite(endX) || !isFinite(endY)) return null;

          return (
            <path
              d={`M ${startX},${startY} A ${R-2},${R-2} 0 ${large} 1 ${endX},${endY}`}
              fill="none" stroke={cpuColor} strokeWidth={2.5} strokeOpacity={0.7}
              strokeLinecap="round"
            />
          );
        })()}

        {/* Label */}
        <text y={R + 14} textAnchor="middle" fontSize={9}
          fontFamily="JetBrains Mono, monospace" fill="#C8D8E8">
          {node.label.length > 14 ? node.label.slice(0, 13) + '…' : node.label}
        </text>

        {/* CPU% text inside */}
        <text y={3} textAnchor="middle" fontSize={10}
          fontFamily="JetBrains Mono, monospace"
          fill={sc} fontWeight="bold">
          {typeof node.cpu === 'number' && isFinite(node.cpu) ? Math.round(node.cpu) : 0}%
        </text>
      </g>
    );
  }, [selectedId, onSelectNode, width, height]);

  return (
    <svg
      width="100%"
      height="100%"
      viewBox={`0 0 ${width} ${height}`}
      style={{ display: 'block' }}
    >
      <defs>
        <filter id="glow-healthy">
          <feGaussianBlur stdDeviation="3" result="blur" />
          <feComposite in="SourceGraphic" in2="blur" operator="over" />
        </filter>
        <filter id="glow-warning">
          <feGaussianBlur stdDeviation="4" result="blur" />
          <feFlood floodColor="#FFB800" floodOpacity="0.3" result="color" />
          <feComposite in="color" in2="blur" operator="in" result="shadow" />
          <feComposite in="SourceGraphic" in2="shadow" operator="over" />
        </filter>
        <filter id="glow-critical">
          <feGaussianBlur stdDeviation="5" result="blur" />
          <feFlood floodColor="#FF3B5C" floodOpacity="0.4" result="color" />
          <feComposite in="color" in2="blur" operator="in" result="shadow" />
          <feComposite in="SourceGraphic" in2="shadow" operator="over" />
        </filter>
        <filter id="glow-red">
          <feGaussianBlur stdDeviation="3" result="blur" />
          <feFlood floodColor="#FF3B5C" floodOpacity="0.5" result="color" />
          <feComposite in="color" in2="blur" operator="in" result="shadow" />
          <feComposite in="SourceGraphic" in2="shadow" operator="over" />
        </filter>
      </defs>

      {/* Background grid */}
      <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
        <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#0D2244" strokeWidth="0.5" />
      </pattern>
      <rect width="100%" height="100%" fill="url(#grid)" />

      {/* Edges */}
      <g>{edges.map((e, i) => renderEdge(e, i))}</g>

      {/* Nodes */}
      <g>{nodes.map(n => renderNode(n))}</g>
    </svg>
  );
};

export default TopologyMap;
