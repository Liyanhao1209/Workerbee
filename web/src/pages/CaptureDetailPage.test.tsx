/**
 * 捕获记录详情页的草案卡片：
 * - 节点 / 连线清单与「观察到的 / 推断的」溯源徽标如实渲染；
 * - 「待配置」与复核降级记录可见；
 * - 点「采用为流程草稿」调 adopt endpoint（as_template=false），成功后显示已采用落点。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { CaptureDraft, CaptureRunDetail, WorkflowDefinition } from '../api/types';

const api = vi.hoisted(() => ({
  capture: {
    createRun: vi.fn(),
    createRunFromTask: vi.fn(),
    listRuns: vi.fn(),
    getRun: vi.fn(),
    generateDraft: vi.fn(),
    getDraft: vi.fn(),
    adoptDraft: vi.fn(),
    rejectDraft: vi.fn(),
  },
  workflows: { get: vi.fn() },
  templates: { get: vi.fn() },
}));

vi.mock('../api/endpoints', () => api);

import { CaptureDetailPage } from './CaptureDetailPage';

const PENDING_DRAFT: CaptureDraft = {
  draft_id: 'd-1',
  run_id: 'run-1',
  name: '抓取并汇总',
  description: '两步流程',
  payload: {
    nodes: [
      {
        node_id: 'n1',
        name: '抓取 issue',
        role: null,
        system_prompt: null,
        profiles: [{ harness_ref: 'claude', model_name: null, credential_ref: null, reasoning_effort: null }],
        skill_refs: [],
        tool_refs: [],
        required_inputs: [],
        basis: 'observed',
        evidence: ['E12'],
      },
      {
        node_id: 'n2',
        name: '写周报',
        role: '汇总',
        system_prompt: null,
        profiles: [],
        skill_refs: [],
        tool_refs: [],
        required_inputs: [],
        basis: 'inferred',
        evidence: [],
      },
    ],
    edges: [{ from_node: 'n1', to_node: 'n2', output_contract: [], basis: 'inferred', evidence: [] }],
    notes: [],
  },
  validation: {
    ok: true,
    summary: '结构可执行',
    error: null,
    diagnostics: [],
    pending_config: ['写周报 的 harness'],
    downgrades: [{ target: '写周报', reason: '材料里没有对应记录' }],
  },
  status: 'pending',
  adopted_ref: null,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

const DETAIL: CaptureRunDetail = {
  run: {
    run_id: 'run-1',
    name: '抓取周报',
    origin: 'live',
    workflow_id: 'wf-1',
    task_id: 'task-1',
    profile: { harness_ref: 'claude', instructions: '抓取并汇总本周 issue' },
    status: 'completed',
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  },
  task_state: 'succeeded',
  material: null,
  drafts: [PENDING_DRAFT],
};

/** 详情由用例控制：采用成功后换成 adopted 版本，模拟 onChanged 重拉的结果。 */
let detail: CaptureRunDetail = DETAIL;

function renderPage(): void {
  render(
    <MemoryRouter initialEntries={['/captures/run-1']}>
      <Routes>
        <Route path="/captures/:runId" element={<CaptureDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  detail = DETAIL;
  vi.clearAllMocks();
  api.capture.getRun.mockImplementation(async () => detail);
});

describe('草案卡片', () => {
  it('渲染节点与连线清单，溯源徽标区分「观察到的」与「推断的」', async () => {
    renderPage();

    expect(await screen.findByText('流程草案「抓取并汇总」')).toBeInTheDocument();
    expect(screen.getByText('节点（2 个）：')).toBeInTheDocument();
    // 节点名既出现在节点清单，也出现在连线的端点标签里，各算一次。
    expect(screen.getAllByText(/抓取 issue/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/写周报/).length).toBeGreaterThanOrEqual(1);

    // 节点 n1 是观察到的；节点 n2 与连线 n1→n2 是推断的。
    expect(screen.getAllByText('观察到的')).toHaveLength(1);
    expect(screen.getAllByText('推断的')).toHaveLength(2);

    // 待配置项与复核降级记录必须可见——它们决定这份草案能不能直接用。
    expect(screen.getByText('待配置：写周报 的 harness')).toBeInTheDocument();
    expect(screen.getByText(/写周报：材料里没有对应记录，已按「推断的」处理/)).toBeInTheDocument();
  });

  it('点「采用为流程草稿」调 adopt（as_template=false），成功后显示已采用落点', async () => {
    api.capture.adoptDraft.mockImplementation(async () => {
      const adopted = { ...PENDING_DRAFT, status: 'adopted', adopted_ref: 'wf-9' };
      detail = { ...detail, drafts: [adopted] };
      return adopted;
    });
    api.workflows.get.mockResolvedValue({ workflow_id: 'wf-9' } as WorkflowDefinition);

    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole('button', { name: '采用为流程草稿' }));

    expect(api.capture.adoptDraft).toHaveBeenCalledWith('d-1', { as_template: false });
    expect(await screen.findByText(/已采用，存为流程草稿/)).toBeInTheDocument();
  });
});
