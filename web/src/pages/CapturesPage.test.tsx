/**
 * 捕获页（/captures）新建 run 的流程：
 * - 必填校验失败时不发请求，表单错误如实展示；
 * - 填齐后创建：createRun 收到的参数与表单一致（捕获是真实执行，参数不能错）；
 * - 创建成功后跳到详情页。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { CaptureRun, HarnessRegistration } from '../api/types';

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
  registry: {
    harnesses: vi.fn(),
    credentials: vi.fn(),
  },
  tasks: { list: vi.fn() },
}));

vi.mock('../api/endpoints', () => api);

import { CapturesPage } from './CapturesPage';

const HARNESS: HarnessRegistration = {
  harness_id: 'h-1',
  name: 'Claude',
  adapter_id: 'claude',
  adapter_version: null,
  exec_path: null,
  env_template: {},
  cwd: null,
  auth_binding: null,
  auth_mode: 'native_login',
  capabilities_snapshot: null,
  last_probe_at: null,
  last_probe_ok: null,
  last_probe_error: null,
  enabled: true,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

const CREATED_RUN: CaptureRun = {
  run_id: 'run-9',
  name: '抓取周报',
  origin: 'live',
  workflow_id: 'wf-1',
  task_id: 'task-9',
  profile: {},
  status: 'running',
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

function renderPage(): void {
  render(
    <MemoryRouter initialEntries={['/captures']}>
      <Routes>
        <Route path="/captures" element={<CapturesPage />} />
        <Route path="/captures/:runId" element={<div>捕获详情页</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.capture.listRuns.mockResolvedValue({ runs: [], returned: 0 });
  api.registry.harnesses.mockResolvedValue([HARNESS]);
  api.registry.credentials.mockResolvedValue([]);
});

describe('新建捕获任务', () => {
  it('必填项缺失时不发请求，错误如实展示', async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('还没有捕获任务');
    await user.click(screen.getAllByRole('button', { name: '新建捕获任务' })[0]!);
    await user.click(await screen.findByRole('button', { name: /新跑一个任务捕获/ }));

    await user.click(await screen.findByRole('button', { name: '创建并开始执行' }));

    expect(await screen.findByText('给这次捕获起个名字。')).toBeInTheDocument();
    expect(api.capture.createRun).not.toHaveBeenCalled();
  });

  it('填齐表单后创建：参数与表单一致，成功后跳到详情页', async () => {
    api.capture.createRun.mockResolvedValue(CREATED_RUN);

    const user = userEvent.setup();
    renderPage();

    await screen.findByText('还没有捕获任务');
    await user.click(screen.getAllByRole('button', { name: '新建捕获任务' })[0]!);
    await user.click(await screen.findByRole('button', { name: /新跑一个任务捕获/ }));

    await user.type(await screen.findByPlaceholderText('如：抓取并汇总本周的 issue'), '抓取周报');
    await user.type(
      screen.getByPlaceholderText('把这个任务交给一个模型去做，就像你平时交代给它一样写。'),
      '抓取本周 issue 并汇总',
    );
    // harness 列表是异步加载的，等选项出现再选。
    await screen.findByRole('option', { name: /Claude/ });
    await user.selectOptions(screen.getByLabelText(/^Harness/), 'h-1');

    await user.click(screen.getByRole('button', { name: '创建并开始执行' }));

    expect(api.capture.createRun).toHaveBeenCalledWith({
      name: '抓取周报',
      instructions: '抓取本周 issue 并汇总',
      harness_ref: 'h-1',
      model_name: null,
      credential_ref: null,
    });
    expect(await screen.findByText('捕获详情页')).toBeInTheDocument();
  });
});
