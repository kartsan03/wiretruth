import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { generateText, streamText, Output, jsonSchema, tool, NoObjectGeneratedError } from 'ai';
import { createOpenAI } from '@ai-sdk/openai';
import { createAnthropic } from '@ai-sdk/anthropic';
import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { readCall, writeObservation, surface, classifyError } from '../common/observe.mjs';

const started = performance.now();
const call = readCall();
const manifest = JSON.parse(readFileSync('wiretruth-shim.json', 'utf8'));
const usageFields = {
  input: u => u.inputTokens,
  output: u => u.outputTokens,
  total: u => u.totalTokens,
  cached_input: u => u.inputTokenDetails?.cacheReadTokens,
  reasoning: u => u.outputTokenDetails?.reasoningTokens,
};
const obs = {
  format: 'wiretruth.observation/v1',
  library: {
    name: manifest.name,
    version: createRequire(import.meta.url)('ai/package.json').version,
    language: 'javascript',
    api: call.stream ? 'streamText' : 'generateText',
  },
  outcome: 'returned', unsupported_reason: null, error: null,
  finish: { value: null, raw: null, surface: 'absent', source: null },
  text: surface(null),
  structured: { attempted: call.output_schema != null, returned_as_success: false, value: null },
  tool_calls: [],
  usage: {
    ...Object.fromEntries(Object.keys(usageFields).map(key => [key, null])),
    surface: Object.fromEntries(Object.keys(usageFields).map(key => [key, 'absent'])),
  },
  stream: { used: call.stream, completed: null, chunks_seen: call.stream ? 0 : null },
  notes: [],
};
let written = false;
let requests = 0;
function write() {
  if (written) return;
  written = true;
  if (requests > 1) obs.notes.push(`retries: ${requests - 1} (${requests} model requests)`);
  obs.elapsed_ms = performance.now() - started;
  writeObservation(obs);
}

function finish(reason, raw, source) {
  obs.finish = {
    value: reason == null ? null : manifest.finish_map[reason] ?? null,
    raw: raw ?? null,
    surface: reason != null ? 'primary' : raw != null ? 'raw' : 'absent',
    source: reason != null || raw != null ? source : null,
  };
}

function usage(value) {
  for (const [key, get] of Object.entries(usageFields)) {
    const count = value == null ? null : get(value);
    obs.usage[key] = count ?? null;
    obs.usage.surface[key] = count == null ? 'absent' : 'primary';
  }
}

function observeError(error, outcome = 'raised') {
  obs.outcome = outcome;
  obs.error = classifyError(error, manifest.error_map);
  if (call.stream) obs.stream.completed = false;
  // https://ai-sdk.dev/docs/reference/ai-sdk-errors/ai-no-object-generated-error
  if (NoObjectGeneratedError.isInstance(error)) {
    finish(error.finishReason, null, 'NoObjectGeneratedError.finishReason');
    usage(error.usage);
    obs.text = surface(error.text);
  }
}

function toolCall(value) {
  const parsed = !value.invalid && value.input != null;
  obs.tool_calls.push({
    id: value.toolCallId ?? null,
    name: value.toolName ?? null,
    arguments_raw: value.input == null ? null : value.invalid && typeof value.input === 'string' ?
      value.input : JSON.stringify(value.input),
    arguments: parsed ? value.input : null,
    arguments_parsed_ok: parsed,
  });
  if (value.invalid) observeError(value.error, 'returned');
}

process.on('unhandledRejection', error => { observeError(error); write(); process.exit(0); });
process.on('uncaughtException', error => { observeError(error); write(); process.exit(0); });

