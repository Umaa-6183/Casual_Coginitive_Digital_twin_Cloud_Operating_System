# Live Updates Fix - Topology & Guardian Tabs

## Problem
The Topology and Guardian tabs were not showing live updates from the backend, making it appear as if the data was static.

## Root Causes

### 1. **Initial Loading State**
Hooks were initialized with `loading: false`, causing the fallback data to appear as the final data without attempting to fetch from the backend.

### 2. **Missing Console Logs**
No visibility into whether backend data was being fetched successfully, making it hard to debug.

### 3. **Silent Failures**
When backend calls succeeded, there was no indication in the console, making it look like polling wasn't working.

## Changes Made

### 1. [useTopology.ts](src/hooks/useTopology.ts)
- Changed initial `loading` state from `false` to `true`
- Added console logging when topology data updates successfully
- Added error logging to track failures
- Shows node counts, status breakdown on each update
- Polls every 3 seconds (already configured)

**Before:**
```typescript
const [loading, setLoading] = useState(false);
```

**After:**
```typescript
const [loading, setLoading] = useState(true);
console.log('Topology data updated:', {
  nodes: validNodes.length,
  edges: topologyData.edges.length,
  critical: validNodes.filter((n: any) => n.status === 'critical').length,
  warning: validNodes.filter((n: any) => n.status === 'warning').length,
});
```

### 2. [useGuardian.ts](src/hooks/useGuardian.ts)
- Changed initial `loading` state from `false` to `true`
- Updated fallback KPIs to match current backend values (46% MTTR, 60% compliance)
- Added console logging when Guardian data updates
- Added error tracking
- Shows policy violations, action counts on update
- Polls every 5 seconds (already configured)

**Before:**
```typescript
const [loading, setLoading] = useState(false);
kpis: {
  mttrReduction: '68%',
  falsePositive: '2.1%',
  opaCompliance: '100%',
  autoResolved: '71%',
}
```

**After:**
```typescript
const [loading, setLoading] = useState(true);
console.log('Guardian data updated:', {
  policies: policiesResp.policies.length,
  actions: actionsResp.actions.length,
  violations: totalPolicies - passedPolicies,
  opaCompliance: opaCompliancePct
});
kpis: {
  mttrReduction: '46%',  // Calculated from backend
  falsePositive: '2.1%',
  opaCompliance: '60%',   // Calculated from backend
  autoResolved: '71%',
}
```

### 3. [useGNN.ts](src/hooks/useGNN.ts)
- Changed initial `loading` state from `false` to `true`
- Added console logging for GNN inference updates
- Shows root cause, confidence, incident type on update
- Polls every 5 seconds (already configured)

## How Live Updates Work

### Data Flow
```
Backend Services (Simulator, Layer2-GNN, Guardian)
    ↓
API Gateway (localhost:8000)
    ↓
Frontend Hooks (useTopology, useGuardian, useGNN)
    ↓
Zustand Stores (useClusterStore)
    ↓
React Components (TopologyTab, GuardianTab)
```

### Update Frequencies
- **Topology**: Polls every 3 seconds
- **Guardian**: Polls every 5 seconds
- **GNN Inference**: Polls every 5 seconds
- **Node Metrics**: Updates every 2 seconds (simulated variation in Layout.tsx)
- **Scenarios**: Changes every 60-90 seconds (simulator)

## Verification

### 1. Check Backend is Live
```bash
# Verify topology is changing
curl -s http://localhost:8000/api/v1/topology | jq '.nodes[] | select(.status != "healthy")'

# Verify guardian data
curl -s http://localhost:8000/api/v1/guardian/policies | jq '.total_violations'
curl -s http://localhost:8000/api/v1/guardian/actions | jq '.actions | length'
```

### 2. Check Browser Console
Open DevTools → Console and watch for:
```
Topology data updated: {nodes: 10, edges: 10, critical: 1, warning: 1}
Guardian data updated: {policies: 5, actions: 5, violations: 2, opaCompliance: 60}
GNN inference updated: {rootCause: "redis", confidence: 0.942, incidentType: "fault"}
```

### 3. Watch Simulator Scenarios
```bash
docker compose logs simulator -f
```

Watch for scenario changes like:
```
🚨 NEW SCENARIO: [FAULT] Redis Cache Eviction Storm
✅ RESOLVED: Kafka Consumer Lag Accumulation
```

### 4. Visual Changes
- **Topology Tab**: 
  - Node colors should change (green → yellow → red)
  - CPU percentages should update
  - Causal edges (red solid lines) should appear/disappear
  - Stats bar numbers should change

- **Guardian Tab**:
  - Policy violations should increase/decrease
  - OPA Compliance percentage should update
  - Remediation actions should change
  - Confidence bars should animate

## Debugging

If updates still don't show:

1. **Check browser console** for fetch errors
2. **Check Network tab** in DevTools → filter by `/api/v1/`
3. **Verify API gateway is running**: `docker compose ps api-gateway`
4. **Check simulator is active**: `docker compose logs simulator --tail 50`
5. **Clear browser cache** and hard reload (Cmd+Shift+R / Ctrl+Shift+F5)

## Backend Data Sources

- **Topology**: Served by `api-gateway` from `services/layer1-nervous/simulator.py`
- **Guardian Policies**: OPA engine at `services/layer3-guardian`
- **Guardian Actions**: RL model recommendations from Guardian service
- **GNN Inference**: Layer2-cognitive service (port 8001)

## Future Improvements

1. Add WebSocket real-time updates (already implemented but needs activation)
2. Show "Last Updated" timestamp on each tab
3. Add connection status indicator
4. Implement exponential backoff for failed requests
5. Add manual refresh button
6. Cache backend responses with SWR strategy
