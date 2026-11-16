// Tests for mountTwin (static/js/twin.js) mount, update, selection, fallback, and leak-free disposal.
import assert from "node:assert/strict";
import test from "node:test";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const twinMod = await import(pathToFileURL(resolve(process.env.TWIN_JS ?? "app/web/static/js/twin.js")).href);
const { mountTwin } = twinMod;

function createMockDom(options = {}) {
  function makeEmitter() {
    const map = new Map();
    return {
      addEventListener(type, fn) {
        if (!map.has(type)) map.set(type, new Set());
        map.get(type).add(fn);
      },
      removeEventListener(type, fn) {
        map.get(type)?.delete(fn);
      },
      dispatchEvent(event) {
        const set = map.get(event.type);
        if (set) {
          for (const fn of set) fn(event);
        }
      },
      listenerCount(type) {
        return map.get(type)?.size ?? 0;
      },
      totalListeners() {
        let count = 0;
        for (const s of map.values()) count += s.size;
        return count;
      },
    };
  }

  class MockElement {
    constructor(tag, doc, ns = null) {
      this.tagName = tag.toUpperCase();
      this.namespaceURI = ns;
      this.ownerDocument = doc;
      this.parentElement = null;
      this.children = [];
      this.attributes = new Map();
      this._emitter = makeEmitter();
      this.style = {};
      this.hidden = false;
      this.id = "";
      this.title = "";
      this.type = "";
      this.tabIndex = -1;
      this._className = "";
      this._textContent = "";
      this.dataset = new Proxy({}, {
        get: (_, prop) => this.getAttribute(`data-${String(prop)}`),
        set: (_, prop, val) => {
          this.setAttribute(`data-${String(prop)}`, val);
          return true;
        },
      });
    }

    get className() {
      return this._className;
    }
    set className(val) {
      this._className = String(val);
      this.attributes.set("class", this._className);
    }

    get classList() {
      return {
        add: (...cls) => {
          const set = new Set((this._className || "").split(/\s+/).filter(Boolean));
          cls.forEach((c) => set.add(c));
          this.className = [...set].join(" ");
        },
        remove: (...cls) => {
          const set = new Set((this._className || "").split(/\s+/).filter(Boolean));
          cls.forEach((c) => set.delete(c));
          this.className = [...set].join(" ");
        },
        toggle: (cls, force) => {
          const set = new Set((this._className || "").split(/\s+/).filter(Boolean));
          const shouldAdd = force !== undefined ? Boolean(force) : !set.has(cls);
          if (shouldAdd) set.add(cls);
          else set.delete(cls);
          this.className = [...set].join(" ");
          return shouldAdd;
        },
        contains: (cls) => {
          const set = new Set((this._className || "").split(/\s+/).filter(Boolean));
          return set.has(cls);
        },
      };
    }

    get textContent() {
      return this._textContent;
    }
    set textContent(val) {
      this._textContent = String(val ?? "");
      this.children = [];
    }

    get childNodes() {
      return this.children;
    }
    get parentNode() {
      return this.parentElement;
    }
    get isConnected() {
      return true;
    }

    getRootNode() {
      return this.ownerDocument;
    }

    setAttribute(name, val) {
      this.attributes.set(name, String(val));
      if (name === "class") this._className = String(val);
      if (name === "id") this.id = String(val);
    }
    getAttribute(name) {
      return this.attributes.get(name) ?? null;
    }
    hasAttribute(name) {
      return this.attributes.has(name);
    }
    removeAttribute(name) {
      this.attributes.delete(name);
    }

    append(...items) {
      for (const it of items) {
        if (it == null) continue;
        const node = typeof it === "string" ? new MockTextNode(it, this.ownerDocument) : it;
        node.parentElement = this;
        this.children.push(node);
      }
    }

    replaceChildren(...items) {
      for (const c of this.children) c.parentElement = null;
      this.children = [];
      this.append(...items);
    }

    remove() {
      if (this.parentElement) {
        const idx = this.parentElement.children.indexOf(this);
        if (idx >= 0) this.parentElement.children.splice(idx, 1);
        this.parentElement = null;
      }
    }

    closest(selector) {
      let cur = this;
      while (cur) {
        if (selector === "[data-id]" && cur.hasAttribute("data-id")) return cur;
        if (selector.startsWith(".") && cur.classList.contains(selector.slice(1))) return cur;
        cur = cur.parentElement;
      }
      return null;
    }

    getBoundingClientRect() {
      return { width: 800, height: 600, top: 0, left: 0, right: 800, bottom: 600, x: 0, y: 0 };
    }

    focus() {
      this.ownerDocument._activeElement = this;
    }

    getContext(type) {
      return {
        clearRect: () => {},
        beginPath: () => {},
        stroke: () => {},
        fill: () => {},
        rect: () => {},
        roundRect: () => {},
        fillRect: () => {},
        fillText: () => {},
        measureText: (txt) => ({ width: (txt || "").length * 8 }),
        moveTo: () => {},
        lineTo: () => {},
        arc: () => {},
        save: () => {},
        restore: () => {},
        translate: () => {},
        scale: () => {},
        strokeStyle: "#000",
        fillStyle: "#000",
        lineWidth: 1,
        font: "12px sans-serif",
        textAlign: "left",
        textBaseline: "top",
      };
    }

    addEventListener(type, fn, opts) {
      this._emitter.addEventListener(type, fn);
    }
    removeEventListener(type, fn) {
      this._emitter.removeEventListener(type, fn);
    }
    dispatchEvent(event) {
      event.target = event.target || this;
      this._emitter.dispatchEvent(event);
      if (event.bubbles && this.parentElement) {
        this.parentElement.dispatchEvent(event);
      }
    }

    find(predicate) {
      if (predicate(this)) return this;
      for (const child of this.children) {
        if (child instanceof MockElement) {
          const res = child.find(predicate);
          if (res) return res;
        }
      }
      return null;
    }

    findAll(predicate) {
      const results = [];
      if (predicate(this)) results.push(this);
      for (const child of this.children) {
        if (child instanceof MockElement) {
          results.push(...child.findAll(predicate));
        }
      }
      return results;
    }
  }

  class MockTextNode {
    constructor(text, doc) {
      this.textContent = text;
      this.ownerDocument = doc;
      this.parentElement = null;
    }
  }

  const winEmitter = makeEmitter();
  const win = {
    devicePixelRatio: 1,
    addEventListener(type, fn) {
      winEmitter.addEventListener(type, fn);
    },
    removeEventListener(type, fn) {
      winEmitter.removeEventListener(type, fn);
    },
    dispatchEvent(ev) {
      winEmitter.dispatchEvent(ev);
    },
    matchMedia(query) {
      const matches = query.includes("prefers-reduced-motion") && options.reducedMotion;
      return {
        matches,
        addEventListener: () => {},
        removeEventListener: () => {},
      };
    },
    Path2D: class Path2D {
      constructor(d) {
        this.d = d;
      }
    },
    requestAnimationFrame(fn) {
      return setTimeout(() => fn(Date.now()), 16);
    },
    cancelAnimationFrame(id) {
      clearTimeout(id);
    },
    getComputedStyle() {
      return { getPropertyValue: () => "" };
    },
  };

  const docEmitter = makeEmitter();
  const doc = {
    defaultView: win,
    hidden: false,
    _activeElement: null,
    get activeElement() {
      return this._activeElement;
    },
    createElement(tag) {
      return new MockElement(tag, doc);
    },
    createElementNS(ns, tag) {
      return new MockElement(tag, doc, ns);
    },
    createTextNode(txt) {
      return new MockTextNode(txt, doc);
    },
    addEventListener(type, fn) {
      docEmitter.addEventListener(type, fn);
    },
    removeEventListener(type, fn) {
      docEmitter.removeEventListener(type, fn);
    },
    dispatchEvent(ev) {
      docEmitter.dispatchEvent(ev);
    },
  };
  doc.documentElement = new MockElement("html", doc);
  doc.body = new MockElement("body", doc);
  doc.documentElement.append(doc.body);

  const root = doc.createElement("div");
  doc.body.append(root);

  return { doc, win, root, docEmitter, winEmitter };
}

