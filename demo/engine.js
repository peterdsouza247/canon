/* Canon demo engine.
 *
 * A faithful JavaScript port of the ideas in src/canon: lazy fact references
 * that record what they are asked for, one walk that both evaluates and plans,
 * declared rule dependencies sorted into strata, and a full trace.
 *
 * Everything on the demo page is computed here, in the browser, from these
 * rules and these facts. Nothing is a stored screenshot and no numbers are
 * hard coded.
 *
 * One difference from the Python engine, stated plainly: Python parses rule
 * expressions from source text, so boolean composition can be written with
 * ordinary "and" and "or". JavaScript cannot intercept those operators, so this
 * port asks rule authors to write $.all(...) and $.any(...) with thunks. The
 * semantics are identical, including short circuiting at run time and full
 * branch coverage during planning.
 */

'use strict';

/* ------------------------------------------------------------------ */
/* SHA-256, so hashes, Merkle roots and the deploy chain are real      */
/* ------------------------------------------------------------------ */

const sha256 = (function () {
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
  ]);
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));

  return function sha256(message) {
    const bytes = new TextEncoder().encode(message);
    const length = bytes.length;
    const blocks = Math.ceil((length + 9) / 64);
    const buffer = new Uint8Array(blocks * 64);
    buffer.set(bytes);
    buffer[length] = 0x80;
    const view = new DataView(buffer.buffer);
    const bitLength = length * 8;
    view.setUint32(blocks * 64 - 8, Math.floor(bitLength / 4294967296));
    view.setUint32(blocks * 64 - 4, bitLength >>> 0);

    const H = new Uint32Array([
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
    ]);
    const w = new Uint32Array(64);

    for (let block = 0; block < blocks; block += 1) {
      for (let i = 0; i < 16; i += 1) w[i] = view.getUint32(block * 64 + i * 4);
      for (let i = 16; i < 64; i += 1) {
        const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
        const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }
      let a = H[0], b = H[1], c = H[2], d = H[3];
      let e = H[4], f = H[5], g = H[6], h = H[7];
      for (let i = 0; i < 64; i += 1) {
        const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        const ch = (e & f) ^ (~e & g);
        const t1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
        const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        const maj = (a & b) ^ (a & c) ^ (b & c);
        const t2 = (S0 + maj) >>> 0;
        h = g; g = f; f = e; e = (d + t1) >>> 0;
        d = c; c = b; b = a; a = (t1 + t2) >>> 0;
      }
      H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0;
      H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
      H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0;
      H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
    }
    return Array.from(H).map((x) => x.toString(16).padStart(8, '0')).join('');
  };
}());

