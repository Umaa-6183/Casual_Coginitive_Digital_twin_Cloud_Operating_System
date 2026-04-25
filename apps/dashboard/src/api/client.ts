import axios from 'axios';
import type {
  GNNInference,
  SimulationResult,
  Incident,
  EBPFEvent,
  EBPFProbe,
  OPAPolicy,
  RLAction,
  ChatMessage,
} from '@/types';

// Base URL — proxied through Vite dev server to http://localhost:8000
const BASE = '/api/v1';

const api = axios.create({
  baseURL: BASE,
  timeout: 15_000,
  headers: { 'Content-Type': 'application/json' },
});

// ─── Topology ──────────────────────────────────────────────────────────────────
export const fetchTopology = () =>
  api.get<{ nodes: unknown[]; edges: unknown[] }>('/topology').then(r => r.data);

// ─── GNN Inference ────────────────────────────────────────────────────────────
export const fetchInference = () =>
  api.post<GNNInference>('/infer').then(r => r.data);

export const fetchCounterfactual = (targetNode: string, action: string) =>
  api.post('/counterfactual', { target_node: targetNode, action }).then(r => r.data);

// ─── Guardian ─────────────────────────────────────────────────────────────────
export const fetchGuardianPolicies = () =>
  api.get<{ policies: OPAPolicy[] }>('/guardian/policies').then(r => r.data);

export const fetchGuardianActions = () =>
  api.get<{ actions: RLAction[] }>('/guardian/actions').then(r => r.data);

export const previewAction = (
  actionName: string,
  targetNode: string,
  namespace = 'default',
  parameters: Record<string, unknown> = {},
) =>
  api
    .post<SimulationResult>('/actions/preview', {
      action_name: actionName,
      target_node: targetNode,
      namespace,
      parameters,
    })
    .then(r => r.data);

export const executeAction = (
  actionName: string,
  targetNode: string,
  namespace = 'default',
  parameters: Record<string, unknown> = {},
) =>
  api
    .post('/actions/execute', {
      action_name: actionName,
      target_node: targetNode,
      namespace,
      parameters,
    })
    .then(r => r.data);

// ─── Incidents ────────────────────────────────────────────────────────────────
export const fetchIncidents = (status?: string) =>
  api
    .get<{ incidents: Incident[]; total: number }>('/incidents', {
      params: status ? { status } : {},
    })
    .then(r => r.data);

export const fetchIncident = (id: string) =>
  api.get<Incident>(`/incidents/${id}`).then(r => r.data);

// ─── eBPF ─────────────────────────────────────────────────────────────────────
export const fetchEBPFEvents = (params?: {
  type?: string;
  severity?: string;
  node?: string;
  limit?: number;
}) =>
  api
    .get<{ events: EBPFEvent[]; total: number }>('/ebpf/events', { params })
    .then(r => r.data);

export const fetchEBPFProbes = () =>
  api.get<{ probes: EBPFProbe[] }>('/ebpf/probes').then(r => r.data);

// ─── Co-Pilot (non-streaming) ─────────────────────────────────────────────────
export const chatSync = (sessionId: string, message: string) =>
  api
    .post<{ reply: string }>('/copilot/chat', {
      session_id: sessionId,
      message,
      stream: false,
    })
    .then(r => r.data);

// ─── Co-Pilot streaming via fetch (SSE) ───────────────────────────────────────
export async function* streamChat(
  sessionId: string,
  message: string,
): AsyncGenerator<string> {
  const resp = await fetch(`${BASE}/copilot/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message, stream: true }),
  });

  if (!resp.ok || !resp.body) {
    throw new Error(`Chat stream failed: ${resp.status}`);
  }

  const reader = resp.body.getReader();
  const dec    = new TextDecoder();

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    const chunk = dec.decode(value, { stream: true });
    // Parse SSE lines: "data: <text>\n\n"
    for (const line of chunk.split('\n')) {
      if (line.startsWith('data: ')) {
        const text = line.slice(6);
        if (text === '[DONE]') return;
        yield text;
      }
    }
  }
}

export default api;
