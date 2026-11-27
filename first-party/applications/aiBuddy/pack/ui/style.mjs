// file: first-party/applications/aiBuddy/pack/ui/style.mjs ; version: 1
/** Styles as a constructed stylesheet (no inline style, no extra asset kind). */
const CSS = `
:root { color-scheme: light dark; --bg: Canvas; --fg: CanvasText; --line: color-mix(in srgb, CanvasText 18%, transparent);
  --muted: color-mix(in srgb, CanvasText 60%, transparent); --accent: #3b6fd6; --warn: #b7791f; --live: #2f855a; --bad: #c53030;
  font: 14px/1.45 system-ui, sans-serif; }
body { margin: 0; background: var(--bg); color: var(--fg); }
.app { display: grid; grid-template-columns: minmax(320px, 1fr) minmax(320px, 1fr); grid-template-rows: auto 1fr; gap: 12px;
  height: 100vh; box-sizing: border-box; padding: 12px; }
header { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
header h1 { font-size: 18px; margin: 0 12px 0 0; }
.badges { display: inline-flex; gap: 6px; }
.badge { border: 1px solid var(--line); border-radius: 999px; padding: 1px 8px; font-size: 12px; }
.badge.live { color: var(--live); border-color: var(--live); } .badge.warn { color: var(--warn); border-color: var(--warn); }
.badge.muted { color: var(--muted); } .badge.bad { color: var(--bad); border-color: var(--bad); }
.spacer { flex: 1; }
button { font: inherit; padding: 4px 10px; border-radius: 6px; border: 1px solid var(--line); background: transparent; color: inherit; cursor: pointer; }
button.primary { background: var(--accent); border-color: var(--accent); color: white; }
button:disabled { opacity: .5; cursor: default; }
.column { display: flex; flex-direction: column; gap: 12px; min-height: 0; overflow: auto; }
section { border: 1px solid var(--line); border-radius: 8px; padding: 10px; flex: none; }
section h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 0 0 8px; }
.conversation { display: flex; flex-direction: column; flex: 1 0 320px; min-height: 320px; }
.messages { flex: 1; overflow: auto; display: flex; flex-direction: column; gap: 6px; padding-bottom: 6px; }
.message { max-width: 85%; padding: 6px 10px; border-radius: 10px; white-space: pre-wrap; }
.message.user { align-self: flex-end; background: color-mix(in srgb, var(--accent) 18%, transparent); }
.message.buddy { align-self: flex-start; background: color-mix(in srgb, CanvasText 8%, transparent); }
.message time { display: block; font-size: 11px; color: var(--muted); }
form.say { display: flex; gap: 8px; } form.say textarea { flex: 1; }
textarea, select, input { font: inherit; color: inherit; background: transparent; border: 1px solid var(--line); border-radius: 6px; padding: 6px; }
textarea.code, pre { font: 12px/1.4 ui-monospace, monospace; }
pre { white-space: pre-wrap; margin: 0; max-height: 320px; overflow: auto; }
.breakpoint textarea { width: 100%; box-sizing: border-box; min-height: 220px; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 6px; }
.note { color: var(--muted); font-size: 12px; } .error { color: var(--bad); font-size: 12px; }
table { border-collapse: collapse; width: 100%; } td, th { text-align: left; padding: 2px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }
.board { font: 16px/1.2 ui-monospace, monospace; letter-spacing: .2em; }
.diagnostics ol { list-style: none; margin: 0; padding: 0; max-height: 240px; overflow: auto; font: 12px/1.4 ui-monospace, monospace; }
.diagnostics li time { color: var(--muted); margin-right: 6px; }
.hidden { display: none; }
#tool-select, #tool-payload { display: block; width: 100%; box-sizing: border-box; margin-bottom: 6px; }
@media (max-width: 800px) { .app { grid-template-columns: 1fr; height: auto; } }
`;
export function adoptStyles(document) {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(CSS);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
}
