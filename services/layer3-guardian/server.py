"""
CCDT Layer-3 Guardian — FastAPI Server Entry Point
════════════════════════════════════════════════════
Re-exports the FastAPI app from executor.py.
Start with:  uvicorn server:app --host 0.0.0.0 --port 8002
"""
from executor import app  # noqa: F401  — app is the FastAPI instance
