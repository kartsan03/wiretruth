# Writing a shim

This page is for library maintainers and contributors who want their library in the matrix. It takes you from an
empty directory to a results file: the rules a shim follows, what it reads and prints, a complete example, and how
to test and submit it. Read [Concepts](concepts.md) first for scenarios, fixtures, truth, surfaces and grades.

## How a cell runs

A cell is one scenario on one provider. For each cell, the runner starts a replay server loaded with that
fixture's recorded responses. It then starts your shim as a new process in the shim's directory, writes one call
spec to its stdin and waits for one observation on its stdout. The grade comes from comparing that observation and
the logged requests with the fixture's truth. The [grading page](grading.md) has the checks and the order of the
rules.

A shim lives in `shims/<language>/<name>/`: `shims/python/my-library/` or `shims/js/my-library/`. The directory
name is the `--lib` name; use the same name for the manifest `name` and the observation's `library.name`. The
directory holds the program and its manifest, `wiretruth-shim.json`.

## Rules

A shim MUST:

1. Read one `wiretruth.call/v1` document from stdin.
2. Call the library the way its docs show, for the provider in `WIRETRUTH_PROVIDER`, with the base URL and API key
   from the environment. Put the docs URL in a comment above each call.
3. Print exactly one `wiretruth.observation/v1` document to stdout and exit 0, also when the library raises.
4. Print nothing else to stdout. Logs go to stderr. If the library or a dependency writes to stdout, silence it with
   a documented option or redirect it (Python: `contextlib.redirect_stdout(sys.stderr)`).
5. Catch failures that escape the library call and report them in the observation, the same way as an exception
   from the call. In Python, set `threading.excepthook` and, if the library runs an asyncio event loop, the loop's
   exception handler; in JavaScript, handle `unhandledRejection` and `uncaughtException`. The example below needs
   neither, because the client it calls starts no threads or loops.
6. Send the request the fixture replays: the same endpoint, streamed or not, the same number of requests. A wrong
   endpoint or stream mode grades HARNESS, and so does a result with no request at all, unless it is a
   `validation` or `internal` error the library raised before sending. If the library's documented call can't send
   that request, the cell is unsupported (below).
7. Report only what the library exposes through its public API, each value with its surface (below).
8. Pin exact package versions in the manifest.

A shim MUST NOT:

- Work around the library: no patching, no private imports, no retries of your own (keep the library's built-in
  retries, which are a default), no catching and continuing, no repairing or re-parsing output the library gave up
  on, no copying raw values into primary fields.
- Change a library default unless it has to, and then only with a `config_notes` entry that says why
  ([decision 0006](adr/0006-default-library-config.md)).
- Read credentials, provider URLs or SDK settings from anywhere other than the variables below.

A non-zero exit is a shim bug and grades HARNESS, with the shim's stderr kept for the cell. While you develop, exit
non-zero for anything you haven't mapped yet. The finished shim maps every cell or marks it unsupported, because
`--write-results` refuses a run with HARNESS cells.

## Environment

The shim's environment holds only these variables, plus `PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TMPDIR`,
`TZ` and `SYSTEMROOT` from the runner's environment:

