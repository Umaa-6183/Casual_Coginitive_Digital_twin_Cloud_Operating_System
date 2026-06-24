# Validation Testing Guide

## How to Verify the Fix

### 1. Check Browser Console
Open the dashboard at http://localhost:3000 and check the browser console:
- **Before Fix**: Hundreds of `Error: <path> attribute d: Expected number, "M NaN,NaN Q NaN,Na…"` errors
- **After Fix**: No SVG path errors (only legitimate backend connection warnings if any)

### 2. Test Invalid Data Scenarios

#### Scenario A: Backend Returns Invalid Coordinates
Test by modifying mock data temporarily:
```typescript
// In useTopology.ts or mock-api response
const testNode = {
  id: 'test',
  label: 'Test',
  x: NaN,  // Invalid
  y: 100,
  status: 'healthy',
  layer: 'service',
  cpu: 50,
  mem: 60
};
```
**Expected**: Node should be filtered out, not rendered, no console errors

#### Scenario B: Invalid Metrics
```typescript
const testNode = {
  id: 'test',
  label: 'Test',
  x: 100,
  y: 100,
  status: 'healthy',
  layer: 'service',
  cpu: NaN,  // Invalid
  mem: Infinity  // Invalid
};
```
**Expected**: Node filtered out or metrics defaulted to 0, no console errors

#### Scenario C: Normal Operation
```typescript
const testNode = {
  id: 'test',
  label: 'Test',
  x: 400,
  y: 200,
  status: 'healthy',
  layer: 'service',
  cpu: 45,
  mem: 60
};
```
**Expected**: Node renders correctly with all visualizations

### 3. Visual Tests

Navigate to the Topology tab and verify:
1. ✅ All nodes render with proper positions
2. ✅ All edges render as curves between nodes
3. ✅ CPU arc animations work correctly
4. ✅ Status colors display properly
5. ✅ Node selection highlights work
6. ✅ Causal edge animations play
7. ✅ No console errors

### 4. Stress Test

To verify validation under load:
```javascript
// In browser console
const store = window.__ZUSTAND_STORE__;
const invalidNodes = Array.from({length: 100}, (_, i) => ({
  id: `test-${i}`,
  label: `Test ${i}`,
  x: Math.random() > 0.5 ? NaN : Math.random() * 800,
  y: Math.random() > 0.5 ? NaN : Math.random() * 500,
  status: 'healthy',
  layer: 'service',
  cpu: Math.random() > 0.5 ? NaN : Math.random() * 100,
  mem: Math.random() * 100
}));
store.getState().setNodes(invalidNodes);
```
**Expected**: Only valid nodes render, no console errors

### 5. Check Fallback Data
If backend is unavailable:
- Dashboard should show fallback topology with 10 pre-defined nodes
- All nodes should render correctly
- No console errors

## Success Criteria
✅ Zero SVG path NaN errors in console  
✅ Invalid nodes gracefully filtered out  
✅ Valid nodes render correctly  
✅ No visual glitches or broken graphs  
✅ Performance remains smooth  
