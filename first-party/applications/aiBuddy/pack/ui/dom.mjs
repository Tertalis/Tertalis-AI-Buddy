// file: first-party/applications/aiBuddy/pack/ui/dom.mjs ; version: 1
/** Tiny DOM helpers: text only, never HTML strings. */
export function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (name === 'class') element.className = value;
    else if (name === 'dataset') Object.assign(element.dataset, value);
    else if (name.startsWith('on')) element.addEventListener(name.slice(2).toLowerCase(), value);
    else if (name in element && name !== 'list') element[name] = value;
    else element.setAttribute(name, value === true ? '' : String(value));
  }
  append(element, children);
  return element;
}
export function append(element, children) {
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}
export function replace(element, ...children) { element.replaceChildren(); return append(element, children); }
