/**
 * HITL 确认卡片的前端链路守卫。
 *
 * 这条链路横跨"后端事件 → store 状态 → 卡片渲染 → 点击回传 ui_action"四处，
 * **任一处断了都是静默失效**：卡片不出现、或出现但点了没反应——不会报错。
 *
 * 后端那一半已由 `tests/test_confirm_gate.py` 守住；本文件守住前端这一半：
 *   1. store 收到 `confirm_request` → 写入 pendingConfirm
 *   2. 点「确认执行」→ 下一请求体带 ui_action={type:"confirm"}（后端守卫据此短路恢复）
 *   3. 点「取消」→ 带 {type:"cancel"} 且卡片被清掉
 *   4. 切画布 → 卡片作废（防串台）
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../../api/ai', () => ({
  listCanvasSessions: vi.fn(async () => []),
  listMessages: vi.fn(async () => []),
}));

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn() },
  // store 里用 tokenStore.getAccess() 取 Bearer token；漏 mock 会让 send 在 fetch 之前就抛错
  tokenStore: { getAccess: () => 'test-token', setTokens: vi.fn(), clear: vi.fn() },
}));

import { useCanvasAssistantStore } from '../../stores/canvasAssistantStore';

/** 造一个 SSE 响应体（store 内部用 response.body.getReader() 逐行解析）。 */
function sseResponse(events: unknown[]): Response {
  const payload = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('');
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(payload));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

const ctx = {
  canvasId: 'canvas-1',
  datasourceId: 'ds-1',
  fieldMeta: [],
  canvasBlocks: [],
  allDatasources: [],
} as never;

function lastFetchBody(fetchMock: ReturnType<typeof vi.fn>): Record<string, unknown> {
  const calls = fetchMock.mock.calls;
  const init = calls[calls.length - 1]?.[1] as RequestInit;
  return JSON.parse(String(init.body));
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  useCanvasAssistantStore.setState({
    messages: [],
    pendingConfirm: null,
    isStreaming: false,
    curSessionId: 'sess-1',
    sessionsLoaded: true,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  // 画布 store 没有 clearStream（那是对话页的）；这里直接复位状态。
  // streamingLock 是模块级变量，由 store 自身的 finally 复位（正常流程都会走到）。
  useCanvasAssistantStore.setState({ isStreaming: false, pendingConfirm: null });
});

describe('HITL 确认卡片（前端）', () => {
  it('收到 confirm_request → 写入 pendingConfirm（卡片的数据来源）', async () => {
    fetchMock.mockResolvedValueOnce(sseResponse([
      { type: 'text', content: '这会清空画布上的全部块，确定吗？' },
      { type: 'confirm_request', goal: '清空画布', question: '确定要清空画布上的全部块吗？' },
      { type: 'done', degraded: false },
    ]));

    await useCanvasAssistantStore.getState().send('清空画布', ctx);

    expect(useCanvasAssistantStore.getState().pendingConfirm).toEqual({
      goal: '清空画布',
      question: '确定要清空画布上的全部块吗？',
    });
  });

  it('普通回合（无 confirm_request）不会凭空弹出卡片', async () => {
    fetchMock.mockResolvedValueOnce(sseResponse([
      { type: 'text', content: '已完成分析。' },
      { type: 'done', degraded: false },
    ]));

    await useCanvasAssistantStore.getState().send('分析一下', ctx);

    expect(useCanvasAssistantStore.getState().pendingConfirm).toBeNull();
  });

  it('点「确认执行」→ 请求体带 ui_action={type:"confirm"}（后端守卫据此短路恢复）', async () => {
    fetchMock.mockResolvedValueOnce(sseResponse([{ type: 'done', degraded: false }]));

    await useCanvasAssistantStore.getState().send('确认', ctx, { type: 'confirm' });

    expect(lastFetchBody(fetchMock).ui_action).toEqual({ type: 'confirm' });
  });

  it('点「取消」→ 请求体带 ui_action={type:"cancel"} 且卡片清空', async () => {
    useCanvasAssistantStore.setState({ pendingConfirm: { goal: '清空画布', question: '确定吗？' } });
    fetchMock.mockResolvedValueOnce(sseResponse([{ type: 'done', degraded: false }]));

    // 组件里的按钮行为：先清卡片，再发带 cancel 信号的请求
    useCanvasAssistantStore.getState().clearPendingConfirm();
    await useCanvasAssistantStore.getState().send('取消', ctx, { type: 'cancel' });

    expect(lastFetchBody(fetchMock).ui_action).toEqual({ type: 'cancel' });
    expect(useCanvasAssistantStore.getState().pendingConfirm).toBeNull();
  });

  it('无 ui_action 的普通消息，请求体 ui_action 为 null（不误带确认信号）', async () => {
    fetchMock.mockResolvedValueOnce(sseResponse([{ type: 'done', degraded: false }]));

    await useCanvasAssistantStore.getState().send('画个柱状图', ctx);

    expect(lastFetchBody(fetchMock).ui_action).toBeNull();
  });

  it('切换画布 → 待确认卡片作废（挂起槽在后端按 session 隔离，前端不该留着旧卡片）', () => {
    useCanvasAssistantStore.setState({ pendingConfirm: { goal: '清空画布', question: '确定吗？' } });

    useCanvasAssistantStore.getState().resetForCanvas({
      canvasId: 'canvas-2',
      datasourceId: null,
      fieldMeta: [],
      canvasBlocks: [],
      allDatasources: [],
    } as never);

    expect(useCanvasAssistantStore.getState().pendingConfirm).toBeNull();
  });
});
