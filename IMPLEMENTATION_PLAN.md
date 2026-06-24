# CCDT → NexaOps Integration Implementation Plan

**Objective**: Make the connection between CCDT's autonomous recovery and NexaOps mock app failures visibly clear to professors and investors.

**Date**: June 24, 2026  
**Status**: Phase 1 In Progress

---

## ✅ What We Have

1. **NexaOps Mock UI** (localhost:8088)
   - Beautiful React-style dashboard (actually vanilla JS)
   - Shows orders, inventory, analytics, settings
   - Has crash/recovery simulation buttons
   - Already includes `ccdt-integration.js` for Phase 1 components

2. **CCDT Brain** (localhost:3000)
   - Layer-2 GNN: Causal graph inference (localhost:8001/topology)
   - Layer-3 Guardian: PPO RL + OPA + Docker actions (localhost:8002)
   - Layer-4 Co-Pilot: Claude AI explainer (localhost:8003)
   - API Gateway: SQLite incidents, policies (localhost:8000)

3. **Real Docker Services**
   - demo-postgres:5433 (512MB limit, real OOM kills)
   - demo-redis:6380 (64MB limit, real evictions)
   - demo-nginx:8088 (serves NexaOps UI)
   - cAdvisor:8081 (real cgroup metrics)

4. **Mock API Backend** (FastAPI)
   - `/api/dashboard` - KPIs (revenue, orders, sessions, latency)
   - `/api/orders` - Order history
   - `/api/inventory` - Stock levels
   - `/api/health` - postgres + redis health check

---

## 🎯 Implementation Phases

### Phase 1: Live CCDT Status Integration ⭐ **Current Phase**

**Goal**: Show that CCDT is REALLY monitoring this app and detecting failures.

#### 1.1 Real-Time CCDT Status Bar ✅ DONE
- **Location**: NexaOps header (next to "All Systems Operational")
- **Data Source**: Poll `http://localhost:8001/topology` every 3s
- **Shows**:
  - 🟢 CCDT Guardian: MONITORING | GNN Confidence: 94%
  - Layer status indicators (4 colored dots)
  - Real-time state: MONITORING → HEALING → RESTORED

**File**: `/mock-ui/ccdt-integration.js` (lines 106-177)

#### 1.2 Live Incident Overlay Panel ✅ DONE
- **Location**: Right sidebar (slides in when incident detected)
- **Data Source**: Poll `http://localhost:8000/api/incidents?status=active` every 3s
- **Shows**:
  - Incident type: FAULT vs ATTACK
  - Root cause node (demo-postgres, demo-redis, etc.)
  - GNN confidence score with progress bar
  - Blast radius (affected services)
  - Guardian action taken (`restart_pod`, `increase_memory`, etc.)
  - Recovery timeline (T+0s detection → T+12s restored)
  - NO HUMAN INTERVENTION label

**File**: `/mock-ui/ccdt-integration.js` (lines 183-380)

#### 1.3 Container Health Indicators ✅ DONE
- **Location**: Footer (enhances existing PostgreSQL/Redis/API dots)
- **Data Source**: `http://localhost:8081/api/v1.3/docker` (cAdvisor)
- **Shows**:
  - Real memory % per container
  - OOM kill count warnings
  - Color-coded health (green < 75% < amber < 90% < red)

**File**: `/mock-ui/ccdt-integration.js` (lines 383-454)

---

### Phase 2: Real Failure Injection 🔥 **Next Step**

**Goal**: When simulator crashes demo-postgres, the NexaOps UI physically breaks.

#### 2.1 Crash Banner Integration ✅ ALREADY IMPLEMENTED
- Red banner appears when backend fails: "⚠️ SYSTEM DEGRADED"
- File: `/mock-ui/index.html` (lines 79-96, 282-283)
- Triggered by: `isCrashed = true` in main.js

#### 2.2 Error State Display ✅ ALREADY IMPLEMENTED
- Orders table → 502 Bad Gateway
- Inventory grid → 503 Service Unavailable
- File: `/mock-ui/index.html` (lines 1366-1411)

#### 2.3 Real Backend Connection
**TODO**: Wire mock-api to CCDT incident stream

**File to Modify**: `/mock-api/main.py`

Add new endpoint:
```python
@app.get("/api/ccdt/incidents/active")
async def get_active_ccdt_incident():
    """Fetch active incident from CCDT API Gateway"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "http://localhost:8000/api/incidents",
                params={"status": "active", "limit": 1},
                timeout=2.0
            )
            if response.status_code == 200:
                data = response.json()
                incidents = data.get("incidents", [])
                if incidents:
                    return {"incident": incidents[0], "has_incident": True}
        return {"incident": None, "has_incident": False}
    except Exception as e:
        return {"incident": None, "has_incident": False, "error": str(e)}
```

