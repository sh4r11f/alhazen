/* A fake browser page, just big enough to run the dashboard's renderer.
 *
 * dashboard.js is a classic browser script: it builds SVG and HTML with
 * document.createElement(NS), finds its own nodes again with a handful of
 * simple selectors, measures text on a canvas and paints camera frames into
 * one. This file stands in for exactly those DOM APIs and nothing more, so the
 * tests can run the real script in Node with no browser and no npm package.
 *
 * It is strict where being lenient would hide a bug: a selector it cannot
 * parse throws instead of matching nothing, and a canvas asked for anything
 * but a 2D context throws. A future change to dashboard.js that needs more of
 * the DOM then fails here, by name, rather than letting a test pass against an
 * empty result. Extend the fake when that happens; do not loosen it.
 *
 * What it does not model: layout (every width is whatever a test sets, and 0
 * otherwise, as for a detached element), CSS, events beyond storing and firing
 * listeners, and text nodes as separate children (see `textContent`).
 */

export const SVG_NS = 'http://www.w3.org/2000/svg';
export const HTML_NS = 'http://www.w3.org/1999/xhtml';

/* Text width without fonts: every character is this many pixels wide, so a
 * test can say exactly how wide a label is ("BROKE_FIXATION" is 84 px). */
export const CHAR_WIDTH_PX = 6;

/* One compound selector: an optional tag (or `*`) followed by any number of
 * `.class`, `[attr]` and `[attr="value"]` parts. That is every selector
 * dashboard.js uses; descendant combinators, pseudo-classes and lists are
 * refused below rather than half-supported. */
