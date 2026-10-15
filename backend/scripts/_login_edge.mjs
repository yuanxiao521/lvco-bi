// 用 CDP 在无头 Edge 里真实填表登录（Edge 重启后 localStorage 是空的）
const APP = 'http://127.0.0.1:5174/';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let seq = 0; const pending = new Map();
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
const LOGIN = [
  '(async () => {',
  '  const sleep = (ms) => new Promise(r => setTimeout(r, ms));',
  '  for (let i = 0; i < 40 && !document.querySelector("input[type=password]"); i++) await sleep(250);',
  '  const pwd = document.querySelector("input[type=password]");',
  '  if (!pwd) return "NO_FORM";',
  '  const email = document.querySelector("input[type=email]") || document.querySelector("input:not([type=password])");',
  '  const setV = (el, v) => { const d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value"); d.set.call(el, v); el.dispatchEvent(new Event("input", { bubbles: true })); };',
  '  setV(email, "test@lvco.bi");',
  '  setV(pwd, "Test@12345");',
  '  await sleep(300);',
  '  const btn = [...document.querySelectorAll("button")].find(b => (b.textContent || "").replace(/\\s/g, "").includes("登录"));',
  '  if (!btn) return "NO_BUTTON";',
  '  btn.click();',
  '  return "CLICKED";',
  '})()',
].join('\n');
async function main() {
  const list = await (await fetch('http://127.0.0.1:9222/json/list')).json();
  const page = list.find((t) => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws failed')); });
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); }
  };
  await send(ws, 'Page.enable'); await send(ws, 'Runtime.enable');
  await send(ws, 'Page.navigate', { url: APP });
  await sleep(3500);
  console.log('login:', await evaluate(ws, LOGIN, true));
  await sleep(6000);
  await send(ws, 'Page.navigate', { url: APP });
  await sleep(3500);
  console.log('logged_in:', await evaluate(ws, '!!localStorage.getItem("access_token")'));
  ws.close();
}
main().catch((e) => { console.error('FAILED', e); process.exit(1); });