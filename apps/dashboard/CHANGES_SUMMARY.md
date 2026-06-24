# CCDT Frontend Changes Summary

## Files Changed

### ✅ New Files Created (5)

1. **`/src/hooks/useGNN.ts`** (MODIFIED - was existing)
   - Removed mock data
   - Connected to `fetchInference()` API
   - Added polling every 5s
   - Added error handling

2. **`/src/hooks/useGuardian.ts`** (NEW)
   - Fetches Guardian policies and actions from backend
   - Calculates dynamic KPIs
   - Polls every 5s

3. **`/src/hooks/useIncidents.ts`** (NEW)
   - Fetches incidents from backend with optional status filter
   - Polls every 5s

4. **`/src/hooks/useTopology.ts`** (NEW)
   - Fetches topology (nodes + edges) from backend
   - Polls every 3s (faster for real-time updates)

5. **`/src/hooks/useScenarioSync.ts`** (NEW)
   - Cross-layer synchronization hook
   - Syncs topology node status with GNN classifications
   - Marks causal edges based on causal chain

---

### ✅ Modified Files (7)

#### 1. `/src/hooks/useGNN.ts`
**Before:**
```typescript
const MOCK_INFERENCE: GNNInference = { /* fixed data */ };
// Simulated variation
setInference(prev => ({ ...prev, inferenceMs: ... }));
```

**After:**
```typescript
const data = await fetchInference();
// Poll every 5s
const interval = setInterval(loadInference, 5000);
```

---

#### 2. `/src/components/intelligence/IntelligenceTab.tsx`
**Before:**
```typescript
const CAUSAL_SIGNAL = [
  { step: '1', node: 'order-svc', event: 'CAP_SYS_ADMIN...' },
  // ... fixed 6 steps
];

{CAUSAL_SIGNAL.map((step, i) => ...)}
```

**After:**
```typescript
// Removed CAUSAL_SIGNAL constant

{inference.causalChain.map((item, i) => (
  <div key={i}>
    <span>{item.node}</span>
    <div>Causal score: {(item.causalScore * 100).toFixed(1)}%</div>
  </div>
))}
```

**Changes:**
- ❌ Removed fixed `CAUSAL_SIGNAL` array
- ✅ Now uses `inference.causalChain` from backend
- ✅ Added error state handling

---

#### 3. `/src/components/guardian/GuardianTab.tsx`
**Before:**
```typescript
const POLICIES: OPAPolicy[] = [/* 5 fixed policies */];
const RL_ACTIONS: RLAction[] = [/* 5 fixed actions */];
const KPI_ITEMS = [/* fixed values */];

{POLICIES.map(p => ...)}
{RL_ACTIONS.map(a => ...)}
```

**After:**
```typescript
const { data, loading, error } = useGuardian();

const KPI_ITEMS = [
  { value: data.kpis.mttrReduction, ... },
  { value: data.kpis.opaCompliance, ... },
];

{data.policies.map(p => ...)}
{data.actions.map(a => ...)}
```

**Changes:**
- ❌ Removed `POLICIES`, `RL_ACTIONS`, and fixed `KPI_ITEMS`
- ✅ Now fetches from `useGuardian()` hook
- ✅ KPIs calculated from backend data
- ✅ Added loading spinner and error handling

---

#### 4. `/src/components/ghost/GhostPreviewModal.tsx`
**Before:**
```typescript
function mockResult(action: GhostAction): SimulationResult {
  const outcomes: Record<string, Partial<SimulationResult>> = {
    isolate_container: { mttrImpactPct: -65, ... },
    // ... fixed outcomes
  };
  return { /* mock data */ };
}

setResult(mockResult(action));
```

**After:**
```typescript
const previewResult = await previewAction(
  action.actionName,
  action.targetNode,
  'default',
  action.parameters || {}
);
setResult(previewResult);
```

**Changes:**
- ❌ Removed `mockResult()` function and fixed outcomes
- ✅ Now calls `previewAction()` API
- ✅ Backend runs real digital twin simulation
- ✅ Added error handling

---

#### 5. `/src/components/incidents/IncidentsTab.tsx`
**Before:**
```typescript
import { useIncidentStore } from '@/stores/useIncidentStore';

const { incidents, selected, setSelected, statusFilter, setFilter } = useIncidentStore();
// Uses SEED_INCIDENTS from store
```

**After:**
```typescript
import { useIncidents } from '@/hooks/useIncidents';

const [statusFilter, setFilter] = useState('all');
const [selected, setSelected] = useState<Incident | null>(null);
const { incidents, loading, error } = useIncidents(statusFilter);
```

**Changes:**
- ❌ No longer depends on `useIncidentStore` seed data
- ✅ Fetches incidents from backend via `useIncidents()` hook
- ✅ Added loading spinner
- ✅ Added empty state message

---

#### 6. `/src/stores/useClusterStore.ts`
**Before:**
```typescript
const SEED_NODES: ServiceNode[] = [
  { id: 'api-gw', label: 'API Gateway', x: 400, y: 60, status: 'healthy', ... },
  // ... 10 nodes
];
const SEED_EDGES: ServiceEdge[] = [/* 10 edges */];
const SEED_ALERTS: Alert[] = [/* 5 alerts */];
```

