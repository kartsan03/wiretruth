import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from wiretruth.formats import call_spec_document, observation_from_document
from wiretruth.runner import find_cells

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")


def node(code, stdin=""):
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            code,
            (REPO / "shims/js/common/observe.mjs").as_uri(),
        ],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stderr == ""
    return json.loads(result.stdout)


def test_javascript_call_and_surface_helpers_preserve_unicode_and_zero():
    value = {"topic": "Café, Δ, 東京 🌕"}
    result = node(
        """
const { readCall, surface, writeObservation } = await import(process.argv[1]);
writeObservation({ call: readCall(), empty: surface(undefined), zero: surface(0),
  raw: surface('nébuleuse', 'raw') });
""",
        json.dumps(value),
    )
    assert result == {
        "call": value,
        "empty": {"value": None, "surface": "absent"},
        "zero": {"value": 0, "surface": "primary"},
        "raw": {"value": "nébuleuse", "surface": "raw"},
    }


@pytest.mark.parametrize(
    "kind",
    [
        "TypeError",
        "RangeError",
        "ReferenceError",
        "SyntaxError",
        "URIError",
        "EvalError",
        "AggregateError",
        "Error",
    ],
)
def test_javascript_builtin_errors_use_only_the_top_library_frame(kind):
    result = node(f"""
const {{ classifyError }} = await import(process.argv[1]);
const error = new {kind}({"[]," if kind == "AggregateError" else ""} 'crème');
error.stack = `${{error.name}}: crème
    at render (file:///tmp/node_modules/@sdk/core/dist/index.js:12:8)
    at main (/tmp/shim.mjs:9:2)`;
const internal = classifyError(error, []);
error.stack = `${{error.name}}: crème
    at main (/tmp/shim.mjs:9:2)
    at render (/tmp/node_modules/sdk/index.js:1:2)`;
console.log(JSON.stringify({{ internal, local: classifyError(error, []) }}));
""")
    assert result["internal"]["category"] == "internal"
    assert result["internal"]["stack_top"] == "@sdk/core/dist/index.js:12"
    assert result["local"]["category"] == "other"
    assert result["local"]["stack_top"] is None


def test_javascript_error_map_matches_names_inheritance_and_message_regex():
    result = node("""
const { classifyError } = await import(process.argv[1]);
class ProviderError extends Error {
  constructor() { super('quota exceeded'); this.name = 'SDK_API'; }
}
const error = new ProviderError();
const map = [
  { type: 'SDK_API', category: 'parse', message_regex: '^invalid' },
  { type: 'SDK_API', category: 'rate_limit', message_regex: '^quota' },
];
console.log(JSON.stringify({ name: classifyError(error, map),
  base: classifyError(error, [{ type: 'Error', category: 'api' }]),
  returned: classifyError({ name: 'RPC', message: 'busy' }, [{ type: 'RPC', category: 'api' }]) }));
""")
    assert result["name"]["category"] == "rate_limit"
    assert result["base"]["category"] == "api"
    assert result["returned"]["category"] == "api"


def test_javascript_error_message_cut_keeps_whole_code_points():
    result = node("""
const { classifyError } = await import(process.argv[1]);
console.log(JSON.stringify(classifyError(new Error('x'.repeat(499) + '🌕 Mond'), [])));
""")
    assert result["message"] == "x" * 499 + "🌕"


