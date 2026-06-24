#!/bin/bash

echo "🧹 Clearing Vite cache and node_modules cache..."

# Stop any running dev server
echo "Stopping any running dev servers..."
pkill -f "vite" || true

# Clear Vite cache
echo "Removing .vite cache..."
rm -rf node_modules/.vite

# Clear dist folder
echo "Removing dist folder..."
rm -rf dist

echo "✅ Cache cleared!"
echo ""
echo "Now run: npm run dev"
echo "Then in your browser: Hard refresh (Cmd+Shift+R on Mac, Ctrl+Shift+R on Windows)"
