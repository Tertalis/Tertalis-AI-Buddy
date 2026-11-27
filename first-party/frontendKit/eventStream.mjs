// file: first-party/frontendKit/eventStream.mjs ; version: 1
/** Bounded text/event-stream framing over a fetch body reader (EventSource cannot send Authorization). */
export const MAX_EVENT = 1024 * 1024;

export class StreamFormatError extends Error {
  constructor() { super('invalid-stream'); this.code = 'invalid-stream'; }
}
/** Yields {event, data} with data parsed as JSON; comments and unknown fields are ignored. */
export async function* readEvents(reader, {limit = MAX_EVENT} = {}) {
  const decoder = new TextDecoder('utf-8', {fatal: true});
  let buffer = '', event = 'message', data = [], size = 0;
  const flush = function* () {
    if (data.length) {
      let parsed;
      try { parsed = JSON.parse(data.join('\n')); } catch { throw new StreamFormatError(); }
      yield {event, data: parsed};
    }
    event = 'message'; data = []; size = 0;
  };
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    try { buffer += decoder.decode(value, {stream: true}); } catch { throw new StreamFormatError(); }
    let index;
    while ((index = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, index).replace(/\r$/, ''); buffer = buffer.slice(index + 1);
      if (line === '') { yield* flush(); continue; }
      if (line.startsWith(':')) continue;
      const colon = line.indexOf(':');
      const field = colon < 0 ? line : line.slice(0, colon);
      const content = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '');
      if (field === 'event') event = content;
      else if (field === 'data') { size += content.length; if (size > limit) throw new StreamFormatError(); data.push(content); }
    }
    if (buffer.length > limit) throw new StreamFormatError();
  }
}
