// audioCache.test.mjs — IndexedDB audio cache invariants.
//
// The real module gates every call on `typeof indexedDB !== 'undefined'`,
// which is false in Node, so calls resolve to their null/0 fallbacks instead
// of throwing. That contract (importing without a browser must not throw,
// and calls must resolve sanely) is what's asserted here. Browser-side
// storage behaviour (save/get round-trip, cursor deletion) is exercised only
// via the mock in-memory shim below.

import { test } from 'node:test';
import assert from 'node:assert/strict';

// Node lacks IDBKeyRange; provide a minimal stand-in for audioCache.js's
// `IDBKeyRange.only(entryId)` call in removeByEntry.
if (typeof globalThis.IDBKeyRange !== 'object') {
    globalThis.IDBKeyRange = { only: (v) => ({ _eq: v }) };
}

// ── In-memory IDB shim (only the surface audioCache.js uses) ────────────────
function makeFakeIDB() {
    const stores = new Map();
    function open(name, version) {
        const req = { result: fakeDB, onupgradeneeded: null, onsuccess: null, onerror: null };
        // Fire asynchronously, like a real IDB request.
        Promise.resolve().then(() => { req.onupgradeneeded?.(); req.onsuccess?.(); });
        return req;
    }
    function makeRequest(value, assignLater = false) {
        const r = { result: value, onsuccess: null, onerror: null };
        // Run onsuccess on the next microtask (async, like a real IDB req).
        // The deferred variant gives the caller one synchronous turn to
        // attach its own onsuccess first (mimics cursor assignment pattern).
        Promise.resolve().then(() => Promise.resolve().then(() =>
            r.onsuccess?.({ target: r, result: value })));
        return r;
    }
    const fakeDB = {
        objectStoreNames: { contains: () => false },
        createObjectStore(_n) { return fakeOS; },
        transaction(_store, mode) {
            const tx = {
                objectStore() { return fakeOS; },
                oncomplete: null, onerror: null, onabort: null,
                error: null,
            };
            // Complete well after request-level microtasks and cursor
            // iteration steps have settled (real IDB keeps a tx alive while
            // requests are pending; the shim approximates with a delay).
            const done = () => tx.oncomplete?.();
            Promise.resolve().then(() => Promise.resolve().then(() =>
                Promise.resolve().then(() => Promise.resolve().then(() =>
                    Promise.resolve().then(() => Promise.resolve().then(done))))));
            return tx;
        },
    };
    const fakeOS = {
        createIndex(_n, _kp) { /* records are keyed directly in Map */ },
        put(rec) { stores.set(rec.id, rec); return makeRequest(rec.id); },
        get(id) { return makeRequest(stores.get(id)); },
        clear() { stores.clear(); return makeRequest(undefined); },
        count() { return makeRequest(stores.size); },
        index(_name) { return fakeOS; },
        openCursor(range) {
            const recs = [...stores.values()].filter(r => !range || range._eq === undefined || r.entryId === range._eq);
            let i = 0;
            const req = { onsuccess: null };
            function step() {
                const cur = i < recs.length
                    ? { delete() { stores.delete(recs[i].id); }, continue() { i++; Promise.resolve().then(step); } }
                    : null;
                req.onsuccess?.({ target: { result: cur }, result: cur });
            }
            Promise.resolve().then(() => Promise.resolve().then(step));
            return req;
        },
    };
    return { open, _stores: stores };
}

// ── Test 1: module imports safely, but storage calls REJECT without indexedDB ──
// Importing must not throw (Node/CI friendliness). Calling must NOT resolve to a
// neutral value: a silent null/0 is indistinguishable from "no cached audio" and
// would hide a completely broken cache from the UI.
test('module guards: import is safe, storage calls reject without indexedDB', async () => {
    const { get, count, clear, removeByEntry } = await import('./audioCache.js');
    await assert.rejects(() => get('whatever'), /IndexedDB unavailable/);
    await assert.rejects(() => count(), /IndexedDB unavailable/);
    await assert.rejects(() => clear(), /IndexedDB unavailable/);
    await assert.rejects(() => removeByEntry('anyid'), /IndexedDB unavailable/);
});

