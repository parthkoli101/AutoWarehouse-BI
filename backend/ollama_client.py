"""
Thin client for talking to a locally running Ollama server.

Ollama's REST API is documented at:
  POST http://localhost:11434/api/generate  (single-turn, streaming or not)

No API key -- this is entirely local inference, per the project's
no-third-party-LLM-API constraint.
"""

import json
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "qwen2.5:7b"
REQUEST_TIMEOUT_SECONDS = 300  # local 7B inference over a larger schema+KPI/chart prompt can take a while
                                # on CPU, especially on first call while Ollama loads the model into RAM


class OllamaError(Exception):
    pass


def generate_json(prompt: str, model: str = DEFAULT_MODEL) -> dict:
    """Sends a prompt to Ollama, expects (and enforces) a JSON-only response.
    Raises OllamaError with a clear message on any failure -- connection
    refused (Ollama not running), timeout, or invalid JSON in the reply."""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",  # Ollama-level constraint: forces valid JSON output
                "options": {"temperature": 0.2},  # low temperature -- this is a structured generation task, not creative writing
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.ConnectionError:
        raise OllamaError(
            "Could not reach Ollama at localhost:11434. Is it running? "
            "Start it with 'ollama serve' or open the Ollama app."
        )
    except requests.exceptions.Timeout:
        raise OllamaError(f"Ollama did not respond within {REQUEST_TIMEOUT_SECONDS}s. Try a smaller model or check it's not overloaded.")

    if response.status_code != 200:
        raise OllamaError(f"Ollama returned HTTP {response.status_code}: {response.text[:300]}")

    body = response.json()
    raw_text = body.get("response", "")

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise OllamaError(f"Ollama's response wasn't valid JSON ({e}). Raw output: {raw_text[:300]}")
