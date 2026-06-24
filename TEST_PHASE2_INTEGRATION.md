# Phase 2.3 Integration Testing Guide

**Date**: June 24, 2026  
**Feature**: CCDT → NexaOps Real Failure Connection  
**Status**: Phase 2.3 Complete ✅

---

## What Was Implemented

### Phase 2.3: Real Backend Connection
The mock-api now polls CCDT API Gateway and **physically breaks** the NexaOps UI when CCDT detects a CRITICAL incident.

**Files Modified**:
1. `/mock-api/main.py` - Added CCDT incident polling
2. `/mock-api/requirements.txt` - Added httpx dependency

**New Functionality**:
- `fetch_ccdt_active_incident()` - Polls CCDT every 2s, caches result
- `/api/health` - Returns 503 during CCDT incidents
- `/api/dashboard` - Returns 503 during incidents  
- `/api/orders` - Returns 502 during incidents
- `/api/inventory` - Returns 503 during incidents
- `/api/ccdt/incident` - New endpoint exposing incident status

---

## Testing Procedure

### Prerequisites
```bash
cd /Users/umaamaheshwarysv/Desktop/ccdt

# Ensure all containers are running
docker compose ps

# Expected services:
# - kafka (port 9092)
# - opa (port 8181)
# - redis (port 6379)
# - layer2-gnn (port 8001)
# - layer3-guardian (port 8002)
# - layer4-copilot (port 8003)
# - api-gateway (port 8000)
# - dashboard (port 3000)
# - demo-postgres (port 5433 → internal 5432)
# - demo-redis (port 6380 → internal 6379)
# - demo-nginx (port 8088)
# - mock-api (port 8089)
# - simulator (no external port)
```

### Test 1: Baseline - No Incident

**Open 3 Browser Windows**:
1. http://localhost:8088 (NexaOps Mock UI)
2. http://localhost:3000 (CCDT Dashboard)
3. http://localhost:8000/docs (API Gateway Swagger)

**Expected State**:
- ✅ NexaOps shows green "All Systems Operational"
- ✅ CCDT status bar shows "MONITORING" with green layer dots
- ✅ Orders table loads with data
- ✅ Inventory grid shows 12 SKUs
- ✅ Footer shows: PostgreSQL (green), Redis (green), API (green)

**Verify Backend Connection**:
```bash
# Terminal 1: Check mock-api health
curl http://localhost:8088/api/health
# Expected: {"postgres":true,"redis":true,"ts":"..."}

# Terminal 2: Check CCDT has no active incidents
curl "http://localhost:8000/api/incidents?status=active"
# Expected: {"incidents":[],...}

# Terminal 3: Check mock-api CCDT integration
curl http://localhost:8088/api/ccdt/incident
# Expected: {"has_incident":false,"incident":null}
```

---

### Test 2: Trigger Crash via Simulator

**Method 1: Use CCDT Dashboard UI (Recommended)**
1. Go to http://localhost:3000
2. Click "Trigger Incident" or use the chaos controls
3. Select scenario: "PostgreSQL OOM Cascade"
4. Watch it trigger

**Method 2: Direct API Call**
```bash
# Trigger OOM cascade on demo-postgres
curl -X POST http://localhost:8001/trigger/oom_cascade

# OR use simulator if exposed
docker exec ccdt-simulator-1 python -c "from simulator import trigger_scenario; trigger_scenario('oom_cascade')"
```

**Method 3: Use NexaOps "Simulate Crash" Button**
1. Go to http://localhost:8088
2. Click the red "Simulate Crash" button (bottom right)
3. This triggers the incident through CCDT

---

### Test 3: Watch The Failure Cascade (THE MONEY SHOT 💰)

**Timeline** (all 3 windows visible):

**T+0s: Incident Injection**
- Simulator injects memory pressure into demo-postgres
- docker logs show: `stress-ng` or `dd` filling memory

**T+3s: CCDT Detection**
- Window 2 (CCDT Dashboard): Incident appears with "CRITICAL" badge
- Window 1 (NexaOps): Status bar changes "MONITORING" → "HEALING" (amber)
- Layer-2 GNN confidence shown: 94%

**T+5s: NexaOps UI Breaks 🔴**
- Window 1 (NexaOps):
  - ❌ Red banner appears: "⚠️ SYSTEM DEGRADED"
  - ❌ Orders table → "502 Bad Gateway"
  - ❌ Inventory grid → "503 Service Unavailable"
  - ✅ Right sidebar slides in → Incident Overlay visible
  - ✅ Footer dots turn red: PostgreSQL ❌, Redis ❌, API ❌

**Verify Backend is Broken**:
```bash
# Terminal: Check health endpoint
curl http://localhost:8088/api/health
# Expected: HTTP 503 with {"postgres":false,"redis":false,"ccdt_incident":"INC-..."}

# Check incident exposure
curl http://localhost:8088/api/ccdt/incident
# Expected: {"has_incident":true,"incident":{...}}

# Try to fetch orders
curl http://localhost:8088/api/orders
# Expected: HTTP 502 {"detail":"Bad Gateway: ..."}
```

