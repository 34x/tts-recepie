// ── library.test.mjs — unit tests for the text library store ──────────────
// Run: node --no-warnings library.test.mjs
import assert from 'node:assert/strict';
import { createLibrary, djb2, normalize, LIBRARY_CAP_UNITS } from './static/library.js';

let passed = 0;
let failed = 0;

function test(name, fn) {
    try {
        fn();
        passed++;
        console.log(`  ✓ ${name}`);
    } catch (e) {
        failed++;
        console.error(`  ✗ ${name}`);
        console.error(`    ${e.message}`);
    }
}

// In-memory backing store — proves the store contract is swappable and keeps
// tests independent of localStorage.
function makeFakeStore() {
    let data = null;
    return {
        read: () => data,
        write: (t) => { data = t; return true; },
        _dump: () => data,
    };
}

// Controllable clock for deterministic recency ordering.
function clock(start = 1000) {
    let t = start;
    return { now: () => t++, at: (v) => { t = v; } };
}

console.log('\nHashing & normalization:');

test('djb2 is stable and differs per content', () => {
    assert.equal(djb2('hello'), djb2('hello'));
    assert.notEqual(djb2('hello'), djb2('world'));
    assert.equal(typeof djb2(''), 'string');
});

test('normalize trims and collapses whitespace', () => {
    assert.equal(normalize('  a\n\n  b  '), 'a b');
});

console.log('\nSave & dedupe:');

test('save adds an entry', () => {
    const lib = createLibrary(makeFakeStore(), { now: clock().now });
    lib.save('hello world');
    const list = lib.list();
    assert.equal(list.length, 1);
    assert.equal(list[0].text, 'hello world');
    assert.equal(list[0].chars, 11);
});

test('save dedupes by normalized content', () => {
    const lib = createLibrary(makeFakeStore(), { now: clock().now });
    lib.save('hello world');
    lib.save('hello world');            // exact duplicate
    lib.save('  hello   world  ');      // whitespace-only difference
    assert.equal(lib.list().length, 1, 'should not duplicate');
});

test('re-saving refreshes recency, does not duplicate', () => {
    const c = clock();
    const lib = createLibrary(makeFakeStore(), { now: c.now });
    lib.save('first');
    lib.save('second');
    assert.equal(lib.list()[0].text, 'second');
    lib.save('first');                  // re-save older text
    const list = lib.list();
    assert.equal(list.length, 2, 'no duplicate');
    assert.equal(list[0].text, 'first', 'refreshed to top');
});

test('save ignores empty/whitespace-only text', () => {
    const lib = createLibrary(makeFakeStore(), { now: clock().now });
    lib.save('   ');
    lib.save('');
    assert.equal(lib.list().length, 0);
});

test('differing case is treated as distinct', () => {
    const lib = createLibrary(makeFakeStore(), { now: clock().now });
    lib.save('Hello');
    lib.save('hello');
    assert.equal(lib.list().length, 2);
});

console.log('\nOrdering & removal:');

test('list is most-recent-first', () => {
    const c = clock();
    const lib = createLibrary(makeFakeStore(), { now: c.now });
    lib.save('a');
    lib.save('b');
    lib.save('c');
    assert.deepEqual(lib.list().map(e => e.text), ['c', 'b', 'a']);
});

test('remove deletes by id', () => {
    const lib = createLibrary(makeFakeStore(), { now: clock().now });
    lib.save('a');
    lib.save('b');
    const id = lib.list().find(e => e.text === 'a').id;
    lib.remove(id);
    assert.deepEqual(lib.list().map(e => e.text), ['b']);
});

test('clear drops everything', () => {
    const lib = createLibrary(makeFakeStore(), { now: clock().now });
    lib.save('a');
    lib.clear();
    assert.equal(lib.list().length, 0);
});

console.log('\nPersistence & cap:');

