#!/bin/bash
# Test script to verify live updates are working

echo "==================================="
echo "CCDT Live Updates Test"
echo "==================================="
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test 1: Check if API Gateway is responding
echo "Test 1: API Gateway Health"
if curl -s -f http://localhost:8000/health > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} API Gateway is responding"
else
    echo -e "${RED}✗${NC} API Gateway is not responding"
    exit 1
fi
echo ""

# Test 2: Check topology endpoint
echo "Test 2: Topology Data"
NODES=$(curl -s http://localhost:8000/api/v1/topology | jq -r '.nodes | length')
EDGES=$(curl -s http://localhost:8000/api/v1/topology | jq -r '.edges | length')
if [ "$NODES" -gt 0 ] && [ "$EDGES" -gt 0 ]; then
    echo -e "${GREEN}✓${NC} Topology has $NODES nodes and $EDGES edges"
else
    echo -e "${RED}✗${NC} Topology data is empty"
fi
echo ""

# Test 3: Check for unhealthy nodes
echo "Test 3: Node Status Variation"
UNHEALTHY=$(curl -s http://localhost:8000/api/v1/topology | jq -r '.nodes[] | select(.status != "healthy") | .id' | wc -l)
if [ "$UNHEALTHY" -gt 0 ]; then
    echo -e "${GREEN}✓${NC} Found $UNHEALTHY unhealthy node(s)"
    curl -s http://localhost:8000/api/v1/topology | jq -r '.nodes[] | select(.status != "healthy") | "  - \(.id): \(.status) (CPU: \(.cpu)%, MEM: \(.mem)%)"'
else
    echo -e "${YELLOW}⚠${NC} All nodes are healthy (might be between scenarios)"
fi
echo ""

# Test 4: Guardian policies
echo "Test 4: Guardian Policies"
VIOLATIONS=$(curl -s http://localhost:8000/api/v1/guardian/policies | jq -r '.total_violations')
if [ "$VIOLATIONS" -ge 0 ]; then
    echo -e "${GREEN}✓${NC} Guardian has $VIOLATIONS policy violation(s)"
else
    echo -e "${RED}✗${NC} Guardian policies endpoint failed"
fi
echo ""

# Test 5: Guardian actions
echo "Test 5: Guardian Actions"
ACTIONS=$(curl -s http://localhost:8000/api/v1/guardian/actions | jq -r '.actions | length')
if [ "$ACTIONS" -gt 0 ]; then
    echo -e "${GREEN}✓${NC} Guardian has $ACTIONS remediation action(s)"
else
    echo -e "${RED}✗${NC} Guardian actions endpoint failed"
fi
echo ""

# Test 6: Check if data changes over time
echo "Test 6: Data Changes Over Time (10 seconds)"
echo "Checking if topology data changes..."

# Get initial state
INITIAL_CPU=$(curl -s http://localhost:8000/api/v1/topology | jq -r '.nodes[0].cpu')
sleep 5
# Get state after 5 seconds
AFTER_5S=$(curl -s http://localhost:8000/api/v1/topology | jq -r '.nodes[0].cpu')
sleep 5
# Get state after 10 seconds
AFTER_10S=$(curl -s http://localhost:8000/api/v1/topology | jq -r '.nodes[0].cpu')

echo "  Initial: ${INITIAL_CPU}%"
echo "  After 5s: ${AFTER_5S}%"
echo "  After 10s: ${AFTER_10S}%"

if [ "$INITIAL_CPU" != "$AFTER_5S" ] || [ "$AFTER_5S" != "$AFTER_10S" ]; then
    echo -e "${GREEN}✓${NC} Topology data is changing over time"
else
    echo -e "${YELLOW}⚠${NC} Topology data appears static (backend might be between scenario changes)"
fi
echo ""

# Test 7: Check simulator is running
echo "Test 7: Simulator Status"
SIMULATOR_STATUS=$(docker compose ps simulator --format json | jq -r '.[0].State' 2>/dev/null)
if [ "$SIMULATOR_STATUS" = "running" ]; then
    echo -e "${GREEN}✓${NC} Simulator is running"
    echo "Recent simulator activity:"
    docker compose logs simulator --tail 3 2>/dev/null | sed 's/^/  /'
else
    echo -e "${RED}✗${NC} Simulator is not running"
fi
echo ""

# Test 8: Dashboard availability
echo "Test 8: Dashboard Accessibility"
if curl -s -f http://localhost:3000 > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Dashboard is accessible at http://localhost:3000"
else
    echo -e "${RED}✗${NC} Dashboard is not accessible"
fi
echo ""

echo "==================================="
echo "Test Complete!"
echo "==================================="
echo ""
echo "Next steps:"
echo "1. Open http://localhost:3000 in your browser"
echo "2. Open DevTools Console (F12)"
echo "3. Watch for console logs:"
echo "   - 'Topology data updated: {...}'"
echo "   - 'Guardian data updated: {...}'"
echo "   - 'GNN inference updated: {...}'"
echo "4. Switch between tabs and watch data update"
echo ""