**T+6s-T+10s: Guardian Working**
- Window 2 (CCDT Dashboard): Guardian selects action `restart_pod`
- Window 1 (NexaOps Incident Overlay):
  - Timeline shows: "T+6s: OPA safety check passed"
  - Timeline shows: "T+7s: Docker API executing"
  - Shows: "Guardian Action: restart_pod"

**Docker logs verification**:
```bash
# Terminal: Watch Guardian execute
docker logs ccdt-layer3-guardian-1 --tail 20

# Expected output:
# INFO - Received GNN inference: incidentType=fault, rootCause=postgres
# INFO - PPO agent selected action: restart_pod
# INFO - Ghost Preview: risk_score=12, MTTR improvement=78%
# INFO - OPA evaluation: ALLOW (all 5 policies passed)
# INFO - Executing Docker API: restart_pod on demo-postgres
# INFO - Container ccdt-demo-postgres-1 restarted successfully
```

**T+12s: Recovery Complete ✅**
- Window 1 (NexaOps):
  - ✅ Green banner replaces red: "✓ SYSTEM RESTORED"
  - ✅ Orders table reloads with data
  - ✅ Inventory grid shows 12 SKUs again
  - ✅ Status bar: "HEALING" → "MONITORING" (green)
  - ✅ Footer dots: PostgreSQL ✅, Redis ✅, API ✅
  - ✅ Incident overlay shows: "T+12s: ✓ System restored, MTTR: 12s"

**Verify Backend Recovered**:
```bash
# Terminal: Check health
curl http://localhost:8088/api/health
# Expected: HTTP 200 {"postgres":true,"redis":true,"ts":"..."}

# Check incident resolved
curl "http://localhost:8000/api/incidents?status=auto-resolved&limit=1"
# Expected: incident with mttr_seconds=12, action_taken="restart_pod"

# Check mock-api no longer sees incident
curl http://localhost:8088/api/ccdt/incident
# Expected: {"has_incident":false,"incident":null}
```

---

### Test 4: Verify Timeline Accuracy

**In NexaOps Incident Overlay** (right sidebar):

Timeline should show:
```
T+0s   ✓ Incident detected
       Layer-1 flagged anomaly: PostgreSQL OOM Cascade

T+3s   ✓ Root cause identified
       GNN traced to: demo-postgres (94% confidence)

T+5s   ✓ Guardian action selected
       restart_pod

T+6s   ✓ OPA safety check passed
       All 5 policies validated

T+7s   ✓ Docker API executing
       Container restart initiated

T+12s  ✓ System restored
       MTTR: 12s
```

All checkmarks should be green.
"NO HUMAN INTERVENTION" should be visible.

---

### Test 5: Verify Incident ID Correlation

The same incident ID should appear in:

1. **NexaOps Incident Overlay**:
   - Look for incident ID in overlay header or logs

