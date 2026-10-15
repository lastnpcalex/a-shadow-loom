const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/chat.js'), 'utf8');
function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  const end = source.indexOf('\n}', start) + 2;
  assert.ok(start >= 0 && end > start);
  return source.slice(start, end);
}
test('both foreground handlers preserve a pending handshake and recover closed sockets', () => {
  let connects = 0, closes = 0;
  const socket = {readyState: 0, close() {closes++;}};
  const context = vm.createContext({
    State: {currentConvId: 218, currentView: 'chat', ws: socket},
    WebSocket: {CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3},
    document: {hidden: false}, Date, console: {log() {}},
    _lastResumeProbeAt: 0, connectWebSocket() {connects++;},
  });
  vm.runInContext(extract('_probeWSOnResume') + '\n' + extract('_recoverForegroundConnection'), context);
  vm.runInContext("_probeWSOnResume('focus'); _recoverForegroundConnection();", context);
  assert.equal(closes, 0);
  assert.equal(connects, 0);
  socket.readyState = 3;
  vm.runInContext("_probeWSOnResume('online'); _recoverForegroundConnection();", context);
  assert.equal(connects, 2);
});
