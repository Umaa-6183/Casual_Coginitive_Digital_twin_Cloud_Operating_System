# CCDT Frontend Verification Checklist

Use this checklist to verify that all frontend changes work correctly.

---

## Pre-Flight Checks

- [ ] Backend server is running (`http://localhost:8000`)
- [ ] Frontend dev server is running (`npm run dev`)
- [ ] Browser DevTools open (F12)
- [ ] Network tab is visible
- [ ] Console tab is visible

---

## 1. Intelligence Layer Testing

### Navigate to Intelligence Tab
- [ ] Tab loads without errors
- [ ] Shows loading spinner initially
- [ ] Data appears within 5 seconds

### Verify Dynamic Data
- [ ] **Graph Classification** shows percentages (attack/fault/healthy)
- [ ] **Root Cause Node** displays a node name (not fixed "order-svc")
- [ ] **Blast Radius** shows a list of affected nodes
- [ ] **Causal Chain** displays multiple nodes with scores
- [ ] Each causal chain item shows:
  - [ ] Node name
  - [ ] Causal score percentage
  - [ ] Status badge (critical/warning/healthy)

### Test Polling
- [ ] Keep Network tab open
- [ ] See `/api/v1/infer` requests every ~5 seconds
- [ ] Data updates in UI (check inference time changes)

### Test Error Handling
- [ ] Stop backend server
- [ ] Wait 5-10 seconds
- [ ] Error message appears in UI
- [ ] No console errors or crashes

**✅ Intelligence Layer: PASS / FAIL**

---

## 2. Guardian Layer Testing

### Navigate to Guardian Tab
- [ ] Tab loads without errors
- [ ] Shows loading spinner initially
- [ ] Data appears within 5 seconds

### Verify Dynamic KPIs
- [ ] **MTTR Reduction** shows a percentage
- [ ] **False Positive** shows a percentage
- [ ] **OPA Compliance** shows a percentage
- [ ] **Auto-Resolved** shows a percentage

### Verify OPA Policies
- [ ] Policy list displays
- [ ] Each policy shows:
  - [ ] Policy name
  - [ ] Status (PASS / VIOLATION)
  - [ ] Description
- [ ] Total count shows (e.g., "4/5 PASS")

### Verify RL Actions
- [ ] Action list displays (should be 3-5 actions)
- [ ] Each action shows:
  - [ ] Description text
  - [ ] Confidence percentage
  - [ ] Risk level (LOW/MED/HIGH)
  - [ ] Impact (MTTR %)
  - [ ] "Ghost Preview" button

### Test Polling
- [ ] Keep Network tab open
- [ ] See `/api/v1/guardian/policies` requests every ~5 seconds
- [ ] See `/api/v1/guardian/actions` requests every ~5 seconds

**✅ Guardian Layer: PASS / FAIL**

---

## 3. Ghost Preview Testing

### Trigger Ghost Preview
- [ ] Click "Ghost Preview" button on any Guardian action
- [ ] Modal opens
- [ ] Phase stepper animates (4 phases)

### Verify Simulation
- [ ] Phases progress:
  1. [ ] Initialising simulation sandbox
  2. [ ] Cloning cluster state snapshot
  3. [ ] Running action in isolated twin
  4. [ ] Evaluating OPA policies
- [ ] Final results appear after ~3 seconds

### Verify Results Display
- [ ] **OPA Status** banner shows PASS or FAIL
- [ ] Metrics displayed:
  - [ ] MTTR Impact (positive or negative %)
  - [ ] Traffic Impact (positive or negative %)
  - [ ] Risk Score (0-100)
  - [ ] Confidence percentage
- [ ] **Recommendation** text appears
- [ ] "Approve & Execute" button shows (or "Blocked by OPA")

### Test API Call
- [ ] Check Network tab during simulation
- [ ] See `POST /api/v1/actions/preview` request
- [ ] Verify request body contains:
  - [ ] `action_name`
  - [ ] `target_node`
  - [ ] `namespace`

