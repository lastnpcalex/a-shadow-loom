const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/chat.js'),'utf8');
const start=source.indexOf('let _transportReportWindow =');
const end=source.indexOf('\nfunction _showConnectionStatus',start);

test('browser reports only fixed transport fields, deduplicates and bounds requests',async()=>{
  const calls=[];
  const context=vm.createContext({location:{hostname:'portal.example.invalid'},Date,Set,Number,Math,AbortController,
    setTimeout,clearTimeout,fetch:async(url,options)=>{calls.push({url,options});return {status:204};}});
  vm.runInContext(source.slice(start,end),context);
  context.socket={_transportTrace:'a'.repeat(32),_transportStartedAt:Date.now()-300,
    _openedAt:Date.now()-200,_lastActivity:Date.now()-100};
  context.event={code:1006,wasClean:false,reason:'private-reason',cookie:'private-cookie',url:'private-url',data:'private-message'};
  vm.runInContext('_reportPublicSocket(socket,"close",event);_reportPublicSocket(socket,"close",event)',context);
  await new Promise(setImmediate);
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/transport-report');assert.equal(calls[0].options.mode,'same-origin');
  const report=JSON.parse(calls[0].options.body);
  assert.deepEqual(Object.keys(report).sort(),['clean','code','elapsed_ms','idle_ms','opened','stage','trace']);
  assert.equal(report.opened,true);assert.equal(report.code,1006);
  assert.doesNotMatch(calls[0].options.body,/private/);
  assert.deepEqual(Object.keys(calls[0].options.headers),['Content-Type']);
  for(let i=0;i<20;i++) {
    context.socket={_transportTrace:'b'.repeat(32),_transportStartedAt:Date.now()};
    vm.runInContext('_reportPublicSocket(socket,"error",event)',context);
    await new Promise(setImmediate);
  }
  assert.equal(calls.length,12);
  vm.runInContext('location.hostname="localhost";_transportReportWindow.count=0;socket._transportReported=new Set();_reportPublicSocket(socket,"open")',context);
  assert.equal(calls.length,12);
});

test('hanging reporting requests cannot create an unbounded parallel queue',async()=>{
  let requests=0;const pending=[];const timers=[];
  const context=vm.createContext({location:{hostname:'portal.example.invalid'},Date,Set,Number,Math,AbortController,
    setTimeout:(fn,ms)=>{assert.equal(ms,3000);timers.push(fn);return timers.length;},clearTimeout:()=>{},
    fetch:async(url,options)=>{requests++;return new Promise((resolve,reject)=>{
      options.signal.addEventListener('abort',()=>reject(new Error('private error')));pending.push(resolve);
    });}});
  vm.runInContext(source.slice(start,end),context);
  for(let i=0;i<20;i++) {
    context.socket={_transportTrace:'c'.repeat(32),_transportStartedAt:Date.now()};
    vm.runInContext('_reportPublicSocket(socket,"error")',context);
  }
  assert.equal(requests,2);
  timers.forEach(fn=>fn());await new Promise(setImmediate);
  assert.equal(vm.runInContext('_transportReportWindow.pending',context),0);
});