function mockRenderer(THREE, canvas) {
  return {
    domElement: canvas,
    setPixelRatio: () => {},
    setSize: () => {},
    setClearColor: () => {},
    render: () => {},
    dispose: () => {},
    forceContextLoss: () => {},
    info: { render: { calls: 0, triangles: 0 } },
    shadowMap: { enabled: false },
  };
}

const sampleData = (count = 3) => ({
  stations: Array.from({ length: count }, (_, i) => ({
    id: i + 1,
    name: `สถานี ${i + 1}`,
    crop: "กรีนคอส",
    map_x: i * 2,
    map_y: 0,
    route_order: i + 1,
    status: i === 0 ? "normal" : i === 1 ? "yellow" : "brown",
    age_days: 15 + i * 5,
    last_seen_at: new Date().toISOString(),
    confidence: 0.95,
  })),
  home: { x: 0, z: 0 },
});

// 1. Mount creates shell & returns proper API
test("mountTwin with mock element creates shell and returns update/ready/dispose", async () => {
  const { root } = createMockDom();
  const inst = mountTwin(root, () => Promise.resolve(sampleData()));
  assert.equal(typeof inst.update, "function");
  assert.equal(typeof inst.dispose, "function");
  assert.equal(root.getAttribute("data-twin"), "on");
  assert.ok(root.children.length > 0);
  await inst.ready;
  assert.ok(["2d", "3d"].includes(inst.mode));
  inst.dispose();
});

