# SVG Path NaN Error Fix

## Problem
The dashboard was throwing hundreds of console errors:
```
Error: <path> attribute d: Expected number, "M NaN,NaN Q NaN,Na…".
```

This occurred when the topology map tried to render SVG paths with invalid coordinates (NaN values).

## Root Cause
Node coordinates (x, y) and metrics (cpu, mem) were not properly validated before being used in SVG path calculations. If the backend returned invalid data or data wasn't initialized properly, NaN values would propagate through calculations and into SVG path strings.

## Changes Made

### 1. [TopologyMap.tsx](src/components/topology/TopologyMap.tsx)
Added comprehensive validation at multiple points:

- **renderEdge()**: 
  - Added `isFinite()` checks for all node coordinates before calculation
  - Added validation after coordinate transformation to SVG space
  - Removed console.warn to reduce noise
  
- **renderNode()**:
  - Added `isFinite()` checks for x, y coordinates
  - Added validation after transformation
  - Validated CPU values before arc calculation
  - Added fallback for CPU display text (defaults to 0)

### 2. [useTopology.ts](src/hooks/useTopology.ts)
Enhanced data validation when receiving topology from backend:
- Filter nodes to ensure x, y, cpu, mem are all finite numbers
- Added `isFinite()` checks in addition to type checks
- Prevents invalid data from ever entering the state

### 3. [useClusterStore.ts](src/stores/useClusterStore.ts)
Added validation at the state management layer:

- **setNodes()**: Filter out any nodes with invalid coordinates or metrics before storing
- **updateNodeMetric()**: Validate cpu and mem values, fallback to existing values if invalid

## Validation Strategy
The fix uses a layered validation approach:

1. **Data Source Layer** (useTopology): Filter invalid data from backend
2. **State Layer** (useClusterStore): Ensure only valid data is stored
3. **Rendering Layer** (TopologyMap): Final validation before SVG rendering

Each layer checks using:
```typescript
typeof value === 'number' && isFinite(value)
```

This catches:
- `undefined`
- `null`
- `NaN`
- `Infinity` / `-Infinity`

## Testing
- Build successful with no TypeScript errors
- All layers properly filter invalid data
- Fallback topology data has valid coordinates

## Future Improvements
Consider adding:
- Backend validation to ensure data quality at source
- Monitoring/alerting when invalid topology data is detected
- Default coordinate assignment for nodes missing positions