async function run() {
  const unsupported = manifest.unsupported.find(entry => entry.scenario === call.scenario &&
    (entry.providers == null || entry.providers.includes(call.provider)));
  if (unsupported) {
    obs.outcome = 'unsupported';
    obs.unsupported_reason = `${unsupported.reason} ${unsupported.docs}`;
    obs.structured.attempted = false;
    return;
  }
  if (!(call.provider in manifest.providers) || call.provider !== process.env.WIRETRUTH_PROVIDER) {
    throw new Error('vercel-ai: provider not mapped');
  }
  const baseURL = process.env[`WIRETRUTH_BASE_URL_${call.provider.toUpperCase().replaceAll('-', '_')}`];
  const credentials = {
    baseURL, apiKey: process.env.WIRETRUTH_API_KEY,
    fetch: (...args) => { requests += 1; return fetch(...args); },
  };
  let model;
  // https://ai-sdk.dev/providers/ai-sdk-providers/openai
  if (call.provider === 'openai-chat') model = createOpenAI(credentials).chat(call.model);
  if (call.provider === 'openai-responses') model = createOpenAI(credentials)(call.model);
  // https://ai-sdk.dev/providers/ai-sdk-providers/anthropic
  if (call.provider === 'anthropic') {
    model = createAnthropic({ ...credentials, baseURL: `${baseURL}/v1` })(call.model);
  }
  // https://ai-sdk.dev/providers/ai-sdk-providers/google-generative-ai
  if (call.provider === 'gemini') {
    model = createGoogleGenerativeAI({ ...credentials, baseURL: `${baseURL}/v1beta` })(call.model);
  }
  // https://ai-sdk.dev/providers/openai-compatible-providers
  if (call.provider === 'ollama-openai') {
    model = createOpenAICompatible({
      name: 'ollama', baseURL, fetch: credentials.fetch,
      includeUsage: call.extra?.include_usage,
      ...(call.output_schema != null ? { supportsStructuredOutputs: true } : {}),
    })(call.model);
  }
  // https://ai-sdk.dev/docs/reference/ai-sdk-core/generate-text
  const options = {
    model,
    instructions: call.messages.filter(message => message.role === 'system'),
    messages: call.messages.filter(message => message.role !== 'system'),
  };
  if (call.max_output_tokens != null) options.maxOutputTokens = call.max_output_tokens;
  if (call.stop != null) options.stopSequences = call.stop;
  // https://ai-sdk.dev/docs/ai-sdk-core/reasoning
  if (call.reasoning != null) options.reasoning = call.reasoning.effort;
  // https://ai-sdk.dev/docs/ai-sdk-core/tools-and-tool-calling
  if (call.tools?.length) {
    options.tools = Object.fromEntries(call.tools.map(value => [value.name, tool({
      description: value.description, inputSchema: jsonSchema(value.parameters),
      ...(call.extra?.anthropic?.eager_input_streaming ? {
        providerOptions: { anthropic: { eagerInputStreaming: true } },
      } : {}),
    })]));
  }
  if (call.tool_choice != null) {
    options.toolChoice = typeof call.tool_choice === 'string' ? call.tool_choice :
      { type: 'tool', toolName: call.tool_choice.name };
  }
  // https://ai-sdk.dev/providers/ai-sdk-providers/anthropic#prompt-caching
  if (call.extra?.anthropic?.cache) {
    options.providerOptions = { anthropic: { cacheControl: { type: 'ephemeral' } } };
  }
  // https://ai-sdk.dev/docs/ai-sdk-core/generating-structured-data
  if (call.output_schema != null) options.output = Output.object({ schema: jsonSchema(call.output_schema) });
  if (call.stream) {
    // https://ai-sdk.dev/docs/ai-sdk-core/generating-text#streamtext
    const result = streamText(options);
    for await (const part of result.fullStream) {
      obs.stream.chunks_seen += 1;
      if (part.type === 'text-delta') obs.text = surface((obs.text.value ?? '') + part.text);
      if (part.type === 'tool-call') toolCall(part);
      if (part.type === 'finish-step') {
        finish(part.finishReason, part.rawFinishReason, 'fullStream.finish-step.finishReason');
        usage(part.usage);
      }
      if (part.type === 'finish') {
        finish(part.finishReason, part.rawFinishReason, 'fullStream.finish.finishReason');
        // The finish part also closes a stream that already yielded an error or abort part.
        obs.stream.completed ??= true;
      }
      if (part.type === 'error') observeError(part.error, 'returned');
      if (part.type === 'abort') obs.stream.completed = false;
    }
    if (call.output_schema != null && obs.error == null) {
      obs.structured.value = await result.output;
      obs.structured.returned_as_success = true;
    }
  } else {
    // https://ai-sdk.dev/docs/ai-sdk-core/generating-text#generatetext
    const result = await generateText(options);
    finish(result.finishReason, result.rawFinishReason, 'result.finishReason');
    obs.text = surface(result.text);
    usage(result.usage);
    for (const value of result.toolCalls) toolCall(value);
    if (call.output_schema != null) {
      obs.structured.value = result.output;
      obs.structured.returned_as_success = true;
    }
  }
}

try { await run(); } catch (error) { observeError(error); }
write();
