// Tests of who can see a listing (platform-web/src/60_helpers.js, design.md section 5a): the access
// labels, the "Visible to" groups, My experiments' sections and the rules of set_access.
//   node --test 'platform-web/test/*.test.mjs'
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(HERE, '..', 'src', '60_helpers.js'), 'utf8');
const H = new Function('window', 'ehFormat', source + '\nreturn { accessOf, accessCanManage, accessGroup, accessBadge, accessSummary, normalizeAccess, librarySections, marketplaceListings, VISIBLE_TO, ACCESS_CHOICES };')({}, () => '');

const listing = (uid, exp, access, extra = {}) => ({ uid, exp, key: uid + '~' + exp, title: exp, publisher: { uid, name: uid === 'pub' ? 'Sharif' : 'Other' }, ...(access ? { access } : {}), ...extra });

test('a listing without access is public; people are kept only for Specific people', () => {
  assert.deepEqual(H.accessOf(listing('pub', 'a')), { visibility: 'public', people: [] });
  assert.deepEqual(H.accessOf(listing('pub', 'a', { visibility: 'odd' })), { visibility: 'public', people: [] });
  assert.deepEqual(H.accessOf(listing('pub', 'a', { visibility: 'private', people: ['x'] })), { visibility: 'private', people: [] });
  assert.deepEqual(H.accessOf(listing('pub', 'a', { visibility: 'people', people: ['x', 'x', '', 'y'] })), { visibility: 'people', people: ['x', 'y'] });
  // Firebase can hand an array back as an object keyed 0, 1, ...
  assert.deepEqual(H.accessOf(listing('pub', 'a', { visibility: 'people', people: { 0: 'x', 1: 'y' } })).people, ['x', 'y']);
});

test('groups and badges for the publisher, a recipient and everyone else', () => {
  const priv = listing('pub', 'p', { visibility: 'private' });
  const shared = listing('pub', 's', { visibility: 'people', people: ['bob'] });
  const open = listing('pub', 'o');
  assert.equal(H.accessGroup(priv, 'pub'), 'private');
  assert.equal(H.accessGroup(shared, 'pub'), 'by-you');
  assert.equal(H.accessGroup(shared, 'bob'), 'with-you');
  assert.equal(H.accessGroup(open, 'bob'), 'public');
  assert.equal(H.accessBadge(priv, 'pub'), 'Only you');
  assert.equal(H.accessBadge(shared, 'pub'), 'Shared');
  assert.equal(H.accessBadge(shared, 'bob'), 'Shared with you');
  assert.equal(H.accessBadge(open, 'pub'), null);
  // The server tells the publisher with canManage and a recipient with access {visibility} only.
  const served = { uid: 'pub', exp: 's', access: { visibility: 'people' }, sharedWithYou: true };
  assert.equal(H.accessBadge(served, 'bob'), 'Shared with you');
  assert.equal(H.accessCanManage({ uid: 'pub', canManage: true }, null), true);
  assert.equal(H.accessCanManage(shared, null), false);
  assert.deepEqual(H.VISIBLE_TO.map(([k]) => k), ['public', 'with-you', 'by-you', 'private']);
});

test('the summary line names people for the publisher and the publisher for a recipient', () => {
  const shared = listing('pub', 's', { visibility: 'people', people: ['a', 'b', 'c', 'd'] });
  assert.equal(H.accessSummary(shared, 'pub', { a: 'ann@x.org', b: 'ben@x.org' }), 'You and 4 people: ann@x.org, ben@x.org and 1 more');
  assert.equal(H.accessSummary(listing('pub', 's', { visibility: 'people', people: ['a'] }), 'pub', {}), 'You and 1 person');
  assert.equal(H.accessSummary(shared, 'a', {}), 'Shared with you by Sharif');
  assert.equal(H.accessSummary(listing('pub', 'p', { visibility: 'private' }), 'pub'), 'Only you');
  assert.equal(H.accessSummary(listing('pub', 'o'), 'pub'), 'Everyone');
});

test('set_access rules: de-duplicated, without the publisher, at most 200, never empty', () => {
  assert.deepEqual(H.normalizeAccess({ visibility: 'people', people: ['b', 'pub', 'b', ' c '] }, 'pub'), { visibility: 'people', people: ['b', 'c'] });
  assert.deepEqual(H.normalizeAccess({ visibility: 'private', people: ['b'] }, 'pub'), { visibility: 'private', people: [] });
  assert.deepEqual(H.normalizeAccess({ visibility: 'public' }, 'pub'), { visibility: 'public', people: [] });
  assert.throws(() => H.normalizeAccess({ visibility: 'people', people: ['pub'] }, 'pub'), /Add at least one person, or choose Only you/);
  assert.throws(() => H.normalizeAccess({ visibility: 'people', people: Array.from({ length: 201 }, (_, i) => 'u' + i) }, 'pub'), /at most 200/);
  assert.throws(() => H.normalizeAccess({ visibility: 'friends' }, 'pub'), /Choose who can see it/);
});

test('My experiments: not on the marketplace, shared with you, and the collapsed public list', () => {
  const all = [listing('pub', 'p', { visibility: 'private' }), listing('pub', 's', { visibility: 'people', people: ['bob'] }), listing('pub', 'o'),
    listing('eve', 'e', { visibility: 'people', people: ['pub'] }), listing('eve', 'x')];
  const mine = H.librarySections(all, 'pub');
  assert.deepEqual(mine.notOnMarketplace.map((L) => L.exp), ['p', 's']);
  assert.deepEqual(mine.sharedWithYou.map((L) => L.exp), ['e']);
  assert.deepEqual(mine.onMarketplace.map((L) => L.exp), ['o']);
  const visitor = H.librarySections(all, null);
  assert.deepEqual([visitor.notOnMarketplace.length, visitor.onMarketplace.length], [0, 0]);
});

test('Browse shows only public listings, even to their publisher', () => {
  const all = [listing('pub', 'p', { visibility: 'private' }), listing('pub', 's', { visibility: 'people', people: ['bob'] }), listing('pub', 'o'), listing('other', 'o2')];
  assert.deepEqual(H.marketplaceListings(all, 'pub').map((L) => L.exp), ['o', 'o2']);
  assert.deepEqual(H.marketplaceListings(all, 'bob').map((L) => L.exp), ['o', 'o2']);
  assert.deepEqual(H.marketplaceListings(all, null).map((L) => L.exp), ['o', 'o2']);
  assert.deepEqual(H.marketplaceListings(null, 'pub'), []);
});
