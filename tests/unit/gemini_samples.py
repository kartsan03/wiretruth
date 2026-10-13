"""Synthetic GenerateContent examples; shapes follow the linked documentation, values are local."""

import base64
import json

from wiretruth.formats import build_exchange

MODEL = "gemini-3.5-flash-lite"
PATH = f"/v1beta/models/{MODEL}:generateContent"
STREAM_PATH = f"/v1beta/models/{MODEL}:streamGenerateContent"

# https://ai.google.dev/api/generate-content#GenerateContentResponse
TEXT = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": "Europa is an icy moon."}]},
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 6, "totalTokenCount": 15},
    "modelVersion": MODEL,
    "responseId": "response-moons",
}

# https://ai.google.dev/gemini-api/docs/generate-content/thinking#token-limits-and-max_output_tokens
# Empty output is documented; this precise missing-content layout awaits a live recording.
EMPTY_CANDIDATE = {
    "candidates": [{"finishReason": "MAX_TOKENS", "index": 0}],
    "usageMetadata": {"promptTokenCount": 21, "thoughtsTokenCount": 32, "totalTokenCount": 53},
    "modelVersion": "gemini-3.8-flash",
}

# https://ai.google.dev/gemini-api/docs/generate-content/thinking
THINKING = {
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [
                    {"text": "Compare the two possible pot sizes.", "thought": True},
                    {"text": "Eleven pots.", "thoughtSignature": "c2lnbmF0dXJl"},
                ],
            },
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 24,
        "cachedContentTokenCount": 12,
        "candidatesTokenCount": 4,
        "thoughtsTokenCount": 37,
        "totalTokenCount": 65,
        "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 24}],
    },
    "modelVersion": "gemini-3.8-flash",
}

# https://ai.google.dev/api/generate-content#FunctionCall
TOOLS = {
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {"name": "get_weather", "args": {"city": "São Paulo"}},
                        "thoughtSignature": "c2lnbmF0dXJl",
                    },
                    {
                        "functionCall": {
                            "id": "call-weather",
                            "name": "get_weather",
                            "args": {"city": "Zürich"},
                        }
                    },
                ],
            },
            "finishReason": "STOP",
        }
    ],
    "modelVersion": MODEL,
}

# https://ai.google.dev/gemini-api/docs/generate-content/api-errors
BAD_REQUEST = {
    "error": {"code": 400, "message": "Invalid generation options.", "status": "INVALID_ARGUMENT"}
}
RATE_LIMIT = {"error": {"code": 429, "message": "Quota exhausted.", "status": "RESOURCE_EXHAUSTED"}}

# https://ai.google.dev/api/generate-content#PromptFeedback
BLOCKED = {
    "promptFeedback": {"blockReason": "SAFETY"},
    "usageMetadata": {"promptTokenCount": 9, "totalTokenCount": 9},
}


def event(payload):
    return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


# https://ai.google.dev/api/generate-content#method:-models.streamgeneratecontent
# Repeated usage snapshots and their exact placement are assumptions pending a live recording.
STREAM = [
    event(
        {
            "candidates": [{"content": {"role": "model", "parts": [{"text": "Café "}]}}],
            "usageMetadata": {
                "promptTokenCount": 9,
                "candidatesTokenCount": 2,
                "totalTokenCount": 11,
            },
            "modelVersion": MODEL,
        }
    ),
    event(
        {
            "candidates": [{"content": {"parts": [{"text": "καλημέρα 東京 🌸"}]}}],
            "usageMetadata": {
                "promptTokenCount": 9,
                "candidatesTokenCount": 8,
                "totalTokenCount": 17,
            },
        }
    ),
    event(
        {
            "candidates": [{"finishReason": "MAX_TOKENS", "index": 0}],
            "usageMetadata": {
                "promptTokenCount": 9,
                "candidatesTokenCount": 8,
                "thoughtsTokenCount": 3,
                "totalTokenCount": 20,
            },
        }
    ),
]

# https://ai.google.dev/gemini-api/docs/generate-content/api-errors (the error envelope)
# https://github.com/googleapis/python-genai/blob/main/google/genai/_api_client.py (raises on a
# streamed chunk that starts with {"error":)
STREAM_ERROR = event(
    {"error": {"code": 503, "status": "UNAVAILABLE", "message": "Generation interrupted."}}
)


def exchange(payload=None, *, events=None, status=200, end="clean", kind=None):
    stream = events is not None
    payload = TEXT if payload is None else payload
    raw = "".join(events).encode() if stream else json.dumps(payload, ensure_ascii=False).encode()
    body = {"sse": events} if stream else {"json": payload}
    if kind == "text":
        body = {"text": raw.decode()}
    elif kind == "chunks_b64":
        body = {"chunks_b64": [base64.b64encode(raw[i : i + 1]).decode() for i in range(len(raw))]}
    return build_exchange(
        {
            "match": {"method": "POST", "path": STREAM_PATH if stream else PATH},
            "response": {
                "status": status,
                "headers": {"content-type": "text/event-stream" if stream else "application/json"},
                "body": body,
                "end": end,
            },
        }
    )
