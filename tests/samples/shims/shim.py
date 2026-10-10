"""A process whose protocol behavior is selected by the manifest's MODE variable."""

import http.client
import json
import os
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

if os.name == "posix":
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

call = json.load(sys.stdin)
mode = os.environ["MODE"]


def hang():
    print("waiting", file=sys.stderr, flush=True)
    time.sleep(60)


def crash():
    os.kill(os.getpid(), signal.SIGSEGV)


if mode == "hang-before":
    hang()
if mode == "crash-before":
    crash()

if mode not in {"unsupported", "internal"}:
    url = urlsplit(os.environ["WIRETRUTH_BASE_URL_OPENAI_CHAT"])
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
    path = "/wrong" if mode == "wrong-path" else url.path + "/chat/completions"
    stream = not call["stream"] if mode == "wrong-stream" else call["stream"]
    connection.request(
        "POST", path, json.dumps({"stream": stream}), {"Content-Type": "application/json"}
    )
    connection.getresponse().read()
    connection.close()

if mode == "hang-child":
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
if mode in {"hang-after", "hang-child"}:
    hang()
if mode == "crash-after":
    crash()
if mode == "junk":
    print("not an observation")
    sys.exit(0)
if mode == "empty":
    sys.exit(0)

obs = {
    "format": "wiretruth.observation/v1",
    "library": {"name": "sample", "version": "1", "language": "python", "api": "sample.call"},
    "outcome": "returned",
    "unsupported_reason": None,
    "error": None,
    "finish": {"value": "length", "raw": "length", "surface": "primary", "source": "finish"},
    "text": {"value": "Mountain weather", "surface": "primary"},
    "structured": {"attempted": False, "returned_as_success": False, "value": None},
    "tool_calls": [],
    "usage": None,
    "stream": {"used": call["stream"], "completed": None, "chunks_seen": None},
    "notes": [],
    "elapsed_ms": 0,
}
if mode == "unsupported":
    obs["outcome"] = "unsupported"
    obs["unsupported_reason"] = "No feature: https://example.org/features"
elif mode == "internal":
    obs["outcome"] = "raised"
    obs["error"] = {
        "type": "builtins.IndexError",
        "message": "No first chunk",
        "category": "internal",
        "retry_after": None,
        "stack_top": "sample.py:1",
    }
elif mode == "absent":
    obs["finish"] = None
elif mode == "wrong":
    obs["finish"]["value"] = "stop"

print("sample stderr", file=sys.stderr)
print(json.dumps(obs))
sys.exit(3 if mode == "exit" else 0)
