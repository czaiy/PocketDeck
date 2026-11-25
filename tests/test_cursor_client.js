// 手机端光标事件回归: 直接执行 index.html 中的真实输入协议代码, 无第三方依赖。
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('D:/AI-agent/KZQ/pc-remote/static/index.html', 'utf8');
const start = html.indexOf("const textEl = $('#textInput');");
const end = html.indexOf('// ---- ⌨ 点按联动', start);
if (start < 0 || end < 0) throw new Error('未找到手机输入协议代码段');
const source = html.slice(start, end);
const padStart = html.indexOf('function padUp(e) {');
const padEnd = html.indexOf("pad.addEventListener('pointerup', padUp);", padStart);
if (padStart < 0 || padEnd < 0) throw new Error('未找到触控板 padUp 代码段');
const padSource = html.slice(padStart, padEnd);

const listeners = {};
const textListeners = {};
const textEl = {
  value: '', selectionStart: 0, selectionEnd: 0, readOnly: false,
  addEventListener(name, fn) { textListeners[name] = fn; },
  setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
  focus() { }
};
const document = {
  activeElement: textEl, visibilityState: 'visible', hidden: false,
  addEventListener(name, fn) { listeners[name] = fn; }
};
const sent = [];
let apiHandler = () => Promise.resolve({ ok: false });
const context = vm.createContext({
  document, Intl, Date, Promise, JSON, Math, Number, Map, performance,
  setTimeout, clearTimeout,
  $: () => textEl,
  token: 'test', setConn() {}, toast() {}, updatePadOv() {},
  pointers: new Map(), gesture: { quick: true, two: false },
  dragMode: false, kbLink: true, flush() {},
  fetch(path, options) {
    sent.push({ path, body: JSON.parse(options.body) });
    return Promise.resolve({ status: 200 });
  },
  api(path, body) { return apiHandler(path, body); }
});
vm.runInContext(source, context, { filename: 'index-inline.js' });
vm.runInContext(padSource, context, { filename: 'padup-inline.js' });

const checks = [];
function check(name, cond, detail = '') {
  checks.push({ name, ok: !!cond });
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (detail ? ` [${detail}]` : ''));
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function reset(text = 'abcdef', caret = 2) {
  vm.runInContext(`
    clearTimeout(syncTimer); clearTimeout(cursorTimer); clearTimeout(readTimer);
    syncTimer = cursorTimer = readTimer = null;
    textEl.value = ${JSON.stringify(text)}; lastSynced = textEl.value;
    textEl.selectionStart = textEl.selectionEnd = ${caret};
    document.activeElement = textEl; document.visibilityState = 'visible';
    mirrorFull = true; pendingIns = null; queuedOps = 0; chainIdle = true;
    composing = false; textEl.readOnly = false; editCaret = -1;
    lastUserOp = 0; focusInFlight = false; lastFocusAt = 0;
  `, context);
  sent.length = 0;
  apiHandler = () => Promise.resolve({ ok: false });
}

