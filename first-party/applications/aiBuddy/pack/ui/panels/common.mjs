// file: first-party/applications/aiBuddy/pack/ui/panels/common.mjs ; version: 1
/** Runs one Invocation and reports its outcome in a note element. */
export async function act(session, note, target, args = {}, options = {}) {
  if (note) { note.className = 'note'; note.textContent = 'sent...'; }
  try {
    const value = await session.invoke(target, args, options);
    if (note) note.textContent = '';
    return value;
  } catch (error) {
    if (note) {
      note.className = 'error';
      note.textContent = error.code === 'conflict' ? 'That breakpoint is no longer current; the view has been refreshed.'
        : error.code === 'unknown' ? 'Outcome unknown (the connection or host changed).' : (error.message || error.code);
    }
    throw error;
  }
}
export function time(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString();
}
