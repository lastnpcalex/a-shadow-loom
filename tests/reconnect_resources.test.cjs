const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/chat.js'), 'utf8');
const start = source.indexOf('function connectWebSocket(');
const end = source.indexOf('\n}', start) + 2;

test('snapshot recovery refreshes metadata without rendering a hidden tree',async()=>{
  const offset=source.indexOf('async function _syncMessagesForStreaming(');
  const boundary=source.indexOf('\n}',offset)+2;
  let reads=0,renders=0,current=true;
  const context=vm.createContext({State:{currentView:'chat'},TREE:{},
    _fetchCursorBranchMessages:async()=>{reads++;return current;},renderTree:()=>{renders++;}});
  vm.runInContext(source.slice(offset,boundary),context);
  await vm.runInContext('_syncMessagesForStreaming(161)',context);
  assert.equal(reads,1);assert.equal(renders,0);
  context.State.currentView='tree';await vm.runInContext('_syncMessagesForStreaming(161)',context);
  assert.equal(renders,1);
  current=false;await vm.runInContext('_syncMessagesForStreaming(161)',context);
  assert.equal(renders,1,'stale branch read cannot repaint a tree');
});

test('rapid upgraded socket failures retain backoff and release worker URLs', () => {
  let now = 100000, scheduled, created = 0, revoked = 0, terminated = 0;
  const delays = [];
  class Socket {
    static OPEN = 1;
    readyState = 1;
    send() {}
    close() { this.readyState = 3; this.onclose?.({code:1000}); }
  }
  class Clock extends Date { static now() { return now; } }
  const State = {currentConvId:161, currentView:'chat', ws:null, isStreaming:false};
  const context = vm.createContext({State, WebSocket:Socket, Date:Clock,
    URLSearchParams, Blob, location:{protocol:'https:',host:'fixture.test'},
    URL:{createObjectURL:()=>{created++;return 'blob:fixture';}, revokeObjectURL:()=>revoked++},
    Worker:class {terminate(){terminated++;}}, console:{log(){},warn(){},error(){}},
    _wsReconnectTimer:null, _wsReconnectDelay:2000, _loomClientInstanceId:'fixture', _pendingLaunch:null,
    _sameConvId:(a,b)=>a===b, _sameMessageId:(a,b)=>a===b,
    _followedGenerationProbe:()=>({}), refreshAgentRuns:()=>{}, _flushPendingGenerate:()=>{},
    _scheduleDisconnectedRecovery:()=>{}, _checkPublicTransport:()=>{}, _showConnectionStatus:()=>{}, _reportPublicSocket:()=>{},
    setTimeout:(fn,delay)=>{scheduled=fn;delays.push(delay);return 1}, clearTimeout:()=>{},
  });
  vm.runInContext(source.slice(start,end), context);
  vm.runInContext('connectWebSocket(161)',context);
  for(let n=0;n<8;n++) {
    State.ws.onopen();
    now+=100;
    State.ws.close();
    scheduled();
  }
  assert.deepEqual(delays.filter(delay=>delay!==10000),[100,500,1000,2000,4000,8000,8000,8000]);
  assert.equal(created,revoked);
  assert.equal(terminated,created);
  State.ws.onopen();
  vm.runInContext('connectWebSocket(161)',context);
  assert.equal(terminated,created,'replacement must terminate its previous worker immediately');
});

test('pending handshakes have a deadline, recover over HTTP and ignore late close retries',()=>{
  const timers=new Map();let next=0,recovery=0;
  class Socket {
    static CONNECTING=0;static OPEN=1;
    readyState=0;
    close(){this.readyState=2;}
  }
  const State={currentConvId:161,currentView:'chat',ws:null,isStreaming:false};
  const context=vm.createContext({State,WebSocket:Socket,Date,URLSearchParams,location:{protocol:'https:',host:'fixture.test'},
    console:{log(){},warn(){},error(){}},_wsReconnectTimer:null,_loomClientInstanceId:'fixture',_pendingLaunch:null,
    _sameConvId:(a,b)=>a===b,_sameMessageId:(a,b)=>a===b,
    _scheduleDisconnectedRecovery:()=>recovery++, _reportPublicSocket:()=>{},_showConnectionStatus:()=>{},
    setTimeout:(fn,delay)=>{const id=++next;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(source.slice(start,end),context);
  vm.runInContext('connectWebSocket(161)',context);
  const first=State.ws;
  assert.equal(recovery,1,'HTTP status starts while the handshake is pending');
  assert.equal(timers.get(first._handshakeTimer).delay,10000);
  timers.get(first._handshakeTimer).fn();
  assert.equal(first._replaced,true);
  const retry=vm.runInContext('_wsReconnectTimer',context);
  assert.equal(timers.get(retry).delay,500);
  const scheduledCount=next;
  first.onclose({code:1006,wasClean:false});
  assert.equal(next,scheduledCount,'late native close cannot schedule duplicate retries');
  timers.get(retry).fn();
  assert.notEqual(State.ws,first);
  assert.equal(State.ws._reconnectAttempt,1);
});
