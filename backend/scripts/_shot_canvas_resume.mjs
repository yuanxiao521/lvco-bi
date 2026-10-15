import fs from 'node:fs';

// 只做一件事：在'后端任务正在跑'的时刻刷新页面，然后
//   1) 用 CDP Network 域**抓浏览器真实发出的请求**（证明 resume 是前端自动发的）
//   2) 连拍两张：续收中 / 续收完成
// 这是浏览器层面的证据，不依赖后端日志或我的推断。

const IDS = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const OUT_MID = process.argv[3];
const OUT_END = process.argv[4];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let seq = 0;
const pending = new Map();
const chatReqs = [];
const sessionsReqs = [];

function send(ws, method, params) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
}

async function evaluate(ws, expression, awaitPromise) {
  const r = await send(ws, 'Runtime.evaluate', { expression, awaitPromise: !!awaitPromise, returnByValue: true });
  if (r.exceptionDetails) throw new Error('eval failed: ' + JSON.stringify(r.exceptionDetails).slice(0, 300));
  return r.result ? r.result.value : undefined;
}

const OPEN_EXPR = [
  '(async () => {',
  '  const sleep = (ms) => new Promise(r => setTimeout(r, ms));',
  '  for (let i = 0; i < 60; i++) {',
  '    const b = document.querySelector("button[title=\'AI 画布助手\']");',
  '    if (b) { b.click(); return "OPENED"; }',
  '    await sleep(250);',
  '  }',
  '  return "NO_BALL";',
  '})()',
].join('\n');

async function main() {
  const list = await (await fetch('http://127.0.0.1:9222/json/list')).json();
  const page = list.find((t) => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws failed')); });
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.method === 'Network.requestWillBeSent') {
      const r = m.params.request;
      if (r.url.includes('/ai/canvas/chat')) chatReqs.push({ url: r.url, postData: r.postData || null });
      if (r.url.includes('/sessions')) sessionsReqs.push(r.url);
    }
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      if (m.error) p.reject(new Error(JSON.stringify(m.error))); else p.resolve(m.result);
    }
  };
  await send(ws, 'Page.enable');
  await send(ws, 'Runtime.enable');
  await send(ws, 'Network.enable');
  await send(ws, 'Emulation.setDeviceMetricsOverride', { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });

  // 先确保处在应用 origin（上一次失败可能把页面留在 chrome-error:// 上，
  // 那种文档下访问 localStorage 会抛 SecurityError）
  await send(ws, 'Page.navigate', { url: 'http://127.0.0.1:5174/' });
  await sleep(3500);

  // 把草稿指向"本次这一轮"的画布（用户上次就在这张画布上）
  const draft = {
    blocks: [], chartConfigs: {}, chartResults: {},
    selectedDatasourceId: '970d798d-f5da-4442-8d25-3563020d5a29', chartType: 'bar',
    dimensions: [], measures: [], filters: [], renderer: 'echarts',
    canvasId: IDS.canvas_id,
  };
  await evaluate(ws, 'localStorage.setItem("lvco:canvas:draft:v1", ' + JSON.stringify(JSON.stringify(draft)) + '); "ok"');

  console.log('刷新前是否已登录:', await evaluate(ws, '!!localStorage.getItem("access_token")'));
  console.log('刷新前草稿里的画布:', await evaluate(ws, 'JSON.parse(localStorage.getItem("lvco:canvas:draft:v1")||"{}").canvasId'));

  // ★ 关键动作：用户按 F5。此时后端任务仍在跑。
  const t0 = Date.now();
  await send(ws, 'Page.reload', {});
  await sleep(2500);
  console.log('panel:', await evaluate(ws, OPEN_EXPR, true));

  await sleep(1500);
  const shot1 = await send(ws, 'Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(OUT_MID, Buffer.from(shot1.data, 'base64'));
  console.log('saved mid: +' + (Date.now() - t0) + 'ms');

  await sleep(22000);
  const shot2 = await send(ws, 'Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(OUT_END, Buffer.from(shot2.data, 'base64'));
  console.log('saved end: +' + (Date.now() - t0) + 'ms');

  console.log('--- 浏览器发出的 chat 请求 ---');
  for (const r of chatReqs) console.log(r.url + '  body=' + r.postData);
  console.log('--- 浏览器发出的 sessions 请求 ---');
  for (const u of sessionsReqs) console.log(u);
  const txt = await evaluate(ws, 'document.body.innerText.replace(/\\n{2,}/g, "\\n").slice(0, 1400)');
  console.log('---PAGE TEXT---');
  console.log(txt);
  ws.close();
}

main().catch((e) => { console.error('FAILED', e); process.exit(1); });
