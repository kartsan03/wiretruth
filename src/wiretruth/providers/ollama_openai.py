"""ollama-openai: Ollama's OpenAI-compatible ``POST /v1/chat/completions``.

Truth and transforms use the Chat Completions rules in ``openai_chat``. Requests use
``max_tokens`` because Ollama ignores ``max_completion_tokens``. No API key is needed.
"""

from wiretruth.formats import Call
from wiretruth.providers import RawRequest, openai_chat
from wiretruth.providers.ollama import DEFAULT_MODEL as DEFAULT_MODEL
from wiretruth.providers.ollama import auth_headers as auth_headers
from wiretruth.providers.openai_chat import error_event as error_event
from wiretruth.providers.openai_chat import extract_truth as extract_truth
from wiretruth.providers.openai_chat import finish_index as finish_index
from wiretruth.providers.openai_chat import replace_finish as replace_finish
from wiretruth.providers.openai_chat import requested_stream as requested_stream
from wiretruth.providers.openai_chat import returned_model as returned_model
from wiretruth.replay import BASE_PATHS

ID = "ollama-openai"
API_BASE = "http://127.0.0.1:11434" + BASE_PATHS[ID]
KEY_ENV = ""


def build_request(call: Call, model: str) -> RawRequest:
    request = openai_chat.build_request(call, model)
    if "max_completion_tokens" in request.body:
        request.body["max_tokens"] = request.body.pop("max_completion_tokens")
    return request
