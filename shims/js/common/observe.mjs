import { readFileSync } from 'node:fs';

export function readCall() {
  return JSON.parse(readFileSync(0, 'utf8'));
}

export function writeObservation(observation) {
  process.stdout.write(`${JSON.stringify(observation)}\n`);
}

export function surface(value, kind = 'primary') {
  return { value: value ?? null, surface: value == null ? 'absent' : kind };
}

export function classifyError(error, errorMap) {
  const names = new Set([error?.name]);
  for (let proto = error; proto != null; proto = Object.getPrototypeOf(proto)) {
    names.add(proto.constructor?.name);
  }
  const message = String(error?.message ?? error);
  const frame = error?.stack?.split('\n').find(line => /^\s+at /.test(line));
  const relative = frame?.replaceAll('\\', '/').match(/\/node_modules\/(.+):(\d+):\d+\)?$/);
  const builtin = [Error, EvalError, RangeError, ReferenceError, SyntaxError, TypeError, URIError,
    AggregateError].some(type => error?.constructor === type);
  const entry = errorMap.find(item => names.has(item.type) &&
    (item.message_regex == null || new RegExp(item.message_regex).test(message)));
  return {
    type: error?.name ?? typeof error,
    message: message.slice(0, 500),
    category: entry?.category ?? (builtin && relative ? 'internal' : 'other'),
    retry_after: null,
    stack_top: relative ? `${relative[1]}:${relative[2]}` : null,
  };
}
