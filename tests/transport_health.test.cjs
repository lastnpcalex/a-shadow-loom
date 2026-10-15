const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/chat.js'), 'utf8');
const start = source.indexOf('let _publicTransportCheckAt = 0;');
const end = source.indexOf('\n}', start) + 2;
test('public browser health check is bounded, throttled and projects no credentials', async () => {
  let requests = 0;const reports = [];
  const context = vm.createContext({
    location: {hostname: 'portal.example.invalid'}, document: {hidden: false},
    Date, AbortController, setTimeout, clearTimeout, Number,
    console: {info: (...values) => reports.push(values), warn: (...values) => reports.push(values)},
    fetch: async (url, options) => {
      requests++;
      assert.equal(url, '/api/transport-health');
      assert.equal(options.cache, 'no-store');assert.ok(options.signal);
      assert.equal(options.headers, undefined, 'browser owns its existing cookie; code must not extract it');
      return {ok: true, json: async () => ({authenticated: true, origin_status: 101,
        socket_open: true, pong: true, cookie: 'must-never-be-logged'})};
    },
  });
  vm.runInContext(source.slice(start, end), context);
  await vm.runInContext('_checkPublicTransport()', context);
  await vm.runInContext('_checkPublicTransport()', context);
  assert.equal(requests, 1);
  assert.doesNotMatch(JSON.stringify(reports), /cookie|must-never/);
  vm.runInContext("_publicTransportCheckAt=0; location.hostname='localhost'", context);
  await vm.runInContext('_checkPublicTransport()', context);
  assert.equal(requests, 1);
});