function canonical(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  const keys = Object.keys(value).sort();
  return '{' + keys.map((k) => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
}

const contentHash = (value) => sha256(canonical(value));

/* ------------------------------------------------------------------ */
/* Lazy fact references                                                */
/* ------------------------------------------------------------------ */

const IS_REF = Symbol('canon.ref');
const REF_PATH = Symbol('canon.path');
const REF_CTX = Symbol('canon.ctx');
const REF_VALUE = Symbol('canon.value');
const STATIC = Symbol('canon.static');

function makeRef(path, ctx, hasValue, value) {
  return new Proxy({}, {
    get(target, prop) {
      if (prop === IS_REF) return true;
      if (prop === REF_PATH) return path;
      if (prop === REF_CTX) return ctx;
      if (prop === REF_VALUE) return hasValue ? value : undefined;
      if (prop === STATIC) return hasValue;
      if (prop === Symbol.toPrimitive) {
        return () => (hasValue ? ctx.note(path, value) : ctx.read(path));
      }
      if (prop === 'valueOf' || prop === 'toString') {
        return () => (hasValue ? ctx.note(path, value) : ctx.read(path));
      }
      if (typeof prop === 'symbol') return undefined;
      const childPath = path ? path + '.' + prop : String(prop);
      if (hasValue) {
        const child = (value === null || value === undefined || value === ctx.unknown)
          ? ctx.unknown
          : value[prop];
        return makeRef(childPath, ctx, true, child);
      }
      return makeRef(childPath, ctx, false, undefined);
    }
  });
}

const isRef = (x) => x !== null && typeof x === 'object' && x[IS_REF] === true;

/* ------------------------------------------------------------------ */
/* Resolution context: one object, two modes                           */
/* ------------------------------------------------------------------ */

class Context {
  constructor(options) {
    this.static = !!options.static;
    this.facts = options.facts || {};
    this.derived = options.derived || {};
    this.reads = new Map();
    this.scope = null;
    this.unknown = options.static ? Symbol('unknown') : undefined;
    this.fetchedRoots = new Set();
    this.fetches = 0;
    this.sourceCalls = options.sourceCalls || null;
  }

  record(path, kind, value) {
    const existing = this.reads.get(path);
    if (!existing) this.reads.set(path, { path, kind, value });
    else if (existing.kind === 'scalar' && kind === 'collection') existing.kind = 'collection';
    // The per rule scope is what the trace shows. It is separate from the
    // transaction wide map so that two rules reading the same field each
    // record it, while the underlying fetch still happens once.
    if (this.scope && !this.scope.has(path)) this.scope.set(path, { path, kind, value });
  }

  beginScope() {
    this.scope = new Map();
    return this.scope;
  }

  endScope() {
    const scope = this.scope || new Map();
    this.scope = null;
    return scope;
  }

  root(name) {
    if (name === 'derived') return this.derived;
    if (!this.fetchedRoots.has(name)) {
      this.fetchedRoots.add(name);
      this.fetches += 1;
      if (this.sourceCalls) this.sourceCalls.push(name);
    }
    return this.facts[name];
  }

  walk(path) {
    const parts = path.split('.');
    let cursor = this.root(parts[0]);
    for (let i = 1; i < parts.length; i += 1) {
      if (cursor === null || cursor === undefined) return undefined;
      cursor = cursor[parts[i]];
    }
    return cursor;
  }

  read(path) {
    if (this.static) {
      this.record(path, 'scalar', null);
      return NaN;
    }
    const value = this.walk(path);
    this.record(path, 'scalar', value);
    return value;
  }

  note(path, value) {
    if (this.static) {
      this.record(path, 'scalar', null);
      return NaN;
    }
    this.record(path, 'scalar', value);
    return value;
  }

  collection(refOrArray) {
    if (!isRef(refOrArray)) return Array.isArray(refOrArray) ? refOrArray : [];
    const path = refOrArray[REF_PATH];
    const base = path + '[*]';
    if (this.static) {
      this.record(path, 'collection', null);
      return [makeRef(base, this, true, this.unknown)];
    }
    const raw = refOrArray[STATIC] ? refOrArray[REF_VALUE] : this.walk(path);
    this.record(path, 'collection', Array.isArray(raw) ? raw.length : 0);
    if (!Array.isArray(raw)) return [];
    return raw.map((item) => makeRef(base, this, true, item));
  }

  readPaths() {
    return Array.from(this.reads.keys()).sort();
  }
}

/* ------------------------------------------------------------------ */
/* The helper surface rules are written against                        */
/* ------------------------------------------------------------------ */

function helpers(ctx) {
  const v = (x) => {
    if (!isRef(x)) return x;
    const path = x[REF_PATH];
    return x[STATIC] ? ctx.note(path, x[REF_VALUE]) : ctx.read(path);
  };
  const num = (x) => {
    const raw = v(x);
    return typeof raw === 'number' ? raw : Number(raw);
  };
  const hoursBetween = (a, b) => {
    const start = Date.parse(v(a));
    const end = Date.parse(v(b));
    if (Number.isNaN(start) || Number.isNaN(end)) return NaN;
    return (end - start) / 3600000;
  };

  return {
    ctx,
    static: ctx.static,
    v,
    num,
    hours: hoursBetween,

    // comparison, explicit so that planning and evaluation take the same walk
    gt: (a, b) => (ctx.static ? (v(a), v(b), false) : num(a) > num(b)),
    gte: (a, b) => (ctx.static ? (v(a), v(b), false) : num(a) >= num(b)),
    lt: (a, b) => (ctx.static ? (v(a), v(b), false) : num(a) < num(b)),
    lte: (a, b) => (ctx.static ? (v(a), v(b), false) : num(a) <= num(b)),
    eq: (a, b) => (ctx.static ? (v(a), v(b), false) : v(a) === v(b)),
    ne: (a, b) => (ctx.static ? (v(a), v(b), false) : v(a) !== v(b)),
    isTrue: (a) => (ctx.static ? (v(a), false) : v(a) === true),
    isFalse: (a) => (ctx.static ? (v(a), false) : v(a) === false),
    oneOf: (a, list) => (ctx.static ? (v(a), false) : list.indexOf(v(a)) !== -1),
    has: (list, item) => {
      const raw = v(list);
      if (ctx.static) { v(item); return false; }
      return Array.isArray(raw) && raw.indexOf(v(item)) !== -1;
    },
    lacks: (list, item) => {
      const raw = v(list);
      if (ctx.static) { v(item); return false; }
      return !Array.isArray(raw) || raw.indexOf(v(item)) === -1;
    },

    // boolean composition over thunks: every branch is visited when planning,
    // and short circuits when evaluating
    all: (...thunks) => {
      if (ctx.static) { thunks.forEach((t) => t()); return false; }
      for (const thunk of thunks) if (!thunk()) return false;
      return true;
    },
    any: (...thunks) => {
      if (ctx.static) { thunks.forEach((t) => t()); return false; }
      for (const thunk of thunks) if (thunk()) return true;
      return false;
    },
    not: (x) => (ctx.static ? false : !x),

    // collections: the vertical slice
    count: (collection, predicate) => {
      const items = ctx.collection(collection);
      let total = 0;
      for (const item of items) {
        const keep = predicate ? predicate(item) : true;
        if (!ctx.static && keep) total += 1;
      }
      return ctx.static ? NaN : total;
    },
    some: (collection, predicate) => {
      const items = ctx.collection(collection);
      let found = false;
      for (const item of items) {
        const keep = predicate(item);
        if (!ctx.static && keep) found = true;
      }
      return ctx.static ? false : found;
    },
    sumOf: (collection, selector, predicate) => {
      const items = ctx.collection(collection);
      let total = 0;
      for (const item of items) {
        const keep = predicate ? predicate(item) : true;
        const picked = selector(item);
        if (ctx.static) { v(picked); continue; }
        if (keep) total += Number(v(picked)) || 0;
      }
      return ctx.static ? NaN : total;
    },

    min: (...values) => (ctx.static ? (values.forEach(v), NaN) : Math.min(...values.map(num))),
    max: (...values) => (ctx.static ? (values.forEach(v), NaN) : Math.max(...values.map(num))),
    round: (x, places) => {
      const raw = num(x);
      if (ctx.static || Number.isNaN(raw)) return NaN;
      const factor = Math.pow(10, places || 0);
      return Math.round(raw * factor) / factor;
    }
  };
}

/* ------------------------------------------------------------------ */
/* Projection                                                          */
/* ------------------------------------------------------------------ */

function projectionFromPaths(paths) {
  const roots = {};
  paths.forEach((path) => {
    const segments = path.split('.').map((raw) => {
      let name = raw;
      let collection = false;
      while (name.endsWith('[*]')) { collection = true; name = name.slice(0, -3); }
      return { name, collection };
    });
    let level = roots;
    let node = null;
    segments.forEach((segment) => {
      if (!level[segment.name]) level[segment.name] = { collection: false, children: {} };
      node = level[segment.name];
      if (segment.collection) node.collection = true;
      level = node.children;
    });
  });
  return roots;
}

function leafCount(tree) {
  let total = 0;
  Object.keys(tree).forEach((key) => {
    const node = tree[key];
    const children = Object.keys(node.children);
    total += children.length === 0 ? 1 : leafCount(node.children);
  });
  return total;
}

function selectByProjection(tree, document) {
  const out = {};
  Object.keys(tree).forEach((key) => {
    if (document === null || document === undefined) return;
    const node = tree[key];
    const raw = document[key];
    if (raw === undefined || raw === null) return;
    out[key] = selectNode(node, raw);
  });
  return out;
}

function selectNode(node, value) {
  const children = Object.keys(node.children);
  if (node.collection && Array.isArray(value)) {
    return value.map((item) => (children.length ? selectByProjection(node.children, item) : item));
  }
  if (!children.length) return value;
  return selectByProjection(node.children, value);
}

/* ------------------------------------------------------------------ */
/* Rule identity                                                       */
/* ------------------------------------------------------------------ */

function ruleCanonical(rule) {
  const detail = {};
  if (rule.emit && rule.emit.detail) {
    Object.keys(rule.emit.detail).sort().forEach((key) => {
      detail[key] = rule.emit.detail[key].src;
    });
  }
  const sets = {};
  Object.keys(rule.sets || {}).sort().forEach((key) => {
    sets[key] = rule.sets[key].src;
  });
  return {
    id: rule.id,
    version: rule.version,
    when: rule.whenSrc || null,
    emit: rule.emit ? {
      code: rule.emit.code,
      severity: rule.emit.severity,
      message: rule.emit.message,
      detail: detail
    } : null,
    sets: sets,
    reads: (rule.reads || []).slice().sort(),
    clients: (rule.clients || ['*']).slice().sort(),
    effective_from: rule.effectiveFrom || null,
    effective_to: rule.effectiveTo || null,
    priority: rule.priority
  };
}

function hashRule(rule) {
  return contentHash(ruleCanonical(rule));
}

/* ------------------------------------------------------------------ */
/* Ruleset: static analysis, strata, projections                       */
/* ------------------------------------------------------------------ */

class RuleSet {
  constructor(id, version, rules, derivedPolicy) {
    this.id = id;
    this.version = version;
    this.rules = rules;
    this.derivedPolicy = derivedPolicy || {};
    this.byId = {};
    rules.forEach((rule) => {
      rule.hash = hashRule(rule);
      this.byId[rule.id] = rule;
    });
    this.analyse();
  }

  analyse() {
    this.rulePaths = {};
    this.ruleDerived = {};
    this.producers = {};

    this.rules.forEach((rule) => {
      const ctx = new Context({ static: true });
      const $ = helpers(ctx);
      const f = makeRef('', ctx, false, undefined);
      try {
        if (rule.when) rule.when(f, $);
        if (rule.emit && rule.emit.detail) {
          Object.keys(rule.emit.detail).forEach((key) => rule.emit.detail[key].fn(f, $));
        }
        Object.keys(rule.sets || {}).forEach((key) => rule.sets[key].fn(f, $));
      } catch (error) {
        console.error('static analysis failed for ' + rule.id, error);
      }
      const all = ctx.readPaths();
      this.rulePaths[rule.id] = all.filter((p) => !p.startsWith('derived.'));
      this.ruleDerived[rule.id] = all.filter((p) => p.startsWith('derived.'));
      Object.keys(rule.sets || {}).forEach((name) => {
        const key = 'derived.' + name;
        if (!this.producers[key]) this.producers[key] = [];
        this.producers[key].push(rule.id);
      });
    });

    this.allPaths = Array.from(new Set(
      this.rules.reduce((acc, rule) => acc.concat(this.rulePaths[rule.id]), [])
    )).sort();
    this.projection = projectionFromPaths(this.allPaths);
    this.strata = this.buildStrata();
  }

  buildStrata() {
    const dependsOn = {};
    this.rules.forEach((rule) => {
      dependsOn[rule.id] = new Set();
      this.ruleDerived[rule.id].forEach((name) => {
        (this.producers[name] || []).forEach((producer) => {
          if (producer !== rule.id) dependsOn[rule.id].add(producer);
        });
      });
    });

    const resolved = new Set();
    const remaining = new Set(this.rules.map((r) => r.id));
    const strata = [];
    while (remaining.size) {
      const ready = Array.from(remaining).filter((id) =>
        Array.from(dependsOn[id]).every((dep) => resolved.has(dep)));
      if (!ready.length) throw new Error('dependency cycle between rules: ' + Array.from(remaining));
      ready.sort((a, b) => (this.byId[a].priority - this.byId[b].priority) || a.localeCompare(b));
      strata.push(ready.map((id) => this.byId[id]));
      ready.forEach((id) => { resolved.add(id); remaining.delete(id); });
    }
    return strata;
  }

  applies(rule, client, asOf) {
    const clients = rule.clients || ['*'];
    if (client && clients.indexOf('*') === -1 && clients.indexOf(client) === -1) {
      return { ok: false, reason: 'not enabled for client ' + client };
    }
    if (asOf && rule.effectiveFrom && asOf < rule.effectiveFrom) {
      return { ok: false, reason: 'not effective until ' + rule.effectiveFrom };
    }
    if (asOf && rule.effectiveTo && asOf > rule.effectiveTo) {
      return { ok: false, reason: 'expired on ' + rule.effectiveTo };
    }
    return { ok: true, reason: 'applicable' };
  }

  applicable(client, asOf, enabled) {
    return this.rules.filter((rule) => {
      if (enabled && enabled.indexOf(rule.id) === -1) return false;
      return this.applies(rule, client, asOf).ok;
    });
  }

  projectionFor(client, asOf, enabled) {
    const paths = new Set();
    this.applicable(client, asOf, enabled).forEach((rule) => {
      this.rulePaths[rule.id].forEach((path) => paths.add(path));
    });
    const sorted = Array.from(paths).sort();
    return { paths: sorted, tree: projectionFromPaths(sorted) };
  }

  merkleRoot(client, asOf) {
    const leaves = this.applicable(client, asOf, null)
      .map((rule) => sha256('leaf:' + rule.hash)).sort();
    if (!leaves.length) return sha256('canon:empty');
    let layer = leaves;
    while (layer.length > 1) {
      const next = [];
      for (let i = 0; i < layer.length; i += 2) {
        const left = layer[i];
        const right = i + 1 < layer.length ? layer[i + 1] : left;
        next.push(sha256('node:' + left + right));
      }
      layer = next;
    }
    return layer[0];
  }
}

/* ------------------------------------------------------------------ */
/* Evaluation                                                          */
/* ------------------------------------------------------------------ */

const SEVERITY_RANK = { info: 0, advisory: 1, soft: 2, hard: 3 };

function renderMessage(template, detail) {
  return String(template || '').replace(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g, (match, key) => {
    if (!(key in detail)) return match;
    const value = detail[key];
    return typeof value === 'number' ? String(Math.round(value * 100) / 100) : String(value);
  });
}

function combine(policy, values) {
  const usable = values.filter((x) => x !== null && x !== undefined && !Number.isNaN(x));
  if (!usable.length) return null;
  switch (policy) {
    case 'min': return Math.min(...usable);
    case 'max': return Math.max(...usable);
    case 'sum': return usable.reduce((a, b) => a + b, 0);
    case 'last': return usable[usable.length - 1];
    default: return usable[0];
  }
}

function evaluate(ruleset, facts, options) {
  const settings = options || {};
  const client = settings.client || null;
  const asOf = settings.asOf || null;
  const enabled = settings.enabled || null;
  const started = performance.now();

  const sourceCalls = [];
  const derived = {};
  const pending = {};
  const ctx = new Context({ static: false, facts, derived, sourceCalls });
  const $shared = helpers(ctx);

  const projection = ruleset.projectionFor(client, asOf, enabled);
  const traces = [];
  const findings = [];

  ruleset.strata.forEach((stratum, stratumIndex) => {
    let wrote = false;
    stratum.forEach((rule) => {
      const trace = {
        ruleId: rule.id,
        version: rule.version,
        hash: rule.hash,
        title: rule.title,
        stratum: stratumIndex,
        considered: true,
        skipReason: null,
        guardSource: rule.whenSrc || null,
        guardResult: null,
        fired: false,
        reads: [],
        emitted: null,
        sets: {},
        micros: 0
      };

      if (enabled && enabled.indexOf(rule.id) === -1) {
        trace.considered = false;
        trace.skipReason = 'switched off in this demo';
        traces.push(trace);
        return;
      }
      const applicability = ruleset.applies(rule, client, asOf);
      if (!applicability.ok) {
        trace.considered = false;
        trace.skipReason = applicability.reason;
        traces.push(trace);
        return;
      }

      const scope = ctx.beginScope();
      const ruleStart = performance.now();
      const f = makeRef('', ctx, false, undefined);
      try {
        let guard = true;
        if (rule.when) {
          guard = rule.when(f, $shared);
          trace.guardResult = guard;
        }
        if (guard) {
          trace.fired = true;
          if (rule.emit) {
            const detail = {};
            Object.keys(rule.emit.detail || {}).forEach((key) => {
              detail[key] = rule.emit.detail[key].fn(f, $shared);
            });
            const finding = {
              code: rule.emit.code,
              severity: rule.emit.severity,
              message: renderMessage(rule.emit.message, detail),
              ruleId: rule.id,
              ruleVersion: rule.version,
              detail: detail
            };
            findings.push(finding);
            trace.emitted = finding;
          }
          Object.keys(rule.sets || {}).forEach((name) => {
            const value = rule.sets[name].fn(f, $shared);
            if (!pending[name]) pending[name] = [];
            pending[name].push(value);
            trace.sets[name] = value;
            wrote = true;
          });
        }
      } catch (error) {
        trace.error = String(error);
      }
      trace.micros = (performance.now() - ruleStart) * 1000;
      ctx.endScope();
      scope.forEach((read) => {
        trace.reads.push({ path: read.path, value: read.value, kind: read.kind });
      });
      trace.reads.sort((a, b) => a.path.localeCompare(b.path));
      traces.push(trace);
    });

    if (wrote) {
      Object.keys(pending).forEach((name) => {
        derived[name] = combine(ruleset.derivedPolicy[name] || 'first', pending[name]);
      });
    }
  });

  findings.sort((a, b) => (SEVERITY_RANK[b.severity] - SEVERITY_RANK[a.severity])
    || (ruleset.byId[a.ruleId].priority - ruleset.byId[b.ruleId].priority));

  const readPaths = ctx.readPaths().filter((p) => !p.startsWith('derived.'));
  const inputs = {};
  ctx.reads.forEach((read, path) => {
    if (read.kind === 'scalar' && !path.startsWith('derived.')) inputs[path] = read.value;
  });

  return {
    ok: !findings.some((finding) => finding.severity === 'hard'),
    severity: findings.length
      ? findings.reduce((worst, f) => (SEVERITY_RANK[f.severity] > SEVERITY_RANK[worst] ? f.severity : worst), 'info')
      : 'info',
    findings,
    derived,
    traces,
    projection,
    stats: {
      plannedPaths: projection.paths.length,
      readPaths: readPaths.length,
      unread: projection.paths.filter((p) => !ctx.reads.has(p)),
      rootsPlanned: Object.keys(projection.tree).length,
      rootsFetched: ctx.fetchedRoots.size,
      fetches: ctx.fetches
    },
    inputs,
    inputDigest: contentHash(inputs),
    outputDigest: contentHash({
      findings: findings.map((x) => ({ code: x.code, severity: x.severity, detail: x.detail })),
      derived
    }),
    micros: (performance.now() - started) * 1000
  };
}

function explain(decision, code) {
  const chain = [];
  const emitting = decision.traces.find((t) => t.emitted && t.emitted.code === code);
  if (!emitting) return chain;
  chain.push(emitting);
  const seen = new Set([emitting.ruleId]);
  const walk = (trace) => {
    const wanted = new Set(trace.reads
      .filter((r) => r.path.startsWith('derived.'))
      .map((r) => r.path.split('.')[1]));
    decision.traces.forEach((other) => {
      if (seen.has(other.ruleId)) return;
      const writes = Object.keys(other.sets);
      if (writes.some((name) => wanted.has(name))) {
        seen.add(other.ruleId);
        chain.push(other);
        walk(other);
      }
    });
  };
  walk(emitting);
  return chain;
}

/* ------------------------------------------------------------------ */
/* Deterministic pseudo random, for the shadow section                 */
/* ------------------------------------------------------------------ */

function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

window.CanonEngine = {
  sha256,
  canonical,
  contentHash,
  RuleSet,
  evaluate,
  explain,
  projectionFromPaths,
  selectByProjection,
  leafCount,
  mulberry32,
  hashRule
};