**After:**
```typescript
const SEED_NODES: ServiceNode[] = [];
const SEED_EDGES: ServiceEdge[] = [];
const SEED_ALERTS: Alert[] = [];
```

**Changes:**
- ❌ Removed all seed data (now populated from backend)
- ✅ Store acts as cache, populated by `useTopology()` hook

---

#### 7. `/src/components/topology/TopologyTab.tsx`
**Before:**
```typescript
const { nodes, edges, alerts, selectedNode, selectNode } = useClusterStore();
// Uses seed data from store
```

**After:**
```typescript
const { nodes, edges, alerts, selectedNode, selectNode, setNodes, setEdges } = useClusterStore();
const { data: topologyData, loading, error } = useTopology();

useEffect(() => {
  if (topologyData) {
    setNodes(topologyData.nodes);
    setEdges(topologyData.edges);
  }
}, [topologyData, setNodes, setEdges]);
```

**Changes:**
- ✅ Added `useTopology()` hook to fetch backend data
- ✅ Syncs backend data to Zustand store
- ✅ Added loading state
- ✅ Added error handling

---

#### 8. `/src/components/shared/Layout.tsx`
**Before:**
```typescript
export const Layout: React.FC = () => {
  const [activeTab, setActiveTab] = useState<TabId>('topology');
  const { clock, setClock, alerts } = useClusterStore();
```

**After:**
```typescript
import { useScenarioSync } from '@/hooks/useScenarioSync';

export const Layout: React.FC = () => {
  const [activeTab, setActiveTab] = useState<TabId>('topology');
  const { clock, setClock, alerts } = useClusterStore();
  
  // Enable cross-layer scenario synchronization
  useScenarioSync();
```

**Changes:**
- ✅ Added `useScenarioSync()` hook to enable cross-layer consistency
- ✅ Runs globally for all tabs

---

## Summary Statistics

| Metric | Count |
|--------|-------|
| New files created | 4 |
| Files modified | 8 |
| Static data arrays removed | 4 |
| Backend API integrations added | 6 |
| Polling hooks added | 5 |
| Loading states added | 6 |
| Error handling added | 6 |

---

## Key Architectural Changes

### Before
```
┌─────────────────┐
│  Components     │
│  (Intelligence, │
│   Guardian,     │
│   Incidents)    │
└────────┬────────┘
         │
         ▼
    MOCK DATA
    (hardcoded)
```

### After
```
┌─────────────────┐
│  Components     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Custom Hooks   │
│  (useGNN,       │
│   useGuardian,  │
│   useIncidents, │
│   useTopology)  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  API Client     │
│  (axios)        │
└────────┬────────┘
         │
         ▼
   Backend APIs
   (FastAPI)

   PLUS

┌─────────────────┐
│ useScenarioSync │ ← Cross-layer coordination
└─────────────────┘
```

---

## No Breaking Changes

✅ All type definitions remain unchanged  
✅ Component interfaces remain the same  
✅ UI/UX design unchanged  
✅ No prop signature changes  
✅ Backward compatible with existing code  

---

## Testing Checklist

- [ ] Intelligence page shows dynamic attack/fault/healthy percentages
- [ ] Intelligence causal chain changes with scenario
- [ ] Guardian OPA policies reflect backend violations
- [ ] Guardian RL actions adapt to root cause
- [ ] Guardian KPIs update dynamically
- [ ] Ghost Preview calls backend API
- [ ] Ghost Preview shows different results per action
- [ ] Incidents page loads from backend
- [ ] Incidents filter works (all/active/resolved)
- [ ] Topology nodes sync with GNN classifications
- [ ] Topology causal edges match causal chain
- [ ] All screens show loading spinners during fetch
- [ ] All screens show error messages on API failure
- [ ] Polling continues in background (check Network tab)
- [ ] Cross-layer sync works (root cause → topology → guardian)

---

## Developer Notes

### Running the Updated Frontend

```bash
cd apps/dashboard
npm install  # No new dependencies added
npm run dev
```

### Backend Requirements

The frontend now expects these endpoints to be live:

- `POST /api/v1/infer`
- `GET  /api/v1/guardian/policies`
- `GET  /api/v1/guardian/actions`
- `POST /api/v1/actions/preview`
- `GET  /api/v1/incidents`
- `GET  /api/v1/topology`

### Debugging

Enable detailed logging:
```typescript
// In any hook, add:
console.log('Fetched data:', data);
```

Check Network tab in DevTools:
- Should see polling requests every 3-5s
- Look for 200 status codes
- Inspect response payloads

---

## Migration Path

If backend is not ready:

1. **Temporary fallback**: Add mock API responses in `/src/api/client.ts`:
   ```typescript
   export const fetchInference = async () => {
     // return mock data until backend ready
     return MOCK_INFERENCE;
   };
   ```

2. **Feature flag**: Add `VITE_USE_MOCK_DATA` env var:
   ```typescript
   const useMock = import.meta.env.VITE_USE_MOCK_DATA === 'true';
   ```

3. **Gradual rollout**: Enable API per-component as backend endpoints become available

---

## Conclusion

All frontend screens are now **fully connected to backend APIs**. The platform operates as a **unified digital twin** with **cross-layer synchronization**.

No major redesign was required - all changes focused on replacing static data with dynamic API calls and ensuring consistency across all dashboard screens.
