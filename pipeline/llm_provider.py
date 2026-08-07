from __future__ import annotations

import os
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI


def get_llm(provider: str, model: str, temperature: float = 0) -> Any:
    """Get an LLM instance for the specified provider and model."""
    provider = provider.lower()
    
    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is required for Anthropic provider")
        return ChatAnthropic(model=model, temperature=temperature, api_key=api_key)
    
    elif provider == "groq":
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY environment variable is required for Groq provider")
        return ChatGroq(model=model, temperature=temperature, api_key=api_key)
    
    elif provider == "google":
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY environment variable is required for Google provider")
        return ChatGoogleGenerativeAI(model=model, temperature=temperature, api_key=api_key)
    
    else:
        raise ValueError(f"Unsupported provider: {provider}. Supported providers: anthropic, groq, google")


def get_default_model_for_provider(provider: str) -> str:
    """Get the default model name for a given provider."""
    provider = provider.lower()
    
    defaults = {
        "anthropic": "claude-sonnet-4-6",
        "groq": "llama-3.3-70b-versatile",
        "google": "gemini-3.1-flash-lite",
    }
    
    if provider not in defaults:
        raise ValueError(f"Unsupported provider: {provider}")
    
    return defaults[provider]