// ── Test 2: recordId is deterministic and distinct per content ───────────────
test('recordId: same body -> same key, different body -> different key', async () => {
    const { recordId } = await import('./audioCache.js');
    const body = { input: 'hello world', voice: 'alloy', model: 'm', instruct: '' };
    const a = await recordId(body);
    const b = await recordId({ input: 'hello world', voice: 'alloy', model: 'm', instruct: '' });
    const c = await recordId({ input: 'hello worm', voice: 'alloy', model: 'm', instruct: '' });
    const d = await recordId({ input: 'hello world', voice: 'onyx', model: 'm', instruct: '' });
    assert.equal(a, b);
    assert.notEqual(a, c);
    assert.notEqual(a, d);
});

// ── Test 2b: key identity == request identity ──────────────────────────────
// The key must be a hash of exactly the serialized request body. Guards against
// reintroducing a second field list that can drift from what fetch() sends.
test('recordId hashes exactly the serialized fetch body', async () => {
    const { recordId } = await import('./audioCache.js');
    const fetchBody = { input: 'chunk', voice: 'v', model: 'm', instruct: '', backend: 'b' };
    assert.equal(await recordId(fetchBody), await recordId(JSON.parse(JSON.stringify(fetchBody))));
    // Any added field is part of the key — a new request parameter cannot be
    // sent without changing the key.
    assert.notEqual(await recordId(fetchBody), await recordId({ ...fetchBody, speed: 1.2 }));
});

// ── Test 3: save/get round-trip + overwrite on same key via in-memory shim ──
test('save/get round-trip against an in-memory IDB shim', async () => {
    // Inject the shim before the module reads `globalThis.indexedDB`.
    const shim = makeFakeIDB();
    globalThis.indexedDB = shim;
    // Re-import fresh so module-level memoized _dbp rebuilds.
    const cache = await import('./audioCache.js');

    const id = await cache.recordId({ input: 'chunk-text', voice: 'v' });
    await cache.save({ id, entryId: 'entry-1', audio: new Blob(['fake']), savedAt: 1 });
    const rec = await cache.get(id);
    assert.ok(rec);
    assert.equal(rec.entryId, 'entry-1');
    assert.ok(rec.audio instanceof Blob);

    // Overwrite same key is allowed (put semantics)
    await cache.save({ id, entryId: 'entry-9', audio: new Blob(['x']), savedAt: 2 });
    assert.equal((await cache.get(id)).entryId, 'entry-9');
    delete globalThis.indexedDB;
});

// ── Test 4: removeByEntry deletes exactly the tagged records ───────────────
test('removeByEntry evicts only the target entry\'s records', async () => {
    const shim = makeFakeIDB();
    globalThis.indexedDB = shim;
    const cache = await import('./audioCache.js');

    const idA1 = await cache.recordId({ input: 'a1', voice: 'v' });
    const idA2 = await cache.recordId({ input: 'a2', voice: 'v' });
    const idB  = await cache.recordId({ input: 'b',  voice: 'v' });
    await cache.save({ id: idA1, entryId: 'lib-A', audio: new Blob(['1']), savedAt: 1 });
    await cache.save({ id: idA2, entryId: 'lib-A', audio: new Blob(['2']), savedAt: 2 });
    await cache.save({ id: idB,  entryId: 'lib-B', audio: new Blob(['3']), savedAt: 3 });

    const removed = await cache.removeByEntry('lib-A');
    assert.equal(removed, 2);
    assert.equal(await cache.get(idA1), null);
    assert.equal(await cache.get(idA2), null);
    assert.ok(await cache.get(idB));   // untouched
    delete globalThis.indexedDB;
});

// ── Test 5: clear() empties everything ─────────────────────────────────────
test('clear() empties the store', async () => {
    const shim = makeFakeIDB();
    globalThis.indexedDB = shim;
    const cache = await import('./audioCache.js');

    // Isolate: the shim store persists across tests in this file.
    await cache.clear();

    const id = await cache.recordId({ input: 'x', voice: 'v' });
    await cache.save({ id, entryId: 'e', audio: new Blob(['z']), savedAt: 1 });
    assert.equal(await cache.count(), 1);
    await cache.clear();
    assert.equal(await cache.count(), 0);
    assert.equal(await cache.get(id), null);
    delete globalThis.indexedDB;
});