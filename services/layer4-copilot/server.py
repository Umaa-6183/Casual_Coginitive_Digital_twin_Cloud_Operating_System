"""
CCDT Layer-4 Co-Pilot — FastAPI Server Entry Point
════════════════════════════════════════════════════
Re-exports the FastAPI app from copilot.py.
Start with:  uvicorn server:app --host 0.0.0.0 --port 8003
"""
from copilot import app  # noqa: F401  — app is the FastAPI instance
