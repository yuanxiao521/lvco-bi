/**
 * 画布助手「刷新自动续收」回归测试。
 *
 * 【缺口】画布的后端任务早就后台化了（断线不影响任务），前端也早就有
 * resumeStream()，但**没有人调用它**——刷新页面后任务仍在跑，用户看到的
 * 却是"进度停了"。对话助手在 loadMessages 里有这个触发
 * （sess.running && !isStreaming → streamChat(sid,'','',true)），画布这边缺失。
 *
 * 【本次补齐】onCanvasReady(canvasId, ctx) 末尾：会话 running=true → 自动 resumeStream。
 * 本测试守住三条契约：
 *   1) running=true 且传入 ctx → 自动发起 resume 请求（真正触发，不是"有函数就行"）
 *   2) running=false → 不发请求（避免无谓请求与误报"已恢复"）
 *   3) 不传 ctx（旧调用方）→ 行为不变，只加载历史（向后兼容）
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../../api/ai', () => ({
  listCanvasSessions: vi.fn(async () => []),
  listMessages: vi.fn(async () => []),
}));

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn() },
  // resumeStream 会取 access token 拼 Authorization 头——补上，否则 mock 不完整
  tokenStore: { getAccess: () => 'test-token', setAccess: vi.fn(), clear: vi.fn() },
}));

import { listCanvasSessions, listMessages } from '../../api/ai';
import { useCanvasAssistantStore } from '../../stores/canvasAssistantStore';

const ctxFor = (canvasId: string) => ({
  canvasId,
  datasourceId: null,
  fieldMeta: [],
  canvasBlocks: [],
  allDatasources: [],
} as never);

/** 只含 status(resumed) + done 的 SSE 响应 */
function sseResponse(): Response {
  const payload = [
    'data: {"type":"status","message":"已恢复连接，正在续收后台任务","resumed":true}',
    '',
    'data: {"type":"done"}',
    '',
    '',
  ].join('\n');
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(payload));
      controller.close();
    },
  });
  return { ok: true, status: 200, body } as unknown as Response;
}

/** 把任意事件数组拼成一条 SSE 响应（用于验证各类事件在续收路径上的处理）。 */
function sseFrom(events: Record<string, unknown>[]): Response {
  const payload = events.map((e) => 'data: ' + JSON.stringify(e) + '\n\n').join('');
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(payload));
      controller.close();
    },
  });
  return { ok: true, status: 200, body } as unknown as Response;
}

/** 让 store 处于"刷新后刚进画布"的初始态 */
async function enterCanvas(canvasId: string, sessionId: string, running: boolean) {
  vi.mocked(listCanvasSessions).mockResolvedValue(
    [{ id: sessionId, entry: 'canvas', running }] as never,
  );
  vi.mocked(listMessages).mockResolvedValue([] as never);
  useCanvasAssistantStore.setState({
    curSessionId: null,
    sessionsLoaded: false,
    isStreaming: false,
    canvasSessions: [],
    messages: [],
    steps: [],
    meta: null,
  });
  // refreshSessions 负责设置模块级 activeCanvasId —— onCanvasReady 靠它做归属校验
  await useCanvasAssistantStore.getState().refreshSessions(canvasId);
}

describe('画布助手：刷新自动续收', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async () => sseResponse());
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    useCanvasAssistantStore.setState({ isStreaming: false });
  });

  it('会话 running=true 且传入 ctx → 自动发起 resume 续收', async () => {
    await enterCanvas('canvas-R', 'sess-R', true);

    await useCanvasAssistantStore.getState().onCanvasReady('canvas-R', ctxFor('canvas-R'));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, { body: string }];
    expect(String(url)).toContain('/ai/canvas/chat');
    const body = JSON.parse(String(init.body));
    expect(body.resume).toBe(true);
    expect(body.session_id).toBe('sess-R');
  });

  it('会话 running=false → 不发 resume 请求', async () => {
    await enterCanvas('canvas-N', 'sess-N', false);

    await useCanvasAssistantStore.getState().onCanvasReady('canvas-N', ctxFor('canvas-N'));

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('不传 ctx（旧调用方）→ 只加载历史，不发 resume（向后兼容）', async () => {
    await enterCanvas('canvas-C', 'sess-C', true);

    await useCanvasAssistantStore.getState().onCanvasReady('canvas-C');

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('续收会填充工作台（meta + steps）—— 事件表只有一份，两条路径共享', async () => {
    // 【回归】原先 resumeStream 只处理 status/message/error/done，
    // 于是刷新续收后 meta/steps 全空 → 面板里的 agent 工作台整个消失。
    // 现在 send 与 resumeStream 共用同一个 applyCanvasEvent，这里钉死续收路径也有工作台。
    await enterCanvas('canvas-W', 'sess-W', true);
    fetchMock.mockResolvedValue(
      sseFrom([
        { type: 'status', resumed: true, message: '已恢复连接' },
        { type: 'intent', intent: 'data_query', confidence: 0.9 },
        { type: 'progress', round: 1, index: 1, total: 3, title: '执行查询', status: 'start' },
        { type: 'tool_call', name: 'run_query', args: { sql: 'select 1' } },
        { type: 'tool_result', name: 'run_query', result: '{"rows":1}' },
        { type: 'message', delta: '查询完成' },
        { type: 'done' },
      ]),
    );

    await useCanvasAssistantStore.getState().onCanvasReady('canvas-W', ctxFor('canvas-W'));

    const s = useCanvasAssistantStore.getState();
    expect(s.meta?.intent).toBe('data_query');
    expect(s.steps.length).toBeGreaterThan(0);
    // FeedStep 未声明 seq（生产代码在 progress 分支里动态挂上），故此处按结构断言
    expect(s.steps.some((st) => (st as unknown as { seq?: string }).seq === '1/3')).toBe(true);
    expect(
      s.steps.some((st) => st.tools.some((t) => t.name === 'run_query' && t.status === 'ok')),
    ).toBe(true);
    // 文本仍要正常流入
    expect(s.messages.some((m) => m.content.includes('查询完成'))).toBe(true);
  });

  it('intent 事件带 source=derived → meta.intentSource 保留（UI 标"（推断）"，不冒充模型判断）', async () => {
    await enterCanvas('canvas-D', 'sess-D', true);
    fetchMock.mockResolvedValue(
      sseFrom([
        { type: 'status', resumed: true, message: '已恢复连接' },
        { type: 'intent', intent: 'canvas_edit', source: 'derived' },
        { type: 'decision', action: 'call_analysis', tool: 'dispatch_analysis', reason: '落图' },
        { type: 'done' },
      ]),
    );

    await useCanvasAssistantStore.getState().onCanvasReady('canvas-D', ctxFor('canvas-D'));

    const s = useCanvasAssistantStore.getState();
    expect(s.meta?.intent).toBe('canvas_edit');
    expect((s.meta as { intentSource?: string } | null)?.intentSource).toBe('derived');
    // decision 事件不得覆盖 intent（理解层 vs 执行层，两层各自留存）
    expect(s.meta?.decision).toBe('call_analysis');
  });
});
