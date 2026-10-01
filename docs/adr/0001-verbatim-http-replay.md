# 0001. Verbatim HTTP replay

- Status: Accepted
- Date: 2026-10-01

## Context

The bugs wiretruth looks for live in the code that turns provider bytes into a library's result. That code
includes server-sent event framing, chunk boundaries inside multi-byte characters, finish-reason mapping, usage
arithmetic and stream completion. A test double that re-encodes the response, or patches the library above its
HTTP client, skips exactly that code. The libraries under test are written in Python and JavaScript and use
different HTTP stacks (httpx, aiohttp, requests, fetch). Some scenarios need control over individual bytes: where a
chunk ends, or whether the connection closes without the final chunk.

## Decision

A small HTTP server built on the Python standard library replays recorded responses on `127.0.0.1`. Each library
is pointed at it through its documented base URL option, so the library runs its real HTTP client and parser.

- Stream events are stored and sent exactly as the provider sent them, over HTTP/1.1 chunked transfer encoding,
  one chunk per event, flushed with a short gap between events.
- JSON bodies are stored as parsed JSON with the key order kept, and sent with `Content-Length`. Whitespace and
  escapes can differ from the original; the hash of the original bytes stays in the fixture's provenance.
- When exact bytes or chunk boundaries matter, a fixture gives the raw byte chunks instead, and it can end with an
  aborted connection instead of the final chunk.
- The server logs every request it receives (with credentials removed), so checks can look at what the library
  sent.

## Consequences

- One server serves every language, and adding a library needs no new replay code.
- Libraries are tested through their real network and parsing paths.
- Only libraries that accept a custom base URL can be tested. Most do. When one doesn't, the shim reports the
  provider as unsupported.
- The server controls how bytes are framed and flushed, not how the client's reader groups them. A client can read
  two chunks in one call, so a library could pass a chunk-boundary scenario only because the split never reached
  it. Flushing each chunk with a gap makes separate reads likely, not certain. A correct library passes either way.
- The server has to be tested to show it sends exactly the fixture's events and bytes: its tests compare what a raw
  socket client receives with the fixture.
- Timing is not modeled beyond the small gap between events.

## Alternatives considered

- **HTTP interception or record-and-replay inside the library's process (vcrpy, pytest-recording, msw):** one
  implementation per language, it hooks into each library's HTTP layer, and it gives no control over chunk
  boundaries or aborted connections.
- **A mock server that renders responses from a model of the API (such as aimock):** it re-encodes streams, which
  is the layer we want to test. aimock is a good fit for application tests, which is a different job.
- **Calling the live APIs in CI:** needs keys, costs money, and isn't reproducible, because the same request
  returns different bytes each time.
