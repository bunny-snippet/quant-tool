const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const css = fs.readFileSync(path.join(__dirname, '../static/surveys/theme.css'), 'utf8');

test('dark donut centres use a valid pseudo-element selector and theme tokens', () => {
  assert.doesNotMatch(css, /:is\([^)]*::?(?:before|after)/);
  assert.match(css, /html\[data-theme="dark"\] :is\(\.bi-client-donut, \.bi-device-ring\)::before\s*\{\s*background: var\(--surface\)/);
  assert.match(css, /:is\(\.bi-client-donut, \.bi-device-ring\) b\s*\{\s*color: var\(--navy\)/);
  assert.match(css, /:is\(\.bi-client-donut, \.bi-device-ring\) small\s*\{\s*color: var\(--muted\)/);
});

test('dark centre value and label tokens have readable contrast', () => {
  const tokens = css.match(/html\[data-theme="dark"\]\s*\{([^}]+)/)[1];
  const value = name => tokens.match(new RegExp('--'+name+':\\s*(#[0-9a-fA-F]{6})'))[1];
  const luminance = hex => {
    const rgb = hex.slice(1).match(/../g).map(n => parseInt(n,16)/255).map(n => n <= .04045 ? n/12.92 : ((n+.055)/1.055)**2.4);
    return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;
  };
  const background = luminance(value('surface'));
  for (const token of ['navy','muted']) {
    const foreground = luminance(value(token));
    const contrast = (Math.max(foreground,background)+.05)/(Math.min(foreground,background)+.05);
    assert.ok(contrast >= 4.5, token+' contrast must be at least 4.5:1');
  }
});
