// The persistent skip list, run against the shipped source with a stub store.
const assert = require('assert');
const { extract } = require('./extract');

const store = {};
global.window = { localStorage: {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
} };
let media = 'anime';
global.mediaType = () => media;
let actionable = true;
global.isActionable = () => actionable;
const toasts = [];
global.showToast = (m, t) => toasts.push([m, t || 'success']);
global.btnForgetIgnored = { textContent: '', style: {} };
global.ignoredKeys = new Set();

const src = extract(['IGNORE_KEY', 'entryKey', 'readIgnoreStore', 'loadIgnored',
                     'saveIgnored', 'enabledFor', 'updateIgnoredLabel']);
// `const`/`function` at top level of eval stay local, so expose what we need.
eval(src + `
  global.entryKey = entryKey;
  global.loadIgnored = loadIgnored;
  global.saveIgnored = saveIgnored;
  global.enabledFor = enabledFor;
  global.updateIgnoredLabel = updateIgnoredLabel;
`);

let checks = 0;
function eq(a, b, what) { checks++; assert.strictEqual(a, b, what); }
function ok(c, what) { checks++; assert.ok(c, what); }

// 1. a fresh browser has an empty list and every actionable row enabled
eq(loadIgnored().size, 0, 'a fresh browser skips nothing');
eq(enabledFor({ id: 1, title: 'Lain' }), true, 'and enables an actionable row');

// 2. remembering a row keeps it out of future lists
ignoredKeys.add(entryKey({ id: 1, title: 'Lain' }));
saveIgnored();
eq(enabledFor({ id: 1, title: 'Lain' }), false, 'a remembered row stays unchecked');
eq(enabledFor({ id: 2, title: 'Kaiba' }), true, 'and its neighbour is untouched');

// 3. it survives a reload
ignoredKeys = new Set();
ignoredKeys = loadIgnored();
eq(ignoredKeys.size, 1, 'the skip list must survive a reload');
eq(enabledFor({ id: 1, title: 'Lain' }), false, 'and still applies');

// 4. the lists are per media type: a MAL id means a different work in each
media = 'manga';
ignoredKeys = loadIgnored();
eq(ignoredKeys.size, 0, 'manga must not inherit the anime skip list');
eq(enabledFor({ id: 1, title: 'Berserk' }), true, 'so the same id is free here');
ignoredKeys.add(entryKey({ id: 9, title: 'Berserk' }));
saveIgnored();
media = 'anime';
eq(loadIgnored().size, 1, 'saving manga must not clobber anime');

// 5. a row with no id falls back to the title, case-insensitively
ignoredKeys = new Set([entryKey({ title: 'Texhnolyze' })]);
eq(enabledFor({ title: '  texhnolyze ' }), false, 'title keys ignore case and padding');

// 6. an unresolved row is disabled whatever the skip list says
actionable = false;
eq(enabledFor({ id: 777, title: 'nowhere' }), false, 'an unusable row is never enabled');
actionable = true;

// 7. the Forget button hides itself when the list is empty
ignoredKeys = new Set();
updateIgnoredLabel();
eq(btnForgetIgnored.style.display, 'none', 'nothing skipped means no button');
eq(btnForgetIgnored.textContent, 'Forget 0 skipped');
ignoredKeys = new Set(['title:x', 'title:y']);
updateIgnoredLabel();
eq(btnForgetIgnored.style.display, '', 'it appears once something is skipped');
eq(btnForgetIgnored.textContent, 'Forget 2 skipped', 'and counts them');

// 8. blocked storage must not throw, only warn
global.window.localStorage.setItem = () => { throw new Error('QuotaExceeded'); };
saveIgnored();
ok(toasts.some(t => t[1] === 'error'), 'a blocked write must surface as an error toast');

console.log('  skip list       ' + checks + ' assertions');