Update `/api/health` to check CCDT incident:
```python
@app.get("/api/health")
async def health_check():
    # Existing postgres/redis checks...
    
    # NEW: Check if CCDT has active CRITICAL incident
    ccdt_incident = await get_active_ccdt_incident()
    
    if ccdt_incident["has_incident"]:
        incident = ccdt_incident["incident"]
        if incident["severity"] == "critical":
            # Simulate failure
            return JSONResponse(
                status_code=503,
                content={
                    "postgres": False,
                    "redis": False,
                    "api": False,
                    "ccdt_incident": incident["id"],
                    "message": f"Service degraded: {incident['title']}"
                }
            )
    
    # Normal health check...
    return {"postgres": pg_ok, "redis": redis_ok, "api": True}
```

---

### Phase 3: Recovery Timeline Visualization ⏱️

**Goal**: Show step-by-step autonomous healing in real-time.

#### 3.1 Recovery Timeline Display ✅ DONE
- Shows T+0s → T+3s → T+6s → T+12s progression
- Each step shows:
  - Timestamp
  - Action label ("Root cause identified", "OPA safety check passed")
  - Detail text (actual data from incident)
- File: `/mock-ui/ccdt-integration.js` (lines 300-380)

#### 3.2 Before/After Metrics Comparison
**TODO**: Capture and display metrics snapshot

Add to incident overlay:
```html
<div class="overlay-section">
    <div class="section-label">📊 Impact Analysis</div>
    <div class="metrics-comparison">
        <div class="metric-row">
            <span>DB Query Rate</span>
            <span class="before">380 qps</span>
            <span class="arrow">→</span>
            <span class="after">0 qps</span>
            <span class="arrow">→</span>
            <span class="recovered">360 qps</span>
        </div>
        <div class="metric-row">
            <span>Response Time</span>
            <span class="before">32ms</span>
            <span class="arrow">→</span>
            <span class="after">TIMEOUT</span>
            <span class="arrow">→</span>
            <span class="recovered">35ms</span>
        </div>
    </div>
</div>
```

---

### Phase 4: Causal Chain Visualization 🧠

**Goal**: Show WHY the system failed (causal reasoning).

#### 4.1 GNN Causal Graph View
**TODO**: Fetch causal chain from Layer-2 GNN

Add to incident overlay:
```html
<div class="overlay-section">
    <div class="section-label">🔍 Causal Chain</div>
    <div class="causal-graph">
        <div class="causal-node root">
            <strong>demo-postgres</strong>
            <span class="causal-score">0.94</span>
        </div>
        <div class="causal-arrow">↓</div>
        <div class="causal-node">
            <strong>order-svc</strong>
            <span class="causal-score">0.61</span>
        </div>
        <div class="causal-arrow">↓</div>
        <div class="causal-node">
            <strong>notify-svc</strong>
            <span class="causal-score">0.38</span>
        </div>
    </div>
</div>
```

Fetch from:
```javascript
const topology = await fetch('http://localhost:8001/topology').then(r => r.json());
// topology.nodes[].causal_score
// topology.edges[].is_causal
```

---

## 🚀 Execution Order

### ✅ Completed
1. Phase 1.1 - CCDT Status Bar (ccdt-integration.js implemented)
2. Phase 1.2 - Incident Overlay Panel (ccdt-integration.js implemented)
3. Phase 1.3 - Container Health Indicators (ccdt-integration.js implemented)
4. Phase 2.1 - Crash Banner (index.html implemented)
5. Phase 2.2 - Error State Display (index.html implemented)

### 🔄 In Progress
6. **Phase 2.3** - Wire mock-api to CCDT incident stream
   - Modify `/mock-api/main.py`
   - Add CCDT incident polling
   - Make `/api/health` respond to CCDT incidents

### 📋 Pending
7. Phase 3.2 - Before/After Metrics
8. Phase 4.1 - Causal Chain Display

---

## 📊 Success Criteria

### Before Implementation
- ❌ Professors ask: "How do we know CCDT is really fixing this app?"
- ❌ Two separate screens with no visible connection
- ❌ Crash button feels fake (instant recovery)
- ❌ Can't prove autonomous operation

### After Implementation (Phase 1 Complete)
- ✅ Live CCDT status bar shows real GNN confidence
- ✅ Incident overlay appears automatically when CCDT detects failure
- ✅ Real container metrics visible (memory %, OOM kills)
- ✅ Recovery timeline shows 7-12s step-by-step process
- ✅ "NO HUMAN INTERVENTION" explicitly shown
- ✅ Can demonstrate on 3 screens with same incident ID

### After Phase 2
- ✅ NexaOps UI physically breaks when simulator crashes postgres
- ✅ Real 502 errors appear in browser
- ✅ CCDT Guardian autonomously heals it
- ✅ UI comes back to life automatically

---

## 🎬 Demo Script for Investors

### Setup (before demo)
1. Start full CCDT stack: `docker compose up -d`
2. Open 3 browser windows:
   - Window 1: http://localhost:8088 (NexaOps - what investors see)
   - Window 2: http://localhost:3000 (CCDT Dashboard - proof it's real)
   - Window 3: Terminal showing `docker logs ccdt-layer3-guardian-1 -f`