| Variable | Value |
|---|---|
| `WIRETRUTH_PROVIDER` | The provider id of this cell, such as `openai-chat` (the same as the call's `provider`) |
| `WIRETRUTH_BASE_URL_<PROVIDER>` | One per provider: the id upper-cased, `-` replaced by `_`, such as `WIRETRUTH_BASE_URL_OPENAI_CHAT` |
| `WIRETRUTH_API_KEY` | `wt-test-key`. Pass it the way the library takes an API key; replay ignores it |
| `NO_PROXY`, `no_proxy` | `127.0.0.1,localhost` |
| The manifest's `env` entries | Settings the library needs to run offline, each explained in `config_notes` |

Only the current provider's base URL reaches the replay server. The others point at a closed port, so a library
that calls the wrong provider gets a connection error. The base URL already contains the path prefix that each
provider's clients expect:

| Provider | Base URL | The library adds |
|---|---|---|
| `openai-chat`, `openai-responses`, `ollama-openai` | `http://127.0.0.1:<port>/v1` | `/chat/completions`, `/responses` |
| `anthropic` | `http://127.0.0.1:<port>` | `/v1/messages` |
| `gemini` | `http://127.0.0.1:<port>` | `/v1beta/models/<model>:generateContent` |
| `ollama` | `http://127.0.0.1:<port>` | `/api/chat` |

If the library wants a different form, say so in the manifest's `providers` entry.

## Reading the call

The [call spec](formats.md#call-spec--wiretruthcallv1) has every field, always present. Map each one to the
library's documented argument:

- `model` and `messages` always apply. `max_output_tokens`, `stop`, `tools`, `tool_choice`, `output_schema` and
  `reasoning` apply when they aren't `null` or empty.
- A field the library can't send makes the cell unsupported. Exception: if the library can't send a `reasoning`
  effort (often `none`), make the call without it and add a `notes` entry. The response is the same either way.
- If the library sends a field exactly as you pass it (the token-limit field, for example), the `request.*` cells
  grade your choice, not the library. Pass the field the provider's docs name, and say so in a `notes` entry.
- `output_schema` is a JSON Schema. Use the library's documented structured-output call, the one that parses the
  result. If that call takes a type instead of a schema (a Pydantic model, a zod schema), build the type from
  `output_schema`: that maps the input and leaves the output to the library. Cover the schema features the
  scenarios use and exit non-zero on anything else. `schema_type` in the
  [openai-python shim](../shims/python/openai-python/shim.py) does this with Pydantic's `create_model` in about
  20 lines.
- `extra` holds hints for one scenario, such as `{"include_usage": true}`. Pass a hint only when the library has a
  documented switch for it. Otherwise leave it out and add a `notes` entry.

**Unsupported.** When the library's documented call can't express a cell (a provider it doesn't speak, a setting it
can't send, a request shape it never makes, such as TanStack AI's `chat()`, which always streams), print an
observation with `outcome: "unsupported"` and an `unsupported_reason` that ends with a docs link. List the same
scenario and providers in the manifest's `unsupported`. Each entry names one scenario id exactly (no patterns);
`providers` limits it to some providers, and leaving it out covers them all. For a provider the library doesn't
speak, add one entry per scenario that lists that provider; the [scenario catalog](scenarios.md) shows which
scenarios each provider has. A cell the shim reports as unsupported but the manifest doesn't list grades HARNESS.
Describe the library's design, not a defect: "The SDK has no native Ollama client".

## Writing the observation

The [observation format](formats.md#observation--wiretruthobservationv1) lists every field. Start from an
observation with every field present and nothing observed (as in the example below), then fill in what the library
exposes. Each value has a surface:

- `primary`: the documented, typed result the caller is meant to use. Only primary values are graded as reported.
- `raw`: provider metadata the library passes through, such as a native response object.
- `absent`: the library doesn't expose it. Leave the value `null`.

How to fill the fields that need a decision:

- **`library`**: `name` is the shim's name, `version` the installed version of the manifest's `primary_package`,
  and `api` the entry point the shim called, such as `ollama.Client.chat`.
- **`finish`**: `value` is the library's value mapped through the manifest's `finish_map`; an unmapped value
  becomes `other`. `raw` is the provider's own string, such as `MAX_TOKENS`, and only when the library exposes it.
  If the library normalized it, `raw` is `null`. `source` is the attribute path you read. When the library spreads
  the finish over several fields (a status plus a reason, or a completed status whose output holds tool calls),
  read them in a fixed order, put the field you used in `source` and its string in `raw`, and describe the order
  in the manifest's `surfaces`.
- **Finish on an error.** When the library raises an error that carries a documented finish field (for example a
  length error with the partial completion attached), report it in `finish` with `surface: "primary"` and a
  `source` that names the error field, such as `error.completion.choices[0].finish_reason`.
- **`text`**: the full text. For a stream, join the text of each chunk.
- **`structured`**: `attempted` is true when the call has an `output_schema`. `returned_as_success` is true only
  when the library handed back a parsed value without an error; `value` is that value.
- **`tool_calls`**: `arguments_raw` is the arguments text the library exposes. `arguments_parsed_ok` and
  `arguments` come from parsing that text as JSON (`json.loads`, `JSON.parse`). In a stream, joining the
  documented deltas is reading, not repairing: concatenate each call's argument fragments by its index and keep its
  id and name. When the library also hands back the complete call (a done event, the final response), read that
  instead of adding it to the fragments.
- **`usage`**: the five counts, each with its own surface.
- **`stream`**: `chunks_seen` counts what the library yielded. `completed` is the library's own signal that the
  stream ended normally. If the library has one (a final event, a done flag, a completed status), report `true`
  when it arrives and `false` when the stream ends without it. Report `null` only when the library has no such
  signal, such as an iterator that consumes the end-of-stream marker itself and ends the same way without it. Don't
  set it to true because iteration ended without an exception, and don't count an event that closes one output item
  as the end of the stream.
- **`notes`**: anything the grader should know, such as a setting the library couldn't send. The run summary
  prints each note under its cell as `shim: <note>`, and the results file and the cell page keep them.

**Errors.** When the library raises, set `outcome: "raised"` and fill `error`:

- `type` is the fully qualified type. `category` comes from the manifest's `error_map`: the first entry whose
  `type` is the error's type or one of its base classes. `stack_top` is the raising frame's `file:line`, relative
  to `site-packages` (Python) or `node_modules` (JavaScript), such as `openai/_base_client.py:1042`; a frame in a
  dependency keeps that package's name.
- A built-in exception raised inside the library code (`KeyError`, `TypeError`) gets category `internal` and grades
  CRASH. If the library raises a built-in type on purpose, add an `error_map` entry with `message_regex` and a
  `source` link to the raising line.
- A schema validation error on a value the library didn't expect (pydantic, zod) gets category `validation`, never
  `internal`.
- An exception raised by the shim's own code is a shim bug: let it exit non-zero.
- When the library returns or yields an error object instead of raising, keep `outcome: "returned"` and fill
  `error`. A finish value of `error` alone isn't an error object.

## The manifest

`wiretruth-shim.json` says how to run the shim and documents what it reads. The
[manifest format](formats.md#shim-manifest--wiretruthshimv1) lists every field; `wiretruth lint` checks it. Keep
it short:

- `providers`: one line per provider with the client call and base URL argument.
- `surfaces`: every attribute the shim reads, per field. The cell pages on the results site show it.
- `finish_map`: every finish value the library produces, mapped to a finish value.
- `error_map`: the types a full run actually raises, plus error types the library documents. More specific types
  come first, because the first match wins.
- `config_notes`: each change from the library's defaults and why; also call fields the shim leaves unmapped
  because no cell uses them.
- `docs`: the pages the shim's calls come from. Before you submit, check that every link opens (`curl -sL`) and
  that each `#anchor` names a heading on that page; a page that opens can still have a dead anchor.

## A full example

This shim reports what the [ollama Python client](https://github.com/ollama/ollama-python) returns for text
calls on the native Ollama API. It covers the plain and streamed text cells and exits non-zero for everything else.
Save the two files in one directory.

`wiretruth-shim.json`:

```json
{
  "format": "wiretruth.shim/v1",
  "name": "example",
  "language": "python",
  "packages": {"ollama": "0.6.3"},
  "primary_package": "ollama",
  "entry": ["python", "shim.py"],
  "providers": {"ollama": "ollama.Client(host=WIRETRUTH_BASE_URL_OLLAMA).chat(model, messages, ...)"},
  "surfaces": {
    "finish": {"primary": "done_reason, mapped by finish_map; the raw value is the same string"},
    "text": {"primary": "message.content; streams join each chunk's message.content"},
    "usage": {"primary": "prompt_eval_count and eval_count"},
    "stream": {"primary": "done on the last chunk"}
  },
  "finish_map": {"stop": "stop", "length": "length"},
  "error_map": [
    {"type": "ollama._types.ResponseError", "category": "api"},
    {"type": "httpx.TransportError", "category": "network"}
  ],
  "unsupported": [],
  "config_notes": [],
  "docs": ["https://github.com/ollama/ollama-python"]
}
```

`shim.py`:

```python
import json
import os
import sys
import time
import traceback
from importlib.metadata import version
from pathlib import Path

import ollama

started = time.monotonic()
call = json.load(sys.stdin)
manifest = json.loads(Path("wiretruth-shim.json").read_text())
if call["provider"] != "ollama" or call["tools"] or call["output_schema"] or call["stop"]:
    sys.exit("example: not mapped yet")  # a non-zero exit grades HARNESS
FIELDS = ("input", "cached_input", "output", "reasoning", "total")
obs = {
    "format": "wiretruth.observation/v1",
    "library": dict(
        name="example", version=version("ollama"), language="python", api="ollama.Client.chat"
    ),
    "outcome": "returned",
    "unsupported_reason": None,
    "error": None,
    "finish": dict(value=None, raw=None, surface="absent", source=None),
    "text": dict(value=None, surface="absent"),
    "structured": dict(attempted=False, returned_as_success=False, value=None),
    "tool_calls": [],
    "usage": {**dict.fromkeys(FIELDS), "surface": dict.fromkeys(FIELDS, "absent")},
    "stream": dict(used=call["stream"], completed=None, chunks_seen=0 if call["stream"] else None),
    "notes": [],
}


def observe(response):
    if response.message.content:
        text = obs["text"]
        text.update(value=(text["value"] or "") + response.message.content, surface="primary")
    if response.done_reason is not None:
        value = manifest["finish_map"].get(response.done_reason, "other")
        obs["finish"].update(
            value=value, raw=response.done_reason, surface="primary", source="done_reason"
        )
    for name, attr in (("input", "prompt_eval_count"), ("output", "eval_count")):
        if getattr(response, attr) is not None:
            obs["usage"][name] = getattr(response, attr)
            obs["usage"]["surface"][name] = "primary"


try:
    # https://github.com/ollama/ollama-python#custom-client and #streaming-responses
    client = ollama.Client(host=os.environ["WIRETRUTH_BASE_URL_OLLAMA"])
    options = {"num_predict": call["max_output_tokens"]} if call["max_output_tokens"] else None
    think = False if (call["reasoning"] or {}).get("effort") == "none" else None
    result = client.chat(
        call["model"], call["messages"], stream=call["stream"], options=options, think=think
    )
    if call["stream"]:
        for chunk in result:
            obs["stream"]["chunks_seen"] += 1
            obs["stream"]["completed"] = chunk.done  # the library's own signal
            observe(chunk)
    else:
        observe(result)
except Exception as error:
    frame = traceback.extract_tb(error.__traceback__)[-1]
    if frame.filename == __file__:
        raise  # a bug in this shim, not in the library: the non-zero exit grades HARNESS
    types = [f"{cls.__module__}.{cls.__qualname__}" for cls in type(error).__mro__]
    default = "internal" if type(error).__module__ == "builtins" else "other"
    category = next((e["category"] for e in manifest["error_map"] if e["type"] in types), default)
    relative = frame.filename.partition("/site-packages/")[2]
    obs["outcome"] = "raised"
    obs["error"] = dict(
        type=types[0],
        message=str(error)[:500],
        category=category,
        retry_after=None,
        stack_top=f"{relative}:{frame.lineno}" if relative else None,
    )
    if call["stream"]:
        obs["stream"]["completed"] = False
obs["elapsed_ms"] = (time.monotonic() - started) * 1000
print(json.dumps(obs))
```

Run it from the root of a wiretruth checkout, with the path to that directory:

```sh
uv run wiretruth run --shim path/to/example --provider ollama \
  --scenario truncation.text,truncation.text-stream,stream.missing-finish,finish.stop-sequence
```

```text
scenario                ollama
finish.stop-sequence    ? HARNESS
stream.missing-finish   ✓ PASS
truncation.text         ✓ PASS
truncation.text-stream  ✓ PASS

finish.stop-sequence ollama
  the shim ended without an observation
  the shim exited with code 1

example 0.6.3: 4 cells, 3 PASS, 1 HARNESS
stderr: .wiretruth/runs/20261117T022334.286246Z
```

`finish.stop-sequence` sends `stop`, which this example doesn't map. A real shim would pass `options.stop` and
cover tools, structured output and the other providers its library speaks. The
[openai-python shim](../shims/python/openai-python/) is a complete Python shim built from this page; the
[Vercel AI SDK shim](../shims/js/vercel-ai/) is a complete JavaScript one.

## JavaScript shims

A JavaScript shim uses `"entry": ["node", "shim.mjs"]` and runs with Node.js from `PATH`. Next to the manifest it
needs `package.json`, whose `dependencies` must equal the manifest's `packages`, and a `package-lock.json`. The
runner installs them with `npm ci --ignore-scripts`. Never use `npx` or `npm exec`.

## Testing locally

You need Python 3.11 or newer, [uv](https://docs.astral.sh/uv/) and, for JavaScript, Node.js 22.

```sh
git clone https://github.com/kartsan03/wiretruth && cd wiretruth
uv sync --extra dev
uv run wiretruth envs --build my-library        # installs the manifest's packages; needs the network
uv run wiretruth run --lib my-library --scenario truncation.text --provider openai-chat
uv run wiretruth run --lib my-library           # every cell
uv run wiretruth lint                           # checks the manifest and results files
```

The first run of a Python shim builds its environment under `.wiretruth/envs/`, and later runs reuse it until
`packages` change. `--scenario` takes a comma-separated list or globs such as `'stream.*'`.

The summary prints a grade per cell and, below it, every requirement that isn't satisfied, with the observed
value next to the truth, followed by your `shim: …` notes. Each cell's stderr is kept in
`.wiretruth/runs/<timestamp>/<provider>.<scenario>.stderr`, so printing the observation to stderr while you work
shows exactly what the grader got. To call your library by hand against one fixture, serve it on its own:

```sh
uv run wiretruth serve ollama/truncation.text --port 8400
```

A shim times out after 20 seconds. The cell grades HANG if the response had been fully served, HARNESS otherwise.

## Submitting

Open one pull request with the title `feat(shims): <name>`. It contains:

1. The shim and its manifest, with `wiretruth lint` passing.
2. The library in the `shim:` list of `.github/workflows/conformance.yml`, so CI checks its grades on every change.
3. The results: commit everything else first, then run `uv run wiretruth run --lib <name> --write-results` and
   commit `results/<name>/<version>.json`. It refuses a checkout with uncommitted changes outside `results/` and a
   run with HARNESS cells.
4. A line in `CHANGELOG.md` under `Unreleased`.

A WRONG, CRASH or HANG cell reaches `main` only after the library has a public report of it. Open an issue in the
library's repository (or link an existing one), then add it to `results/upstream.json`
([format](formats.md#upstream-tracking)). If you maintain the library, a fix or a note that the behavior is
intended works as well. Say in the pull request which cells you reported.
