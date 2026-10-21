import { readFileSync } from 'node:fs';
import { chat, toolDefinition } from '@tanstack/ai';
import { createOpenaiChat, createOpenaiChatCompletions } from '@tanstack/ai-openai';
import { openaiCompatible } from '@tanstack/ai-openai/compatible';
import { createAnthropicChat } from '@tanstack/ai-anthropic';
import { createGeminiChat } from '@tanstack/ai-gemini';
import { createOllamaChat } from '@tanstack/ai-ollama';
import { readCall, writeObservation, surface, classifyError } from '../common/observe.mjs';

const started = performance.now();
const call = readCall();
const manifest = JSON.parse(readFileSync('wiretruth-shim.json', 'utf8'));
const usageFields = {
  input: u => u.promptTokens,
  output: u => u.completionTokens,
  total: u => u.totalTokens,
  cached_input: u => u.promptTokensDetails?.cachedTokens,
  reasoning: u => u.completionTokensDetails?.reasoningTokens,
};
const obs = {
  format: 'wiretruth.observation/v1',
  library: {
    name: manifest.name,
    version: JSON.parse(readFileSync(new URL('node_modules/@tanstack/ai/package.json', import.meta.url), 'utf8')).version,
    language: 'javascript', api: 'chat',
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
const tools = new Map();
let written = false;
function write() {
  if (written) return;
  written = true;
  obs.elapsed_ms = performance.now() - started;
  writeObservation(obs);
}

function observeError(error, outcome = 'raised') {
  obs.outcome = outcome;
  obs.error = classifyError(error, manifest.error_map);
  if (call.stream && outcome === 'raised') obs.stream.completed ??= false;
}

function observe(part) {
  obs.stream.chunks_seen += 1;
  const meta = part.metadata?.tanstack;
  if (part.type === 'TEXT_MESSAGE_CONTENT') obs.text = surface((obs.text.value ?? '') + part.delta);
  if (part.type === 'RUN_FINISHED') {
    const reason = meta?.finishReason;
    obs.finish = {
      // finishReason is already normalized; the provider's own string isn't exposed.
      value: reason == null ? null : manifest.finish_map[reason] ?? 'other',
      raw: null, surface: reason == null ? 'absent' : 'primary',
      source: reason == null ? null : 'RUN_FINISHED.metadata.tanstack.finishReason',
    };
    obs.stream.completed ??= true;
  }
  if (part.usage != null) {
    for (const [key, get] of Object.entries(usageFields)) {
      const count = get(part.usage);
      obs.usage[key] = count ?? null;
      obs.usage.surface[key] = count == null ? 'absent' : 'primary';
    }
  }
  if (part.type === 'RUN_ERROR') {
    observeError({ name: part.code ?? 'RUN_ERROR', message: part.message }, 'returned');
    obs.stream.completed = false;
  }
  if (part.type === 'TOOL_CALL_RESULT' && meta?.state === 'output-error') {
    observeError({ name: 'TOOL_CALL_RESULT', message: part.content }, 'returned');
  }
  if (part.type === 'TOOL_CALL_START') {
    const tool = { id: part.toolCallId, name: part.toolCallName, arguments_raw: null,
      arguments: null, arguments_parsed_ok: false };
    tools.set(part.toolCallId, tool);
    obs.tool_calls.push(tool);
  }
  const tool = tools.get(part.toolCallId);
  if (tool && part.type === 'TOOL_CALL_ARGS') tool.arguments_raw = (tool.arguments_raw ?? '') + part.delta;
  if (tool && part.type === 'TOOL_CALL_END') {
    tool.arguments_raw = meta?.args ?? tool.arguments_raw;
    // Strictly decode the public argument string; never repair partial JSON.
    if (typeof tool.arguments_raw === 'string') {
      try { tool.arguments = JSON.parse(tool.arguments_raw); tool.arguments_parsed_ok = true; } catch {}
    }
  }
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
    throw new Error('tanstack-ai: provider not mapped');
  }
  const baseURL = process.env[`WIRETRUTH_BASE_URL_${call.provider.toUpperCase().replaceAll('-', '_')}`];
  const apiKey = process.env.WIRETRUTH_API_KEY;
  let adapter;
  // https://tanstack.com/ai/latest/docs/adapters/openai
  if (call.provider === 'openai-chat') adapter = createOpenaiChatCompletions(call.model, apiKey, { baseURL });
  if (call.provider === 'openai-responses') adapter = createOpenaiChat(call.model, apiKey, { baseURL });
  // https://tanstack.com/ai/latest/docs/adapters/openai-compatible
  if (call.provider === 'ollama-openai') {
    adapter = openaiCompatible({ name: 'ollama', baseURL, apiKey, models: [call.model] })(call.model);
  }
  // https://tanstack.com/ai/latest/docs/adapters/anthropic
  if (call.provider === 'anthropic') adapter = createAnthropicChat(call.model, apiKey, { baseURL });
  // https://tanstack.com/ai/latest/docs/adapters/gemini
  if (call.provider === 'gemini') adapter = createGeminiChat(call.model, apiKey, { baseURL });
  // https://tanstack.com/ai/latest/docs/adapters/ollama
  if (call.provider === 'ollama') adapter = createOllamaChat(call.model, { host: baseURL });
  const modelOptions = {};
  const tokenKey = {
    'openai-chat': 'max_tokens', 'openai-responses': 'max_output_tokens',
    'ollama-openai': 'max_tokens', anthropic: 'max_tokens', gemini: 'maxOutputTokens', ollama: 'num_predict',
  }[call.provider];
  if (call.max_output_tokens != null) modelOptions[tokenKey] = call.max_output_tokens;
  // https://tanstack.com/ai/latest/docs/api/ai#chat-options
  const options = {
    adapter, debug: false,
    modelOptions: call.provider === 'ollama' ? { options: modelOptions } : modelOptions,
    messages: call.messages.filter(message => message.role !== 'system'),
    systemPrompts: call.messages.filter(message => message.role === 'system').map(message => message.content),
  };
  if (call.reasoning != null) options.reasoning = call.reasoning.effort === 'none' ? 'off' : call.reasoning.effort;
  // https://tanstack.com/ai/latest/docs/api/ai#tooldefinition-config
  if (call.tools?.length) options.tools = call.tools.map(value => toolDefinition({
    name: value.name, description: value.description, inputSchema: value.parameters,
  }));
  if (call.tool_choice != null) options.toolChoice = typeof call.tool_choice === 'string' ? call.tool_choice :
    { type: 'tool', name: call.tool_choice.name };
  if (call.stream) {
    // https://tanstack.com/ai/latest/docs/api/ai#chat-options
    for await (const part of chat(options)) observe(part);
  } else {
    // https://tanstack.com/ai/latest/docs/api/ai#usage-examples
    obs.structured.value = await chat({ ...options, outputSchema: call.output_schema });
    obs.structured.returned_as_success = true;
  }
}

try { await run(); } catch (error) { observeError(error); }
write();
