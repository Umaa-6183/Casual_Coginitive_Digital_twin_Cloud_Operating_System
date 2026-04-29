// ─── CCDT Shared Types ────────────────────────────────────────────────────────
import React from "react";
export type NodeStatus     = 'healthy' | 'warning' | 'critical';
export type NodeLayer      = 'network' | 'service' | 'data' | 'system';
export type Severity       = 'critical' | 'warning' | 'info';
export type IncidentType   = 'attack' | 'fault';
export type IncidentStatus = 'active' | 'investigating' | 'auto-resolved' | 'resolved';
export type EdgeType       = 'http' | 'grpc' | 'tcp' | 'probe' | 'kafka';
export type EBPFEventType  = 'syscall' | 'oom' | 'tcp' | 'sched' | 'file' | 'capability' | 'probe';
export type AutonomyMode   = 'human-in-loop' | 'supervised' | 'full-auto';

export interface ServiceNode {
  id:          string;
  label:       string;
  x:           number;
  y:           number;
  status:      NodeStatus;
  layer:       NodeLayer;
  cpu:         number;
  mem:         number;
  namespace?:  string;
  nodeName?:   string;
  restarts?:   number;
}

export interface ServiceEdge {
  from:         string;
  to:           string;
  type:         EdgeType;
  causal?:      boolean;
  latencyMs?:   number;
  errorRate?:   number;
  requestRate?: number;
}

export interface Alert {
  id:       number | string;
  time:     string;
  severity: Severity;
  msg:      string;
  node:     string;
  type:     IncidentType;
}

export interface TimelineEvent {
  time:   string;
  event:  string;
  icon:   React.ReactNode;
}

export interface Incident {
  id:          string;
  title:       string;
  severity:    Severity;
  status:      IncidentStatus;
  type:        IncidentType;
  opened:      string;
  elapsed:     string;
  mttrTarget:  string;
  node:        string;
  rootCause:   string;
  affected:    string[];
  confidence:  number;
  autoAction:  string;
  timeline:    TimelineEvent[];
}

export interface EBPFEvent {
  id:       number;
  ts:       string;
  type:     EBPFEventType;
  pod:      string;
  node:     string;
  detail:   string;
  severity: Severity;
}

export interface EBPFProbe {
  name:        string;
  status:      'active' | 'inactive' | 'error';
  events:      number;
  overheadPct: string;
}

export interface CausalChainItem {
  node:        string;
  causalScore: number;
  status:      string;
}

export interface GNNInference {
  nodeClassifications: Record<string, Record<string, number>>;
  graphClassification: Record<string, number>;
  rootCauseNode:       string;
  rootCauseConfidence: number;
  incidentType:        string;
  blastRadius:         string[];
  causalChain:         CausalChainItem[];
  inferenceMs:         number;
}

export interface OPAPolicy {
  id:          string;
  name:        string;
  status:      'active' | 'disabled';
  violations:  number;
  description: string;
}

export interface RLAction {
  id:         number;
  action:     string;
  confidence: number;
  risk:       'LOW' | 'MED' | 'HIGH';
  impact:     string;
  actionName: string;
  targetNode: string;
}

export interface GhostAction {
  label:       string;
  icon:        React.ReactNode;
  actionName:  string;
  targetNode:  string;
  parameters?: Record<string, unknown>;
}

export interface SimulationResult {
  mttrImpactPct:      number;
  trafficImpactPct:   number;
  collateralServices: string[];
  riskScore:          number;
  confidence:         number;
  opaViolations:      string[];
  projectedStatus:    string;
  recommendation:     string;
  simDurationMs:      number;
  opaStatus:          'PASS' | 'FAIL';
}

export interface ChatMessage {
  role:    'user' | 'assistant';
  content: string;
  ts?:     number;
}

export interface KPI {
  label: string;
  value: string;
  sub:   string;
  color: string;
  phase: string;
}

export interface PhaseStatus {
  phase:  string;
  pct:    number;
  status: 'COMPLETE' | 'ACTIVE' | 'ONGOING' | 'PLANNED';
}

export interface MetricPoint {
  time:  string;
  value: number;
}

export interface AppSettings {
  autonomyMode:           AutonomyMode;
  ebpfSampleRate:         number;
  gnnConfidenceThreshold: number;
  alertCooldown:          number;
  ghostPreviewRequired:   boolean;
  opaEnforce:             boolean;
  llmStreaming:           boolean;
  notifsCritical:         boolean;
  notifsWarning:          boolean;
  notifsInfo:             boolean;
  mttrTarget:             number;
  logRetentionDays:       number;
  replicaLimit:           number;
}

export interface Notification {
  id:      number;
  type:    'alert' | 'action' | 'resolved' | 'policy' | 'info';
  msg:     string;
  time:    string;
  read:    boolean;
  source:  string;
}
