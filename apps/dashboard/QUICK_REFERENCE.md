# CCDT Frontend Quick Reference

## What Changed? (TL;DR)

**Before:** Static mock data everywhere  
**After:** All screens connected to backend APIs

---

## File Locations

### New Hooks (created)
```
src/hooks/
├── useGuardian.ts       → Guardian policies + actions
├── useIncidents.ts      → Incidents list
├── useTopology.ts       → Cluster topology
└── useScenarioSync.ts   → Cross-layer sync
```

### Modified Hooks
```
src/hooks/
└── useGNN.ts            → Now calls backend API
```

### Modified Components
```
src/components/
├── intelligence/IntelligenceTab.tsx  → Dynamic causal chain
├── guardian/GuardianTab.tsx          → Dynamic actions
├── ghost/GhostPreviewModal.tsx       → Real preview API
├── incidents/IncidentsTab.tsx        → Backend incidents
├── topology/TopologyTab.tsx          → Backend topology
└── shared/Layout.tsx                 → Added useScenarioSync()
```

### Modified Stores
```
src/stores/
└── useClusterStore.ts   → Removed seed data
```

---

## API Endpoints Used

| Screen | Endpoint | Method | Poll Interval |
|--------|----------|--------|---------------|
| Intelligence | `/api/v1/infer` | POST | 5s |
| Guardian | `/api/v1/guardian/policies` | GET | 5s |
| Guardian | `/api/v1/guardian/actions` | GET | 5s |
| Ghost Preview | `/api/v1/actions/preview` | POST | On-demand |
| Incidents | `/api/v1/incidents?status={status}` | GET | 5s |
| Topology | `/api/v1/topology` | GET | 3s |

---

## Data Flow

### Intelligence Layer
```
Backend → fetchInference() → useGNN() → IntelligenceTab
                                      ↓
                              useScenarioSync() → Topology sync
```

### Guardian Layer
```
Backend → fetchGuardianPolicies() → useGuardian() → GuardianTab
       → fetchGuardianActions()
```

### Ghost Preview
```
User clicks "Ghost Preview" → previewAction() → Backend simulation → Display results
```

### Incidents
```
Backend → fetchIncidents(status) → useIncidents() → IncidentsTab
```

### Topology
```
Backend → fetchTopology() → useTopology() → useClusterStore → TopologyTab
                                          ↓
                                useScenarioSync() → GNN classifications
```

---

## Key Code Patterns

### 1. Fetch Hook Pattern
```typescript
export function useDataSource() {
  const [data, setData] = useState<DataType | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;

    async function load() {
      try {
        const result = await fetchData();
        if (mounted) {
          setData(result);
          setLoading(false);
        }
      } catch (err) {
        if (mounted) {
          setError(err.message);
          setLoading(false);
        }
      }
    }

    load();
    const interval = setInterval(load, 5000);

    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  return { data, loading, error };
}
```

### 2. Component Usage Pattern
```typescript
export const MyTab: React.FC = () => {
  const { data, loading, error } = useDataSource();

  if (loading || !data) {
    return <LoadingSpinner />;
  }

  return <div>{/* Render data */}</div>;
};
```

### 3. Cross-Layer Sync Pattern
```typescript
// In useScenarioSync.ts
useEffect(() => {
  if (!inference) return;

  // Sync topology with GNN
  const updatedNodes = nodes.map(node => ({
    ...node,
    status: deriveStatusFromClassification(inference.nodeClassifications[node.id])
  }));

  setNodes(updatedNodes);
}, [inference, nodes]);
```

---

## Debugging

### Check if APIs are being called
```javascript
// Chrome DevTools → Network tab
// Filter: XHR
// Look for: /api/v1/infer, /guardian/policies, etc.
// Should see requests every 3-5 seconds
```

### Check hook state
```typescript
const { data, loading, error } = useGuardian();
console.log('Guardian state:', { data, loading, error });
```

### Check scenario sync
```typescript
// In Layout.tsx, add:
useScenarioSync();
console.log('Sync enabled');
```

---

## Common Issues

### "Loading forever"
**Cause:** Backend not running or API unreachable  
**Fix:** Start backend server, check CORS, verify proxy config

### "Error: Failed to fetch"
**Cause:** API endpoint doesn't exist or returns wrong format  
**Fix:** Verify backend implements all required endpoints

### "Screens show different data"
**Cause:** Scenario sync not working  
**Fix:** Ensure `useScenarioSync()` is called in Layout.tsx

### "Old data still showing"
**Cause:** Store still has seed data  
**Fix:** Clear browser cache, verify `SEED_NODES = []` in useClusterStore.ts

---

## Testing Commands

### Manual Test
```bash
# 1. Start backend
cd apps/backend
python -m uvicorn main:app --reload

# 2. Start frontend
cd apps/dashboard
npm run dev

# 3. Open browser
open http://localhost:5173

# 4. Check all tabs:
- Topology: Nodes should load from backend
- Intelligence: Should show dynamic causal chain
- Guardian: Should show policies/actions from backend
- Incidents: Should load incident list
- Click "Ghost Preview": Should call preview API
```

### Check API Responses
```bash
# Test inference
curl -X POST http://localhost:8000/api/v1/infer

# Test Guardian
curl http://localhost:8000/api/v1/guardian/policies
curl http://localhost:8000/api/v1/guardian/actions

# Test incidents
curl http://localhost:8000/api/v1/incidents

# Test topology
curl http://localhost:8000/api/v1/topology
```

---

## Rollback Plan

If you need to revert to mock data:

### Option 1: Git Revert
```bash
git checkout main -- src/hooks/useGNN.ts
git checkout main -- src/components/intelligence/IntelligenceTab.tsx
# etc.
```

### Option 2: Mock Wrapper
In `/src/api/client.ts`:
```typescript
const USE_MOCK = true;

export const fetchInference = async () => {
  if (USE_MOCK) return MOCK_INFERENCE;
  return api.post('/infer').then(r => r.data);
};
```

---

## Performance

### Polling Impact
- **6 endpoints** × **~1 request/5s** = ~1.2 req/s
- Minimal network overhead
- Backend should cache inference results

### Optimization Tips
1. Use WebSockets instead of polling (future enhancement)
2. Add request debouncing
3. Cache responses with SWR or React Query
4. Add conditional requests (If-Modified-Since)

---

## Next Steps

### Immediate
- [ ] Verify all API endpoints work
- [ ] Test scenario switching
- [ ] Check cross-layer sync
- [ ] Test error handling

### Short-term
- [ ] Add WebSocket support
- [ ] Implement scenario selector UI
- [ ] Add real-time alerts
- [ ] Improve loading states

### Long-term
- [ ] Historical data visualization
- [ ] Scenario playback mode
- [ ] Advanced filtering
- [ ] Performance dashboard

---

## Support

**Issues?** Check:
1. Backend logs
2. Browser console (F12)
3. Network tab (API responses)
4. React DevTools (hook state)

**Questions?** Review:
- `/apps/dashboard/FRONTEND_UPDATES.md` - Full documentation
- `/apps/dashboard/CHANGES_SUMMARY.md` - Detailed changes
- `/apps/dashboard/src/api/client.ts` - API definitions

---

**Status:** ✅ All screens connected to backend  
**Testing:** Ready for integration testing  
**Deployment:** Ready to merge