### Test Multiple Actions
- [ ] Click Ghost Preview on 3 different actions
- [ ] Each shows different results
- [ ] Risk scores differ
- [ ] MTTR impacts differ

**✅ Ghost Preview: PASS / FAIL**

---

## 4. Incidents Dashboard Testing

### Navigate to Incidents Tab
- [ ] Tab loads without errors
- [ ] Shows loading spinner initially
- [ ] Incident list appears within 5 seconds

### Verify Incident List
- [ ] Multiple incidents display in left panel
- [ ] Each incident shows:
  - [ ] Incident ID (e.g., INC-2847)
  - [ ] Title
  - [ ] Status badge (active/investigating/resolved)
  - [ ] Elapsed time
  - [ ] Node name

### Test Filters
- [ ] Click "Active" filter
  - [ ] Only active incidents show
  - [ ] API called with `?status=active`
- [ ] Click "Investigating" filter
  - [ ] List updates
- [ ] Click "Resolved" filter
  - [ ] Only resolved incidents show
- [ ] Click "All" filter
  - [ ] All incidents reappear

### Verify Incident Detail
- [ ] Click on an incident
- [ ] Right panel shows:
  - [ ] Incident header (ID, severity, type)
  - [ ] Root Cause section
  - [ ] Affected Services list
  - [ ] Autonomous Action taken
  - [ ] Timeline with events

### Test Polling
- [ ] Keep Network tab open
- [ ] See `/api/v1/incidents` requests every ~5 seconds
- [ ] Incident list updates dynamically

**✅ Incidents Dashboard: PASS / FAIL**

---

## 5. Topology Dashboard Testing

### Navigate to Topology Tab
- [ ] Tab loads without errors
- [ ] Shows loading spinner initially
- [ ] Topology graph appears within 3 seconds

### Verify Topology Display
- [ ] Nodes render on canvas
- [ ] Edges connect nodes
- [ ] Stats bar shows:
  - [ ] Total nodes count
  - [ ] Critical nodes count (red)
  - [ ] Warning nodes count (yellow)
  - [ ] Healthy nodes count (green)
  - [ ] Causal edges count

### Verify Node States
- [ ] Some nodes are red (critical)
- [ ] Some nodes are yellow (warning)
- [ ] Some nodes are green (healthy)
- [ ] Node colors match Intelligence classifications

### Verify Causal Edges
- [ ] Some edges are red (causal)
- [ ] Red edges connect nodes in causal chain
- [ ] Check Intelligence tab for causal chain nodes
- [ ] Verify same nodes have causal edges in Topology

### Test Node Selection
- [ ] Click a node
- [ ] Right panel shows NodeCard with:
  - [ ] Node name
  - [ ] CPU/Memory metrics
  - [ ] Status
  - [ ] Alerts (if any)

### Test Polling
- [ ] Keep Network tab open
- [ ] See `/api/v1/topology` requests every ~3 seconds
- [ ] Node states update (check CPU/mem values change)

**✅ Topology Dashboard: PASS / FAIL**

---

## 6. Cross-Layer Synchronization Testing

### Test Scenario Consistency
- [ ] Note **root cause node** from Intelligence tab
- [ ] Switch to Topology tab
  - [ ] Root cause node is colored **red (critical)**
- [ ] Switch to Guardian tab
  - [ ] Recommended actions target **root cause node**
- [ ] Switch to Incidents tab
  - [ ] Active incident references **root cause node**

### Test Causal Chain Sync
- [ ] Intelligence tab: note causal chain nodes (e.g., A → B → C)
- [ ] Topology tab:
  - [ ] Edge from A to B is **red (causal)**
  - [ ] Edge from B to C is **red (causal)**
  - [ ] All causal chain nodes are **critical or warning**

### Test Dynamic Scenario Change
**Note:** This requires backend scenario switching capability

