import fs from 'node:fs';
// 快速抓取：页面已在应用 origin 且已登录，直接种草稿 → 立刻刷新 → 展开面板 → 截图。
// 目的：把'刷新'压到任务刚启动的那一两秒内，确保刷新时后端任务确实还在跑。
const IDS = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const OUT_MID = process.argv[3];
const OUT_END = process.argv[4];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let seq = 0; const pending = new Map(); const chatReqs = [];
function send(ws, method, params) {
  const id = ++seq;
  return new Promise((resolve, reject) => { pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params: params || {} })); });
}
async function evaluate(ws, expression, awaitPromise) {
  const r = await send(ws, 'Runtime.evaluate', { expression, awaitPromise: !!awaitPromise, returnByValue: true });
  if (r.exceptionDetails) throw new Error('eval failed: ' + JSON.stringify(r.exceptionDetails).slice(0, 300));
  return r.result ? r.result.value : undefined;
}
const OPEN = [
  '(async () => {',
  '  const sleep = (ms) => new Promise(r => setTimeout(r, ms));',
  '  for (let i = 0; i < 40; i++) {',
  '    const b = document.querySelector("button[title=\'AI 画布助手\']");',
  '    if (b) { b.click(); return "OPENED"; }',
  '    await sleep(150);',
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
    if (m.method === 'Network.requestWillBeSent' && m.params.request.url.includes('/ai/canvas/chat')) chatReqs.push(m.params.request.postData || '(no body)');
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); }
  };
  await send(ws, 'Page.enable'); await send(ws, 'Runtime.enable'); await send(ws, 'Network.enable');
  await send(ws, 'Emulation.setDeviceMetricsOverride', { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
  const draft = { blocks: [], chartConfigs: {}, chartResults: {}, selectedDatasourceId: '970d798d-f5da-4442-8d25-3563020d5a29', chartType: 'bar', dimensions: [], measures: [], filters: [], renderer: 'echarts', canvasId: IDS.canvas_id };
  await evaluate(ws, 'localStorage.setItem("lvco:canvas:draft:v1", ' + JSON.stringify(JSON.stringify(draft)) + '); "ok"');
  await send(ws, 'Page.reload', {});
  await sleep(2500);
  console.log('panel:', await evaluate(ws, OPEN, true));
  await sleep(1500);
  const s1 = await send(ws, 'Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(OUT_MID, Buffer.from(s1.data, 'base64'));
  console.log('saved mid');
  await sleep(16000);
  const s2 = await send(ws, 'Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(OUT_END, Buffer.from(s2.data, 'base64'));
  console.log('saved end');
  console.log('--- 浏览器自动发出的 chat 请求 ---');
  for (const b of chatReqs) console.log('  ' + b);
  const txt = await evaluate(ws, 'document.body.innerText.replace(/\\n{2,}/g, "\\n").slice(0, 1600)');
  console.log('---PAGE TEXT---'); console.log(txt);
  ws.close();
}
main().catch((e) => { console.error('FAILED', e); process.exit(1); });