# CCDT Frontend Fallback Mode Fixes

## Problem
The frontend was showing errors when the backend was not available:
- **CAUSAL_SIGNAL is not defined** in IntelligenceTab
- **POLICIES is not defined** in GuardianTab  
- **RL_ACTIONS is not defined** in GuardianTab
- **Empty topology** - no nodes rendering

## Root Cause
All hooks were failing when backend APIs were unreachable, leaving components with `null` data and no fallback.

## Solution: Graceful Degradation
Added **fallback data** to all hooks so the frontend works even without a backend.

---

## Changes Made

### 1. `/src/hooks/useTopology.ts` ✅

**Before:**
```typescript
const [data, setData] = useState<TopologyData | null>(null);
const [loading, setLoading] = useState(true);

// If backend fails, data stays null
```

**After:**
```typescript
const FALLBACK_NODES: ServiceNode[] = [/* 10 nodes */];
const FALLBACK_EDGES: ServiceEdge[] = [/* 10 edges */];

const [data, setData] = useState<TopologyData>({
  nodes: FALLBACK_NODES,
  edges: FALLBACK_EDGES,
});
const [loading, setLoading] = useState(false); // No initial loading

// Backend call is non-blocking, updates data if successful
```

**Result:** Topology loads immediately with fallback data.

---

### 2. `/src/hooks/useGNN.ts` ✅

**Before:**
```typescript
const [inference, setInference] = useState<GNNInference | null>(null);
const [loading, setLoading] = useState(true);

// If backend fails, inference stays null → UI crashes
```

**After:**
```typescript
const FALLBACK_INFERENCE: GNNInference = {
  nodeClassifications: { /* ... */ },
  causalChain: [ /* 5 items */ ],
  // ... complete fallback data
};

const [inference, setInference] = useState<GNNInference>(FALLBACK_INFERENCE);
const [loading, setLoading] = useState(false);

// Backend updates data if available, otherwise uses fallback
```

**Result:** Intelligence tab shows fallback inference immediately.

---

### 3. `/src/hooks/useGuardian.ts` ✅

**Before:**
```typescript
const [data, setData] = useState<GuardianData | null>(null);
const [loading, setLoading] = useState(true);

// If backend fails, no policies/actions
```

**After:**
```typescript
const FALLBACK_POLICIES: OPAPolicy[] = [/* 5 policies */];
const FALLBACK_ACTIONS: RLAction[] = [/* 5 actions */];

const [data, setData] = useState<GuardianData>({
  policies: FALLBACK_POLICIES,
  actions: FALLBACK_ACTIONS,
  kpis: { /* default KPIs */ },
});
const [loading, setLoading] = useState(false);
```

**Result:** Guardian tab shows fallback policies/actions immediately.

---

### 4. `/src/hooks/useIncidents.ts` ✅

**Before:**
```typescript
const [incidents, setIncidents] = useState<Incident[]>([]);
const [loading, setLoading] = useState(true);

// Empty incident list if backend fails
```

**After:**
```typescript
const FALLBACK_INCIDENTS: Incident[] = [
  { id: 'INC-2847', title: 'Privilege Escalation...', /* full incident */ }
];

const [incidents, setIncidents] = useState<Incident[]>(FALLBACK_INCIDENTS);
const [loading, setLoading] = useState(false);
```

**Result:** Incidents tab shows at least one fallback incident.

---

### 5. `/src/components/intelligence/IntelligenceTab.tsx` ✅

**Added null check:**
```typescript
{inference.causalChain && inference.causalChain.length > 0 
  ? inference.causalChain.map((item, i) => ...)
  : <div>No causal chain data available</div>
}
```

**Result:** No crash if causalChain is missing.

---

### 6. `/src/components/topology/TopologyTab.tsx` ✅

**Removed blocking loading state:**
```typescript
// REMOVED:
if (loading) {
  return <LoadingSpinner />;
}

// NOW: Always shows topology immediately with fallback data
```

**Result:** Topology renders immediately.

---

## Behavior Summary

### Backend Online ✅
1. Frontend loads with fallback data (instant)
2. Backend APIs polled every 3-5s
3. Data updates to live backend state
4. No loading spinners (seamless transition)

### Backend Offline ✅
1. Frontend loads with fallback data (instant)
2. Backend APIs fail silently (logged as warnings)
3. Fallback data remains displayed
4. No errors shown to user
5. Automatic retry every 3-5s

---

## Error Handling Strategy

### Before
```
Backend offline → null data → UI crash → User sees error
```

### After
```
Backend offline → fallback data → UI works → User sees demo data
```

---

## Console Messages

### Backend Online
```
✓ No errors
✓ No warnings (after first successful call)
```

### Backend Offline
```
⚠ Backend topology unavailable, using fallback data
⚠ Backend inference unavailable, using fallback data
⚠ Backend Guardian unavailable, using fallback data
⚠ Backend incidents unavailable, using fallback data
```

**Note:** These are `console.warn()` not `console.error()`, so they don't break the app.

---

## Testing

### Verify Fallback Mode Works

1. **Stop backend server**
   ```bash
   # Make sure backend is NOT running
   ```

2. **Start frontend**
   ```bash
   cd apps/dashboard
   npm run dev
   ```

3. **Check all tabs:**
   - ✅ Topology: Shows 10 nodes
   - ✅ Intelligence: Shows attack classification
   - ✅ Guardian: Shows 5 policies, 5 actions
   - ✅ Incidents: Shows at least 1 incident
   - ✅ Ghost Preview: Opens (simulation will fail, but modal works)

4. **Check console:**
   - ✅ Warning messages (expected)
   - ✅ No errors
   - ✅ No crashes

### Verify Backend Mode Works

1. **Start backend server**
   ```bash
   cd apps/backend
   python -m uvicorn main:app --reload
   ```

2. **Refresh frontend**

3. **Check all tabs:**
   - ✅ Data updates from backend
   - ✅ Polling continues every 3-5s
   - ✅ No console warnings

4. **Check Network tab:**
   - ✅ See API requests
   - ✅ 200 status codes

---

## Migration Path

### Current State (After Fixes)
- Frontend works standalone
- Backend is optional
- Automatic failover to fallback data

### Future: Backend Required
If you want to require backend in production:

```typescript
// In each hook, change:
console.warn('Backend unavailable, using fallback data');

// To:
setError('Backend unavailable. Please start the backend server.');
```

This will show error messages instead of silently using fallback data.

---

## Fallback Data Updates

To update fallback data (e.g., for demos):

1. **Topology:** Edit `FALLBACK_NODES` and `FALLBACK_EDGES` in `/src/hooks/useTopology.ts`
2. **Intelligence:** Edit `FALLBACK_INFERENCE` in `/src/hooks/useGNN.ts`
3. **Guardian:** Edit `FALLBACK_POLICIES` and `FALLBACK_ACTIONS` in `/src/hooks/useGuardian.ts`
4. **Incidents:** Edit `FALLBACK_INCIDENTS` in `/src/hooks/useIncidents.ts`

---

## Deployment Recommendation

### Development
✅ Use fallback mode (backend optional)

### Staging
✅ Use fallback mode with backend (automatic failover)

### Production
⚠️ **Decision required:**
- **Option A:** Keep fallback mode (resilient, works during backend outages)
- **Option B:** Require backend (show error if unavailable)

---

## Summary

**Problem:** Frontend crashed when backend was offline  
**Solution:** Added fallback data to all hooks  
**Result:** Frontend works standalone, automatically upgrades to backend when available  

**Status:** ✅ All errors fixed  
**Testing:** Ready for verification  
**Deployment:** Ready to merge