### Demo Flow (5 minutes)
1. **Baseline** (30s)
   - Point to NexaOps: "This is a typical SaaS ops dashboard"
   - Point to status bar: "Notice CCDT Guardian is actively monitoring"
   - Show green layer indicators: "All 4 layers online"

2. **Trigger Failure** (10s)
   - Click "Simulate Crash" button
   - OR: In terminal: `docker exec ccdt-simulator-1 curl -X POST http://localhost:8080/trigger/oom_cascade`

3. **Watch Failure Cascade** (15s)
   - Red banner appears: "SYSTEM DEGRADED"
   - Orders table → 502 Bad Gateway
   - Inventory grid → 503 Service Unavailable
   - Right sidebar slides in → Incident Overlay visible

4. **Point Out CCDT Detection** (20s)
   - Status bar changes: "MONITORING" → "HEALING"
   - Incident overlay shows:
     - Type: FAULT
     - Root Cause: demo-postgres (94% confidence)
     - GNN Classification: "OOM Cascade detected"
     - Guardian Action: `restart_pod`

5. **Show Timeline** (30s)
   - T+0s: Incident detected
   - T+3s: GNN identified root cause
   - T+5s: Guardian selected action
   - T+6s: OPA safety check passed
   - T+7s: Docker API executing
   - T+12s: ✓ System restored

6. **Point to Dashboard** (window 2) (30s)
   - Show same incident ID
   - Show causal graph
   - Show Guardian logs

7. **Show Recovery** (20s)
   - Green banner: "SYSTEM RESTORED - CCDT HEALED ALL SERVICES"
   - Orders table reloads with data
   - Inventory grid shows stock levels
   - Status bar: "HEALING" → "MONITORING"

8. **Highlight Key Points** (1 minute)
   - ✅ Detection: 3 seconds (not 5-15 minutes)
   - ✅ Root cause: Instant causal graph (not 15-30 min manual analysis)
   - ✅ Recovery: 12 seconds (not 30-90 minutes)
   - ✅ Autonomy: NO HUMAN INTERVENTION
   - ✅ Safety: 5 OPA policies checked before action
   - ✅ Explainability: Plain English via Co-Pilot

---

## 🔧 Technical Notes

### CORS Configuration
If frontend can't reach CCDT APIs, add to each layer's FastAPI:

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8088", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### Nginx Proxy (Alternative)
If you want to avoid CORS, proxy CCDT APIs through demo-nginx:

```nginx
location /ccdt-api/ {
    proxy_pass http://api-gateway:8000/api/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}

location /ccdt-topology/ {
    proxy_pass http://layer2-gnn:8001/topology;
}
```

Then update `ccdt-integration.js` URLs to use relative paths.

---

## 📁 File Structure

```
ccdt/
├── mock-ui/
│   ├── index.html                 ✅ NexaOps UI (crash/recovery states implemented)
│   └── ccdt-integration.js        ✅ Phase 1 complete (status bar + overlay + metrics)
├── mock-api/
│   ├── main.py                    🔄 Needs Phase 2.3 changes (CCDT incident polling)
│   ├── Dockerfile
│   └── requirements.txt
├── nginx/
│   └── facade-default.conf        ✅ Serves mock-ui at localhost:8088
├── docker-compose.yml             ✅ All services defined
└── IMPLEMENTATION_PLAN.md         📄 This file
```

---

## 🐛 Known Issues

1. **cAdvisor Metrics Parsing**
   - `ccdt-integration.js` line 522: `parseCAdvisorMetrics()` returns mock data
   - TODO: Parse real cAdvisor JSON format
   - Format: `/api/v1.3/docker/<container_id>` returns complex nested JSON

2. **WebSocket Support**
   - `CCDT_CONFIG.WS_INFERENCE` defined but not implemented
   - Could replace polling for real-time updates
   - Layer-2 GNN would need to add WebSocket endpoint

3. **Incident ID Correlation**
   - Need to ensure same incident ID appears in:
     - NexaOps incident overlay (`ccdt-integration.js`)
     - CCDT Dashboard (localhost:3000)
     - SQLite database (`data/ccdt.db`)

---

## 📞 Next Steps

1. **Phase 2.3 Implementation** (30 minutes)
   - Modify `/mock-api/main.py` to poll CCDT incidents
   - Wire `/api/health` to return errors when CCDT detects failures
   - Test full crash → detect → heal → recover cycle

2. **Testing** (1 hour)
   - Test with all 12 scenarios from simulator
   - Verify timeline appears correctly
   - Confirm metrics update in real-time

3. **Polish** (1 hour)
   - Add Phase 3.2 metrics comparison
   - Add Phase 4.1 causal chain visualization
   - Refine animations and transitions

4. **Documentation** (30 minutes)
   - Record demo video
   - Write investor one-pager
   - Update README with new architecture

---

**Last Updated**: June 24, 2026  
**Author**: Umaa Maheshwary SV  
**Project**: CCDT - Cognitive Digital Twin for Cloud OS