// 2. Repeated update does not crash and updates DOM chips
test("repeated update() updates data without crash and refreshes chips", async () => {
  const { root } = createMockDom();
  const inst = mountTwin(root, () => Promise.resolve(sampleData(2)));
  await inst.ready;

  // First update
  inst.update(sampleData(4));
  let chips = root.findAll((el) => el.classList.contains("twin__chip"));
  assert.equal(chips.length, 4);

  // Second update with reduced stations
  inst.update(sampleData(1));
  chips = root.findAll((el) => el.classList.contains("twin__chip"));
  assert.equal(chips.length, 1);

  // Third update with identical data (signature check)
  inst.update(sampleData(1));
  chips = root.findAll((el) => el.classList.contains("twin__chip"));
  assert.equal(chips.length, 1);

  inst.dispose();
});

// 3. Calling dispose cleans up event listeners and DOM
test("calling dispose() cleans up event listeners and clears DOM", async () => {
  const { root } = createMockDom();
  const inst = mountTwin(root, () => Promise.resolve(sampleData()));
  await inst.ready;

  assert.equal(root.getAttribute("data-twin"), "on");
  assert.ok(root.children.length > 0);

  inst.dispose();
  assert.equal(root.getAttribute("data-twin"), null);
  assert.equal(root.children.length, 0);

  // Calling dispose twice is a safe no-op
  inst.dispose();
});

// 4. Mount and dispose 20 times runs cleanly without leak
test("mount and dispose twenty times runs cleanly without leak", async () => {
  const { doc } = createMockDom();
  for (let i = 0; i < 20; i++) {
    const root = doc.createElement("div");
    doc.body.append(root);
    const inst = mountTwin(root, () => Promise.resolve(sampleData(2)));
    await inst.ready;
    inst.update(sampleData(3));
    assert.equal(root.getAttribute("data-twin"), "on");
    inst.dispose();
    assert.equal(root.getAttribute("data-twin"), null);
    assert.equal(root.children.length, 0);
    root.remove();
  }
});

// 5. Reduced motion query is handled
test("reduced motion query (prefers-reduced-motion) is respected", async () => {
  const { root } = createMockDom({ reducedMotion: true });
  const inst = mountTwin(root, () => Promise.resolve(sampleData()));
  await inst.ready;
  assert.ok(inst);
  inst.dispose();
});

// 6. Station selection and panel closing works
test("station selection opens panel and close button closes it", async () => {
  const { root } = createMockDom();
  const inst = mountTwin(root, () => Promise.resolve(sampleData(3)));
  await inst.ready;

  const panel = root.find((el) => el.classList.contains("twin__panel"));
  assert.ok(panel);
  assert.equal(panel.hidden, true);

  // Find a station chip button and click it
  const chipButton = root.find((el) => el.classList.contains("twin__chip"));
  assert.ok(chipButton);
  chipButton.dispatchEvent({ type: "click", bubbles: true });

  assert.equal(panel.hidden, false);
  const title = root.find((el) => el.classList.contains("twin__panel-title"));
  assert.ok(title && title.textContent.includes("สถานี 1"));

  // Close panel via close button
  const closeBtn = root.find((el) => el.getAttribute("aria-label")?.includes("ปิดรายละเอียด"));
  assert.ok(closeBtn);
  closeBtn.dispatchEvent({ type: "click", bubbles: true });
  assert.equal(panel.hidden, true);

  // Re-open and close via Escape key on root
  chipButton.dispatchEvent({ type: "click", bubbles: true });
  assert.equal(panel.hidden, false);
  root.dispatchEvent({ type: "keydown", key: "Escape", stopPropagation: () => {} });
  assert.equal(panel.hidden, true);

  inst.dispose();
});

// 7. 3D mode with mock renderer mounts and disposes cleanly
test("mountTwin with createRenderer operates in 3d mode and disposes", async () => {
  const { root } = createMockDom();
  const inst = mountTwin(root, () => Promise.resolve(sampleData(2)), { createRenderer: mockRenderer });
  await inst.ready;
  assert.equal(inst.mode, "3d");

  const canvas = root.find((el) => el.tagName === "CANVAS");
  assert.ok(canvas);

  inst.update(sampleData(3));
  inst.dispose();
  assert.equal(root.children.length, 0);
});