(async () => {
  // 同位置连续点击不依赖 selectionchange, 两次都必须发送同一绝对锚点。
  reset();
  textListeners.pointerup();
  await sleep(120);
  textListeners.pointerup();
  await sleep(120);
  const positions = sent.filter(x => x.path === '/api/stream').map(x => x.body.at);
  check('同位置重复点击均发送定位', JSON.stringify(positions) === '[2,2]', JSON.stringify(positions));

  // 周期读回在途时发生点击: 旧 caret=6 不得覆盖用户点击的 2。
  reset();
  let resolveRead;
  apiHandler = () => new Promise(resolve => { resolveRead = resolve; });
  vm.runInContext('syncCheckTick()', context);
  await sleep(0);
  textListeners.pointerup();
  await sleep(10);
  resolveRead({ ok: true, text: 'abcdef', caret: 6 });
  await sleep(20);
  check('旧读回不覆盖新点击', textEl.selectionStart === 2, `caret=${textEl.selectionStart}`);
  await sleep(100);
  check('旧读回丢弃后仍发送点击位置', sent.some(x => x.body.at === 2));

  // 新鲜周期读回只同步文本, 不改变手机当前光标。
  reset();
  let capturedBody = null;
  apiHandler = (path, body) => { capturedBody = body; return Promise.resolve({ ok: true, text: 'abcdef', caret: 6 }); };
  vm.runInContext('syncCheckTick()', context);
  await sleep(20);
  check('周期对账保留手机光标', textEl.selectionStart === 2, `caret=${textEl.selectionStart}`);
  check('周期巡逻不触发剪贴板兜底', capturedBody && capturedBody.clip === 0, JSON.stringify(capturedBody));

  // 用户 3 秒内有操作: 周期巡逻必须推迟, 不发任何读回请求。
  reset();
  let patrolCalls = 0;
  apiHandler = () => { patrolCalls++; return Promise.resolve({ ok: false }); };
  vm.runInContext('lastUserOp = Date.now(); syncCheckTick()', context);
  await sleep(30);
  check('活跃期暂停周期巡逻', patrolCalls === 0, `calls=${patrolCalls}`);

  // focus_input 在途/刚完成 2s 内: 重复 focus 事件不再叠加聚焦请求。
  reset();
  let focusCalls = 0;
  apiHandler = path => {
    if (path === '/api/cmd') { focusCalls++; }
    return Promise.resolve({ ok: true, text: 'abcdef', caret: 5 });
  };
  textListeners.focus();
  await sleep(10);
  textListeners.focus();
  await sleep(10);
  check('聚焦请求节流', focusCalls === 1, `calls=${focusCalls}`);

  // 初始聚焦预填仍采用电脑真实光标。
  reset();
  vm.runInContext("applyReadback({ok:true,text:'abcdef',caret:5})", context);
  check('聚焦预填仍采用电脑光标', textEl.selectionStart === 5, `caret=${textEl.selectionStart}`);

  // 触控板点击电脑输入框触发的 focus_read 在途时, 用户手机点击必须作废旧结果。
  reset();
  let resolvePadRead;
  apiHandler = path => {
    if (path === '/api/mouse') return Promise.resolve({ ok: true, msg: 'input:editor' });
    return new Promise(resolve => { resolvePadRead = resolve; });
  };
  vm.runInContext(`
    pointers.set(1, { x: 0, y: 0, t: performance.now(), moved: 0 });
    gesture = { quick: true, two: false };
    kbLink = true; dragMode = false;
    padUp({ pointerId: 1 });
  `, context);
  await sleep(10);              // mouse 分支完成, focus_read 已在途
  textListeners.pointerup();    // 用户同时点击手机输入框: caretGen++
  await sleep(10);
  resolvePadRead({ ok: true, text: 'zzzzzz', caret: 6 });
  await sleep(30);
  check('触控板旧读回不覆盖手机点击',
    textEl.value === 'abcdef' && textEl.selectionStart === 2,
    `value=${textEl.value} caret=${textEl.selectionStart}`);

  // 点按同步被服务端限频跳过(chromium-blind)后自动补发, 最后一次点击必然同步。
  reset();
  let clip1Calls = 0, replayed = false;
  apiHandler = (path, body) => {
    if (path === '/api/cmd') {
      if (body && body.clip === 1) {
        clip1Calls++;
        if (!replayed) { replayed = true; return Promise.resolve({ ok: true, reason: 'chromium-blind', text: null }); }
        return Promise.resolve({ ok: true, text: '电脑端同步文本', caret: 4, reason: '' });
      }
      return Promise.resolve({ ok: false });
    }
    return Promise.resolve({ ok: false });
  };
  textListeners.pointerdown();   // 点按: clip=1 → 第一次被限频跳过
  await sleep(30);
  check('限频跳过立即无反应(预期行为)', clip1Calls === 1, `clip调用=${clip1Calls}`);
  await sleep(420);              // 补发定时器 350ms 后重放
  check('限频跳过自动补发同步',
    clip1Calls === 2 && textEl.value === '电脑端同步文本',
    `clip调用=${clip1Calls} value=${textEl.value}`);

  vm.runInContext('clearTimeout(syncTimer); clearTimeout(cursorTimer); clearTimeout(readTimer)', context);
  const failed = checks.filter(x => !x.ok).length;
  console.log(`\n${checks.length - failed}/${checks.length} passed`);
  process.exitCode = failed ? 1 : 0;
})().catch(err => {
  console.error(err);
  process.exitCode = 1;
});
