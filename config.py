"""Shared configuration loaded from environment variables or a local .env file."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").strip().lower()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
GOOGLE_MODEL = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
# Optional test-only endpoint called before every generated test. Leave empty
# for real APIs that do not expose an isolated reset mechanism.
TEST_RESET_PATH = os.getenv("TEST_RESET_PATH") or None
