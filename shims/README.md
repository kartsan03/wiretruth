# Shims

A shim calls one library and reports what it exposes. Run it from a repository containing `suite/`:

```sh
wiretruth run --shim shims/reference
wiretruth run --shim shims/liar --check-grades tests/expected-grades/liar.json
wiretruth run --lib any-llm
wiretruth run --lib any-llm --write-results
wiretruth run --lib any-llm --check-against
```

The runner reads `wiretruth-shim.json` in the shim directory and starts its `entry` argv there. A Python entry
starting with `python` uses a cached venv under `.wiretruth/envs/py/<name>-<version>/`, installed with uv from
the manifest's package pins. The built-in reference and liar shims keep the runner's interpreter.
`--lib NAME` selects `shims/*/NAME/`; use either `--lib` or `--shim`.

`--write-results` runs every cell and writes `results/<name>/<version>.json`, including the resolved packages;
it requires `--lib` and a checkout clean outside `results/`, and refuses runs with HARNESS cells.
`--check-against [FILE]` runs every cell and exits 1 if a grade or requirement status differs, using that
library version's results file by default; package drift is reported as a note without changing the exit code.

`wiretruth envs` lists current and stale environments. `--prune` removes stale ones; `--build NAME` builds or
reuses an environment and prints its interpreter path. Build before running without network access. Builds
are not locked, so don't build the same environment from two commands at once.

The shim reads one `wiretruth.call/v1` document from stdin, prints exactly one `wiretruth.observation/v1` document
to stdout and exits 0, including when its library raises. Unexpected exceptions belong in the observation too.
Logs go to stderr; the runner keeps them under `.wiretruth/runs/<UTC timestamp>/`, one file per cell.

A plain non-zero exit is a shim bug and grades HARNESS. A signal after a request reaches the replay server grades CRASH;
before any request it grades HARNESS. The default timeout is 20 seconds. On timeout the runner kills the process
group: the cell grades HANG if the last response was fully served, otherwise HARNESS. Invalid stdout, a wrong
endpoint or stream mode, or an observation with no request in the replay log (other than a `validation` or
`internal` error) grades HARNESS. See [Grading](../docs/grading.md) for the complete precedence rules.

The environment inherits only `PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TMPDIR`, `TZ` and `SYSTEMROOT`, when
set. The runner sets `WIRETRUTH_BASE_URL_<PROVIDER>` for every replay provider, uppercasing the id and replacing
hyphens with underscores. The current provider points at its local replay server; the others point at a reserved
port that refuses connections. It also sets `WIRETRUTH_API_KEY=wt-test-key`, `WIRETRUTH_PROVIDER` and both
`NO_PROXY` and `no_proxy` to `127.0.0.1,localhost`, then adds the manifest's `env` entries. Caller credentials,
provider URLs, proxies and SDK settings are not inherited.

`reference` uses raw HTTP and the provider truth extractor as an ideal client. `liar` shares its wire reader and
deliberately hides truncation, drops usage, reports partial structured output as success, swallows provider
errors and removes raw values. Their expected grades and every evaluated requirement status are checked in CI;
`--check-grades` runs every cell and exits 1 for any difference. Setup errors exit 2; a run without a comparison exits 0.

The [format reference](../docs/formats.md) defines the call spec, observation and manifest fields.