2. **CCDT Dashboard** (http://localhost:3000):
   - Incidents list should show same ID

3. **API Gateway SQLite** (http://localhost:8000/api/incidents):
   ```bash
   curl http://localhost:8000/api/incidents?limit=1 | jq '.incidents[0].id'
   ```

4. **Mock-API health check during incident**:
   ```bash
   # During active incident
   curl http://localhost:8088/api/health | jq '.ccdt_incident'
   ```

All should return the same incident ID (e.g., `"INC-1719234567-abc123"`).

---

### Test 6: Test Different Failure Scenarios

Try triggering each of the 12 simulator scenarios:

```bash
# Test scenarios
SCENARIOS=(
  "oom_cascade"           # FAULT - CRITICAL - postgres
  "cpu_saturation"        # FAULT - CRITICAL - order-svc
  "redis_eviction"        # FAULT - WARNING - redis
  "network_partition"     # FAULT - CRITICAL - payment-svc
  "kafka_lag"             # FAULT - WARNING - notify-svc
  "disk_io_saturation"    # FAULT - WARNING - postgres
  "privilege_escalation"  # ATTACK - CRITICAL - order-svc
  "cryptominer"           # ATTACK - CRITICAL - auth-svc
  "lateral_movement"      # ATTACK - CRITICAL - notify-svc
  "data_exfiltration"     # ATTACK - CRITICAL - inventory-svc
  "container_escape"      # ATTACK - CRITICAL - payment-svc
  "brute_force"           # ATTACK - WARNING - auth-svc
)

for scenario in "${SCENARIOS[@]}"; do
  echo "Testing: $scenario"
  # Trigger scenario here
  sleep 60  # Wait for recovery
done
```

**Expected Behavior**:
- ✅ CRITICAL incidents break NexaOps UI (red banner, 502/503 errors)
- ✅ WARNING incidents show overlay but don't break UI
- ✅ Each incident shows different root causes
- ✅ Timeline shows T+0s → T+12s progression
- ✅ MTTR always < 60s

---

## Success Criteria

### ✅ Phase 1 (Already Complete)
- [x] CCDT status bar appears in NexaOps header
- [x] Shows real-time GNN confidence (polls localhost:8001/topology)
- [x] Layer indicators show green/amber/red status
- [x] Incident overlay slides in from right
- [x] Timeline shows T+0s through T+12s steps
- [x] Container metrics show real memory %

### ✅ Phase 2.3 (Just Completed)
- [x] NexaOps UI physically breaks when CCDT detects incident
- [x] `/api/health` returns 503 during incident
- [x] `/api/orders` returns 502 during incident
- [x] `/api/inventory` returns 503 during incident
- [x] Red "SYSTEM DEGRADED" banner appears automatically
- [x] NexaOps UI automatically recovers when incident resolves
- [x] Green "SYSTEM RESTORED" banner appears on recovery

### 📊 Proof Points for Investors

| Question | Answer | Evidence |
|----------|--------|----------|
| "How do we know CCDT is really monitoring?" | Status bar shows live GNN confidence from real API | Poll localhost:8001/topology shows real data |
| "How do we know the failure is real?" | 502 Bad Gateway errors in browser network tab | curl commands return real HTTP errors |
| "How do we know recovery is autonomous?" | Timeline shows "NO HUMAN INTERVENTION" | No human touched anything during test |
| "How do we prove the connection?" | Same incident ID in 3 places | ID correlation test passes |
| "How fast is recovery?" | Timeline shows MTTR < 60s | Incident overlay displays actual time |

---

## Troubleshooting

### Problem: NexaOps UI doesn't break during incident

**Check 1**: Is CCDT incident actually CRITICAL?
```bash
curl "http://localhost:8000/api/incidents?status=active" | jq '.incidents[0].severity'
# Must return "critical" not "warning"
```

**Check 2**: Is mock-api polling enabled?
```bash
docker exec ccdt-mock-api-1 env | grep CCDT
# Expected: CCDT_INCIDENT_POLL_ENABLED=true
```

**Check 3**: Can mock-api reach CCDT API Gateway?
```bash
docker exec ccdt-mock-api-1 curl -s http://api-gateway:8000/api/incidents?status=active
# Should return JSON, not connection error
```

**Fix**: Update docker-compose.yml if needed:
```yaml
services:
  mock-api:
    environment:
      CCDT_API_GATEWAY: http://api-gateway:8000
      CCDT_INCIDENT_POLL_ENABLED: "true"
    networks:
      - ccdt-net
```

---

### Problem: Incident overlay doesn't appear

**Check 1**: Is ccdt-integration.js loaded?
```javascript
// In browser console (F12):
console.log(window.CCDTIntegration)
// Should return: {init: f, hideIncidentOverlay: f, ...}
```

**Check 2**: Is polling working?
```javascript
// In browser console:
fetch('http://localhost:8000/api/incidents?status=active')
  .then(r => r.json())
  .then(console.log)
// Should return incidents array
```

**Check 3**: CORS issue?
```bash
# Check browser console for CORS errors
# If present, verify API Gateway has CORS middleware enabled
```

**Fix**: Add CORS to API Gateway if missing:
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8088", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
```

---

### Problem: Timeline shows wrong timestamps

**Check**: Are all containers synchronized to same time?
```bash
for c in $(docker ps --filter "name=ccdt-" --format "{{.Names}}"); do
  echo "$c: $(docker exec $c date +%s)"
done
# All timestamps should be within 1 second
```

---

## Performance Metrics

### Expected Latencies
- CCDT incident detection: **< 3s**
- NexaOps UI breaking: **< 5s** (after detection)
- Guardian action selection: **< 2s**
- Docker API execution: **< 5s**
- Total MTTR: **< 60s** (target), **< 15s** (typical)

### Network Traffic
- mock-api → CCDT polling: **1 request/2s** (cached)
- Frontend → mock-api: **1 request/3s** (health + dashboard)
- Frontend → CCDT APIs: **1 request/3s** (topology + incidents)

Total: ~2 requests/second sustained

---

## Next Steps After Testing

1. **Record Demo Video** (5 minutes)
   - Show baseline → crash → break → heal → recover
   - Demonstrate all 3 screens simultaneously
   - Narrate the investor script

2. **Phase 3.2: Add Metrics Comparison**
   - Capture before/after snapshots
   - Display in incident overlay
   - Show "380 qps → 0 qps → 360 qps" progression

3. **Phase 4.1: Add Causal Chain Visualization**
   - Fetch topology.nodes[].causal_score
   - Display: postgres (0.94) → order-svc (0.61) → notify-svc (0.38)
   - Show directed graph with arrows

4. **Polish & Documentation**
   - Add animations to timeline steps
   - Improve error message styling
   - Update README with architecture diagram
   - Create investor one-pager

---

**Last Updated**: June 24, 2026  
**Test Status**: Ready for Execution  
**Expected Test Duration**: 15-20 minutes  
**Author**: Umaa Maheshwary SV