- [ ] Backend: Switch from Scenario A to Scenario B
- [ ] Wait 5-10 seconds
- [ ] Verify all screens update:
  - [ ] Intelligence: New root cause
  - [ ] Guardian: Different actions
  - [ ] Topology: Different critical nodes
  - [ ] Incidents: New incident or updated status

**✅ Cross-Layer Sync: PASS / FAIL**

---

## 7. Error Handling Testing

### Test Backend Offline
- [ ] Stop backend server
- [ ] Refresh frontend
- [ ] Each tab shows:
  - [ ] Loading state initially
  - [ ] Error message after timeout
  - [ ] No crashes or blank screens
  - [ ] User can still navigate between tabs

### Test Invalid API Response
**Note:** Requires backend to return malformed data

- [ ] Backend returns invalid JSON
- [ ] Frontend shows error message
- [ ] Console logs error details
- [ ] App remains functional

### Test Network Timeout
- [ ] Simulate slow network (DevTools → Network → Throttling)
- [ ] All tabs still load (just slower)
- [ ] Loading spinners show during fetch
- [ ] No timeouts under 15 seconds

**✅ Error Handling: PASS / FAIL**

---

## 8. Performance Testing

### Check Polling Overhead
- [ ] Open Network tab
- [ ] Let app run for 1 minute
- [ ] Count total requests:
  - [ ] `/infer`: ~12 requests (5s interval)
  - [ ] `/guardian/policies`: ~12 requests
  - [ ] `/guardian/actions`: ~12 requests
  - [ ] `/incidents`: ~12 requests
  - [ ] `/topology`: ~20 requests (3s interval)
- [ ] Total: ~70 requests/minute = ~1.2 req/sec ✅

### Check Memory Usage
- [ ] Open DevTools → Memory profiler
- [ ] Let app run for 5 minutes
- [ ] Take heap snapshot
- [ ] No memory leaks (graph should plateau, not grow linearly)

### Check Render Performance
- [ ] Open DevTools → Performance tab
- [ ] Record for 10 seconds
- [ ] No dropped frames
- [ ] Rendering stays < 16ms per frame

**✅ Performance: PASS / FAIL**

---

## 9. UI/UX Verification

### Visual Consistency
- [ ] All tabs maintain same design language
- [ ] Colors match design system
- [ ] Fonts are consistent
- [ ] Spacing is uniform

### Loading States
- [ ] Spinner appears for all API calls
- [ ] Loading text is clear ("Loading Intelligence…")
- [ ] No layout shift when data loads

### Error States
- [ ] Error messages are user-friendly
- [ ] Error color (red) is visible
- [ ] User can retry (refresh or navigate away)

### Interactions
- [ ] Buttons have hover states
- [ ] Click feedback is immediate
- [ ] Modal opens/closes smoothly
- [ ] Tab navigation is instant

**✅ UI/UX: PASS / FAIL**

---

## 10. Browser Compatibility

### Test in Chrome
- [ ] All features work
- [ ] No console errors
- [ ] Animations smooth

### Test in Firefox
- [ ] All features work
- [ ] No console errors
- [ ] Animations smooth

### Test in Safari
- [ ] All features work
- [ ] No console errors
- [ ] Animations smooth

**✅ Browser Compatibility: PASS / FAIL**

---

## Final Sign-Off

### All Tests Pass?
- [ ] Intelligence Layer ✅
- [ ] Guardian Layer ✅
- [ ] Ghost Preview ✅
- [ ] Incidents Dashboard ✅
- [ ] Topology Dashboard ✅
- [ ] Cross-Layer Sync ✅
- [ ] Error Handling ✅
- [ ] Performance ✅
- [ ] UI/UX ✅
- [ ] Browser Compatibility ✅

### Known Issues
List any issues found:
1. 
2. 
3. 

### Blocker Issues
List any critical issues that block deployment:
1. 
2. 

---

## Approval

**Tested by:** _________________  
**Date:** _________________  
**Status:** ⬜ APPROVED / ⬜ NEEDS FIXES  

**Notes:**


---

**Next Step:** If all checks pass → Ready to merge to main branch