const SELECTOR = /^([a-zA-Z][\w-]*|\*)?((?:\.[\w-]+|\[[\w-]+(?:="[^"]*"|='[^']*')?\])*)$/;
const ATTRIBUTE_PART = /\[([\w-]+)(?:="([^"]*)"|='([^']*)')?\]/g;

/**
 * Turn a selector into a predicate over fake elements.
 *
 * Throws on anything outside the supported grammar: a fake that silently
 * matched nothing would make "the tooltip was never created" and "the fake
 * could not find the tooltip" look the same.
 */
export function compileSelector(selector) {
  const text = String(selector).trim();
  const match = SELECTOR.exec(text);
  if (!text || !match) {
    throw new Error(
      'the fake DOM cannot match the selector ' + JSON.stringify(selector) +
      ': it understands one compound selector (tag, .class, [attr], [attr="value"]).' +
      ' Extend tests/js/fake_dom.mjs if dashboard.js now needs more.',
    );
  }
  const tag = match[1] && match[1] !== '*' ? match[1].toLowerCase() : null;
  /* Classes are read with the attribute parts cut out first, so a dot inside
   * a quoted attribute value is not mistaken for a class. */
  const classes = [...match[2].replace(ATTRIBUTE_PART, '').matchAll(/\.([\w-]+)/g)].map((m) => m[1]);
  const attributes = [...match[2].matchAll(ATTRIBUTE_PART)].map((m) => ({
    name: m[1],
    value: m[2] !== undefined ? m[2] : m[3],
  }));
  return (element) =>
    (tag === null || element.localName.toLowerCase() === tag) &&
    classes.every((name) => element.classList.contains(name)) &&
    attributes.every((part) =>
      element.hasAttribute(part.name) &&
      (part.value === undefined || element.getAttribute(part.name) === part.value));
}

/* `element.dataset.fooBar` is the attribute `data-foo-bar` in a browser, in
 * both directions; the script writes one form and selects on the other
 * (`canvas.dataset.stream = '1'`, then `canvas.camera[data-stream]`). */
function datasetFor(element) {
  const attributeName = (key) => 'data-' + key.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());
  return new Proxy({}, {
    get(_, key) {
      if (typeof key !== 'string') return undefined;
      const value = element.getAttribute(attributeName(key));
      return value === null ? undefined : value;
    },
    set(_, key, value) {
      element.setAttribute(attributeName(key), value);
      return true;
    },
    has(_, key) {
      return typeof key === 'string' && element.hasAttribute(attributeName(key));
    },
    deleteProperty(_, key) {
      element.removeAttribute(attributeName(key));
      return true;
    },
  });
}

/* classList over the `class` attribute, which is what both `className = …`
 * (HTML elements) and `setAttribute('class', …)` (SVG elements) write. */
function classListFor(element) {
  const read = () => (element.getAttribute('class') || '').split(/\s+/).filter(Boolean);
  const write = (names) => element.setAttribute('class', names.join(' '));
  return {
    add: (...names) => write([...new Set([...read(), ...names])]),
    remove: (...names) => write(read().filter((name) => !names.includes(name))),
    contains: (name) => read().includes(name),
    toggle: (name) => {
      const had = read().includes(name);
      if (had) write(read().filter((other) => other !== name));
      else write([...read(), name]);
      return !had;
    },
  };
}

/** The 2D context the script asks a canvas for: text measurement, and the
 *  image calls a camera panel paints with (recorded for the tests to read). */
class FakeContext2D {
  constructor(canvas) {
    this.canvas = canvas;
    this.font = '10px sans-serif';
    this.fillStyle = '#000000';
    /* Every putImageData call, in order: a test reads the pixels painted. */
    this.painted = [];
  }

  measureText(text) {
    return { width: String(text).length * CHAR_WIDTH_PX };
  }

  createImageData(width, height) {
    return { width: width, height: height, data: new Uint8ClampedArray(width * height * 4) };
  }

  putImageData(image, x, y) {
    this.painted.push({ image: image, x: x, y: y });
  }

  fillRect() {}

  drawImage() {}
}

/**
 * One element, HTML or SVG. Attributes live in a Map; the properties the
 * script sets directly (`type`, `value`, `onclick`, `width` on a canvas…) are
 * plain JavaScript properties, as they are on a real element.
 */
export class FakeElement {
  constructor(document, localName, namespaceURI) {
    this.ownerDocument = document;
    this.namespaceURI = namespaceURI;
    this.localName = localName;
    /* A browser reports HTML tag names in capitals and SVG ones as written
     * (`linearGradient`, `text`); dashboard.js compares the SVG ones. */
    this.tagName = namespaceURI === SVG_NS ? localName : localName.toUpperCase();
    this.attributes = new Map();
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.listeners = new Map();
    /* Detached elements measure 0 in a browser, and the charts fall back to a
     * default width on 0; a test that cares sets these. */
    this.clientWidth = 0;
    this.clientHeight = 0;
    this.offsetWidth = 0;
    this.scrollTop = 0;
    /* This element's own text. Real text is a child node; kept apart here, it
     * reads back the same through `textContent` as long as text comes before
     * any child element, which is how the script always writes it. */
    this.ownText = '';
    this.dataset = datasetFor(this);
    this.classList = classListFor(this);
    this.context2d = null;
  }

  get parentElement() {
    return this.parentNode;
  }

  get firstChild() {
    return this.children[0] || null;
  }

  get className() {
    return this.getAttribute('class') || '';
  }

  set className(value) {
    this.setAttribute('class', value);
  }

  /* All the text under this element, in document order — what the reader of
   * the page would see. Setting it replaces every child, as in a browser. */
  get textContent() {
    return this.ownText + this.children.map((child) => child.textContent).join('');
  }

  set textContent(value) {
    this.replaceChildren();
    this.ownText = value === null || value === undefined ? '' : String(value);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  appendChild(child) {
    child.remove();
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.children;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentNode = null;
  }

  replaceChildren(...nodes) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this.ownText = '';
    nodes.forEach((node) => this.appendChild(node));
  }

  cloneNode(deep) {
    const copy = new FakeElement(this.ownerDocument, this.localName, this.namespaceURI);
    this.attributes.forEach((value, name) => copy.attributes.set(name, value));
    Object.assign(copy.style, this.style);
    if (deep) {
      copy.ownText = this.ownText;
      this.children.forEach((child) => copy.appendChild(child.cloneNode(true)));
    }
    return copy;
  }

  /** Every descendant (not the element itself) matching, in document order. */
  querySelectorAll(selector) {
    const matches = compileSelector(selector);
    const found = [];
    const walk = (node) => node.children.forEach((child) => {
      if (matches(child)) found.push(child);
      walk(child);
    });
    walk(this);
    return found;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  matches(selector) {
    return compileSelector(selector)(this);
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  removeEventListener(type, listener) {
    const list = this.listeners.get(type) || [];
    if (list.includes(listener)) list.splice(list.indexOf(listener), 1);
  }

  /** Call this element's listeners for `type` — the tests' stand-in for a
   *  pointer or keyboard event, which the fake never generates on its own. */
  fire(type, event) {
    (this.listeners.get(type) || []).forEach((listener) => listener(event || {}));
  }

  /* A click runs the element's `onclick` (how the script wires its buttons)
   * and is reported to the document, which is how a test sees a download. */
  click() {
    this.ownerDocument.clicked.push(this);
    if (this.ownerDocument.onElementClick) this.ownerDocument.onElementClick(this);
    if (typeof this.onclick === 'function') this.onclick({ target: this });
  }

  focus() {
    this.ownerDocument.activeElement = this;
  }

  getBoundingClientRect() {
    return {
      left: 0, top: 0, right: this.clientWidth, bottom: this.clientHeight,
      width: this.clientWidth, height: this.clientHeight,
    };
  }

  getContext(kind) {
    if (this.localName !== 'canvas' || kind !== '2d') {
      throw new Error('the fake DOM only gives a <canvas> a "2d" context, not <' +
        this.localName + '>.getContext(' + JSON.stringify(kind) + ')');
    }
    if (!this.context2d) this.context2d = new FakeContext2D(this);
    return this.context2d;
  }

  /* A canvas "encodes" to its own size as JSON: no pixels are drawn here, and
   * the size is what a figure-export test has to read back. */
  toBlob(callback, type) {
    callback(new Blob([JSON.stringify({ width: this.width, height: this.height })], { type: type }));
  }
}

/* The attributes a browser reflects from a boolean property: the script sets
 * `details.open = true` and `button.disabled = true`, and a selector such as
 * `details.table[open]` then matches on the attribute. */
for (const name of ['open', 'disabled']) {
  Object.defineProperty(FakeElement.prototype, name, {
    get() {
      return this.hasAttribute(name);
    },
    set(value) {
      if (value) this.setAttribute(name, '');
      else this.removeAttribute(name);
    },
  });
}

/** The document: a root <html> holding a <body>, and the element factories. */
export class FakeDocument {
  constructor() {
    /* Elements clicked, in order, and an optional hook told of each one as it
     * happens (see FakeElement.click). */
    this.clicked = [];
    this.onElementClick = null;
    this.documentElement = this.createElement('html');
    this.body = this.documentElement.appendChild(this.createElement('body'));
    this.scrollingElement = this.documentElement;
    this.activeElement = this.body;
  }

  createElement(name) {
    return new FakeElement(this, name, HTML_NS);
  }

  createElementNS(namespaceURI, name) {
    return new FakeElement(this, name, namespaceURI);
  }

  querySelectorAll(selector) {
    const matches = compileSelector(selector);
    return [this.documentElement, ...this.documentElement.querySelectorAll('*')].filter(matches);
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  getElementById(id) {
    return this.querySelectorAll('*').find((element) => element.getAttribute('id') === id) || null;
  }
}

const XML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };

/** An element and everything under it as XML text — what XMLSerializer gives
 *  the figure export, so a test can read the exported file's markup. */
export function serialize(element) {
  const escape = (text) => String(text).replace(/[&<>"]/g, (c) => XML_ESCAPES[c]);
  const attributes = [...element.attributes]
    .map(([name, value]) => ' ' + name + '="' + escape(value) + '"')
    .join('');
  const inner = escape(element.ownText) + element.children.map(serialize).join('');
  return '<' + element.localName + attributes + '>' + inner + '</' + element.localName + '>';
}
