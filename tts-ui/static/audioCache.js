// ── audioCache.js — persistent per-chunk audio cache (IndexedDB) ────────────
// Stores finished TTS chunk blobs so playback replays from disk instead of
// re-fetching. One record per successful chunk request; key is a hash of the
// request parameters so cache validity == request equality (same input/params
// always map to the same key; a change in any field simply produces a miss).
// Each record also carries `entryId` (the library entry's djb2 id, passed in
// from chunkedPlayback.js) so deleting a library entry can evict all its
// chunks in one index query.
//
// Non-browser safe for *import*: the module loads in a test/Node environment
// without throwing, because every entry point is guarded on `indexedDB`.
// Calls made where IndexedDB is absent REJECT rather than resolving to a
// neutral value — a silent null/0 would be indistinguishable from "no cached
// audio" and would hide a broken cache from the UI. Callers may catch and
// degrade to their non-cache path, but they must report the degradation.

const DB_NAME = 'tts-ui-audio';
const DB_VERSION = 1;
const STORE = 'tts-audio';
const ENTRY_INDEX = 'byEntryId';

let _dbp = null;   // memoized open-promise (single connection per page)

function hasIDB() {
    return typeof indexedDB !== 'undefined';
}

/**
 * Storage is unavailable. This is a hard failure, not "no cached audio":
 * silently resolving to null/0 would make every lookup look like a miss and
 * every delete look like success. Callers surface it; they may still degrade
 * to their non-cache path.
 */
function _unavailable() {
    return Promise.reject(new Error(
        'IndexedDB unavailable (private browsing, disabled site storage, or non-persistent context)'));
}

/** Open (and, on first call, create) the database. Cache a single promise. */
function _open() {
    if (!hasIDB()) return Promise.reject(new Error('audioCache: indexedDB unavailable'));
    if (_dbp) return _dbp;
    _dbp = new Promise((resolve, reject) => {
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = () => {
            const db = req.result;
            if (!db.objectStoreNames.contains(STORE)) {
                const os = db.createObjectStore(STORE, { keyPath: 'id' });
                os.createIndex(ENTRY_INDEX, 'entryId', { unique: false });
            }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
    // If open fails for any reason, clear the memo so a retry has a chance.
    _dbp.catch(() => { _dbp = null; });
    return _dbp;
}

function _tx(db, mode, fn) {
    return new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, mode);
        const os = tx.objectStore(STORE);
        // In real IDB, the transaction has no `result` — values live on the
        // individual requests. Capture from the request's onsuccess handler
        // before tx.oncomplete fires.
        let result;
        const req = fn(os);
        if (req && typeof req === 'object') {
            req.onsuccess = () => { result = req.result; };
        }
        tx.oncomplete = () => resolve(result);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error || new Error('audioCache: transaction aborted'));
    });
}

// ── Public API ──────────────────────────────────────────────────────────────

/**
 * FNV-1a over two independent 64-bit lanes (hex, 16 chars each).
 *
 * This is the ONLY key derivation. crypto.subtle is deliberately not used: it
 * exists only in secure contexts (https / localhost) and is undefined on plain
 * http:// origins, which is how this app is actually served (0.0.0.0, LAN/VPN
 * IPs). A context-dependent algorithm would produce different keys for the
 * same request on different origins — the same audio cached on a desktop would
 * be unreachable from a phone. Cache keys need determinism, not cryptographic
 * strength; a collision only misplays audio for the one who caused it.
 */
export function hashKey(data) {
    let a = 0x811c9dc5n, b = 0xdeadbeefn;
    // Iterate CODE POINTS, not UTF-16 units: `for...of` over a string yields
    // one entry per code point, so astral characters hash as one value instead
    // of two surrogate halves. Never index the string directly — data[i] is a
    // character, and BigInt(character) throws.
    for (const ch of data) {
        const c = BigInt(ch.codePointAt(0));
        a = BigInt.asUintN(64, (a ^ c) * 0x100000001b3n);
        b = BigInt.asUintN(64, (b ^ c) * 0x1ff1ab0e7c701n);
    }
    return 'fnv-' + a.toString(16).padStart(16, '0') +
                    b.toString(16).padStart(16, '0');
}

/**
 * Cache key for one /api/audio/speech request.
 *
 * `fetchBody` is THE request body object — the same object that is passed to
 * fetch(). The key is a hash of its serialization, so key identity is request
 * identity by construction: there is no second field list to keep in sync.
 * Callers must not build a key from anything other than the body they send.
 *
 * Synchronous and environment-independent by design: same body, same key, on
 * every device and every origin.
 */
export function recordId(fetchBody) {
    return hashKey(JSON.stringify(fetchBody));
}

/** Insert or overwrite a record. Resolves after commit. */
export function save(record) {
    return _open().then(db => _tx(db, 'readwrite', os => os.put(record)));
}

/** Fetch one record by its key, or null when genuinely absent. */
export function get(id) {
    if (!hasIDB()) return _unavailable();
    return _open().then(db => _tx(db, 'readonly', os => os.get(id)))
        .then(r => r ?? null);
}

/** Delete every record tagged with this library entry id. */
export function removeByEntry(entryId) {
    if (!hasIDB()) return _unavailable();
    return _open().then(db => new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, 'readwrite');
        const idx = tx.objectStore(STORE).index(ENTRY_INDEX);
        const dels = [];
        idx.openCursor(IDBKeyRange.only(entryId)).onsuccess = (e) => {
            const cur = e.target.result;
            if (cur) { dels.push(cur.delete()); cur.continue(); }
        };
        tx.oncomplete = () => resolve(dels.length);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error || new Error('audioCache: transaction aborted'));
    }));
}

/** Empty the whole audio cache. */
export function clear() {
    if (!hasIDB()) return _unavailable();
    return _open().then(db => _tx(db, 'readwrite', os => os.clear()));
}

/** Number of records currently stored (diagnostic). */
export function count() {
    if (!hasIDB()) return _unavailable();
    return _open().then(db => _tx(db, 'readonly', os => os.count()));
}
