// ── theme.test.mjs — pure theme logic ───────────────────────────────────────
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalize, resolve, next, metaColor } from './theme.js';

test('normalize coerces unknown values to auto', () => {
    assert.equal(normalize('light'), 'light');
    assert.equal(normalize('dark'), 'dark');
    assert.equal(normalize('auto'), 'auto');
    assert.equal(normalize('bogus'), 'auto');
    assert.equal(normalize(null), 'auto');
    assert.equal(normalize(undefined), 'auto');
});

test('resolve: explicit preference wins over OS', () => {
    assert.equal(resolve('light', false), 'light');
    assert.equal(resolve('light', true), 'light');
    assert.equal(resolve('dark', true), 'dark');
    assert.equal(resolve('dark', false), 'dark');
});

test('resolve: auto follows the OS preference', () => {
    assert.equal(resolve('auto', true), 'light');
    assert.equal(resolve('auto', false), 'dark');
    assert.equal(resolve('bogus', true), 'light'); // normalized first
});

test('next cycles auto → light → dark → auto', () => {
    assert.equal(next('auto'), 'light');
    assert.equal(next('light'), 'dark');
    assert.equal(next('dark'), 'auto');
    assert.equal(next('bogus'), 'light'); // normalized to auto first
});

test('metaColor maps resolved theme to chrome color', () => {
    assert.equal(metaColor('light'), '#ffffff');
    assert.equal(metaColor('dark'), '#0d1117');
});