@pytest.fixture
def vercel(tmp_path):
    shim = tmp_path / "vercel-ai"
    shutil.copytree(REPO / "shims/js/common", tmp_path / "common")
    shim.mkdir()
    for name in ("shim.mjs", "wiretruth-shim.json"):
        shutil.copy(REPO / "shims/js/vercel-ai" / name, shim)
    modules = {
        "ai": """
const sample = JSON.parse(process.env.SAMPLE);
export class NoObjectGeneratedError extends Error {
  constructor(value) { super(value.message); Object.assign(this, value); }
  static isInstance(error) { return error instanceof NoObjectGeneratedError; }
}
export const jsonSchema = value => value;
export const tool = value => value;
export const Output = { object: value => value };
export async function generateText() {
  if (sample.error) throw sample.error.name === 'AI_NoObjectGeneratedError' ?
    new NoObjectGeneratedError(sample.error) : Object.assign(new Error(), sample.error);
  return sample.result;
}
export function streamText() {
  return { fullStream: (async function* () {
    for (const part of sample.parts) yield part;
    if (sample.background) {
      setImmediate(() => {
        if (sample.background === 'rejection') Promise.reject(new Error('orbite 🌕'));
        else throw new Error('orbite 🌕');
      });
      await new Promise(() => {});
    }
  })(),
  get output() {
    if (sample.outputError) return Promise.reject(new Error(sample.outputError));
    return Promise.resolve(null);
  } };
}
""",
        "@ai-sdk/openai": """
export function createOpenAI() { const model = id => id; model.chat = model; return model; }
""",
        "@ai-sdk/anthropic": "export const createAnthropic = () => id => id;",
        "@ai-sdk/google": "export const createGoogleGenerativeAI = () => id => id;",
        "@ai-sdk/openai-compatible": "export const createOpenAICompatible = () => id => id;",
    }
    for name, source in modules.items():
        path = shim / "node_modules" / name
        path.mkdir(parents=True)
        (path / "package.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "version": "1.0.0",
                    "type": "module",
                    "exports": {".": "./index.mjs", "./package.json": "./package.json"},
                }
            )
        )
        (path / "index.mjs").write_text(source)

    def run(sample, stream=True, output_schema=None):
        call = call_spec_document(find_cells(REPO, "stream.utf8", "gemini")[0].call_spec)
        call["stream"] = stream
        call["output_schema"] = output_schema
        result = subprocess.run(
            ["node", "shim.mjs"],
            cwd=shim,
            env={
                "PATH": os.environ["PATH"],
                "WIRETRUTH_PROVIDER": "gemini",
                "WIRETRUTH_BASE_URL_GEMINI": "http://127.0.0.1:1",
                "SAMPLE": json.dumps(sample),
            },
            input=json.dumps(call),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert result.stderr == ""
        return observation_from_document(json.loads(result.stdout))

    return run


YIELDED_ERROR = {"type": "error", "error": {"name": "AI_APICallError", "message": "busy"}}


@pytest.mark.parametrize(
    "terminal,completed,error",
    [
        ([], None, None),
        ([{"type": "finish", "finishReason": "other"}], True, None),
        ([YIELDED_ERROR], False, "api"),
        ([{"type": "abort"}], False, None),
        ([YIELDED_ERROR, {"type": "finish", "finishReason": "error"}], False, "api"),
    ],
)
def test_vercel_stream_completion_and_yielded_error_contract(vercel, terminal, completed, error):
    parts = [{"type": "text-delta", "text": "Café 🌕"}, *terminal]
    observation = vercel({"parts": parts})
    assert observation.outcome == "returned"
    assert observation.stream.completed is completed
    assert observation.text.value == "Café 🌕"
    assert (observation.error.category if observation.error else None) == error


def test_vercel_output_error_after_finish_keeps_the_stream_complete(vercel):
    observation = vercel(
        {"parts": [{"type": "finish", "finishReason": "length"}], "outputError": "JSON tronqué"},
        output_schema={"type": "object"},
    )
    assert observation.outcome == "raised"
    assert observation.stream.completed is True
    assert not observation.structured.returned_as_success


@pytest.mark.parametrize("documented", [False, True])
def test_vercel_error_finish_uses_only_documented_error_fields(vercel, documented):
    error = {
        "name": "AI_NoObjectGeneratedError" if documented else "DiagnosticError",
        "message": "incomplete JSON",
        "finishReason": "length",
        "text": '{"île":',
        "usage": {"inputTokens": 8, "outputTokens": 3, "totalTokens": 11},
    }
    observation = vercel({"error": error}, stream=False)
    assert observation.outcome == "raised"
    assert observation.finish.value == ("length" if documented else None)
    assert observation.finish.surface == ("primary" if documented else "absent")
    assert observation.usage.total == (11 if documented else None)
    assert not observation.structured.returned_as_success


def test_vercel_invalid_tool_input_stays_unparsed(vercel):
    partial = '{"city":"Zür'
    observation = vercel(
        {
            "parts": [
                {
                    "type": "tool-call",
                    "toolCallId": "call_1",
                    "toolName": "weather",
                    "input": partial,
                    "invalid": True,
                    "error": {"name": "AI_InvalidToolInputError", "message": "incomplete input"},
                },
                {"type": "finish", "finishReason": "length"},
            ]
        }
    )
    assert observation.stream.completed is True
    (call,) = observation.tool_calls
    assert call.arguments_raw == partial
    assert call.arguments is None and not call.arguments_parsed_ok
    assert observation.outcome == "returned" and observation.error.category == "validation"


@pytest.mark.parametrize("background", ["rejection", "exception"])
def test_vercel_background_failures_emit_one_observation(vercel, background):
    observation = vercel({"parts": [], "background": background})
    assert observation.outcome == "raised"
    assert observation.error.message == "orbite 🌕"
    assert observation.stream.completed is False