test('entries survive a reload through the same store', () => {
    const store = makeFakeStore();
    createLibrary(store, { now: clock().now }).save('persisted');
    const reopened = createLibrary(store, { now: clock().now });
    assert.equal(reopened.list()[0].text, 'persisted');
});

test('evicts oldest entries once the cap is exceeded', () => {
    const c = clock();
    const store = makeFakeStore();
    const lib = createLibrary(store, { now: c.now, cap: 24 });
    lib.save('a'.repeat(10)); // 10 units
    lib.save('b'.repeat(10)); // 20 total
    lib.save('c'.repeat(10)); // 30 → over cap, oldest ('a') evicted
    const texts = lib.list().map(e => e.text[0]);
    assert.deepEqual(texts, ['c', 'b']);
});

test('newest entry is kept even when it alone exceeds the cap', () => {
    const store = makeFakeStore();
    const lib = createLibrary(store, { now: clock().now, cap: 5 });
    lib.save('x'.repeat(100));
    assert.equal(lib.list().length, 1, 'save must never be silently lost');
});

test('default cap is a positive named constant', () => {
    assert.ok(LIBRARY_CAP_UNITS > 0);
});

console.log('\nLast-played chunk:');

test('save initializes lastChunk to 0', () => {
    const lib = createLibrary(makeFakeStore(), { now: clock().now });
    lib.save('hello');
    assert.equal(lib.list()[0].lastChunk, 0);
});

test('setLastChunk records the index', () => {
    const lib = createLibrary(makeFakeStore(), { now: clock().now });
    lib.save('hello');
    lib.setLastChunk(lib.idFor('hello'), 7);
    assert.equal(lib.get(lib.idFor('hello')).lastChunk, 7);
});

test('re-saving preserves lastChunk', () => {
    const lib = createLibrary(makeFakeStore(), { now: clock().now });
    lib.save('hello');
    lib.setLastChunk(lib.idFor('hello'), 5);
    lib.save('hello'); // refresh — must not reset the remembered chunk
    assert.equal(lib.get(lib.idFor('hello')).lastChunk, 5);
});

test('setLastChunk does not reorder entries', () => {
    const c = clock();
    const lib = createLibrary(makeFakeStore(), { now: c.now });
    lib.save('a');
    lib.save('b');
    lib.setLastChunk(lib.idFor('b'), 3);
    assert.deepEqual(lib.list().map(e => e.text), ['b', 'a']);
});

test('setLastChunk ignores unknown id and clamps negatives', () => {
    const lib = createLibrary(makeFakeStore(), { now: clock().now });
    lib.save('hello');
    lib.setLastChunk('nope', 4); // unknown — no-op, no throw
    assert.equal(lib.list().length, 1);
    lib.setLastChunk(lib.idFor('hello'), -3);
    assert.equal(lib.get(lib.idFor('hello')).lastChunk, 0);
});

test('lastChunk survives a reload through the same store', () => {
    const store = makeFakeStore();
    const lib = createLibrary(store, { now: clock().now });
    lib.save('persisted');
    lib.setLastChunk(lib.idFor('persisted'), 9);
    const reopened = createLibrary(store, { now: clock().now });
    assert.equal(reopened.get(reopened.idFor('persisted')).lastChunk, 9);
});

console.log('\nFailure handling:');

test('write failure throws rather than corrupting state', () => {
    const store = { read: () => null, write: () => false };
    const lib = createLibrary(store, { now: clock().now });
    assert.throws(() => lib.save('boom'), /storage write failed/);
});

test('corrupt stored JSON degrades to empty list', () => {
    const store = { read: () => '{not json', write: () => true };
    const lib = createLibrary(store, { now: clock().now });
    assert.deepEqual(lib.list(), []);
});

test('unavailable store (null read) yields empty list', () => {
    const store = { read: () => null, write: () => true };
    const lib = createLibrary(store, { now: clock().now });
    assert.deepEqual(lib.list(), []);
});

console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed\n`);
process.exit(failed > 0 ? 1 : 0);
