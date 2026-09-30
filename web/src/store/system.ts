/**
 * 系统状态（OBS-01/05）。
 *
 * 两条不能含糊的事：
 * 1. **部署形态**（`session_hosting`）：`in_process` 意味着 harness 子进程由内核自己持有，
 *    内核一重启就会打断在途任务。用户必须知道，否则会默认「重启不丢任务」而实际会丢。
 * 2. **降级声明**（`startup_notes`）：supervisor 不可达、摘要器不可用这类事实要**如实呈现**，
 *    不折叠成一个小图标——用户有权知道当前跑在降级模式下。
 */

import { create } from 'zustand';
import type { SystemStatus } from '../api/types';
import { system } from '../api/endpoints';
import { ApiError } from '../api/client';
import { onKernelPush, onKernelReconnect } from './connection';

interface SystemStore {
  status: SystemStatus | null;
  error: ApiError | null;
  loaded: boolean;
  loading: boolean;
  refresh: () => Promise<void>;
}

export const useSystem = create<SystemStore>((set, get) => ({
  status: null,
  error: null,
  loaded: false,
  loading: false,

  refresh: async () => {
    if (get().loading) return;
    set({ loading: true });
    try {
      const status = await system.status();
      set({ status, error: null, loaded: true, loading: false });
    } catch (err) {
      set({
        error: err instanceof ApiError ? err : null,
        loaded: true,
        loading: false,
      });
    }
  },
}));

/** 部署形态的展示文案与警示级别。 */
export function hostingDescription(status: SystemStatus | null): {
  label: string;
  detail: string;
  variant: 'ok' | 'warn';
} {
  if (!status) {
    return { label: '托管方式未知', detail: '尚未读取到后台服务状态。', variant: 'warn' };
  }
  if (status.session_hosting === 'supervisor') {
    return {
      label: '会话托管：独立进程',
      detail: status.supervisor_connected
        ? '任务会话由独立进程托管。重启后台服务不会打断正在运行的任务。'
        : '连不上托管会话的独立进程，无法确认正在运行的会话是否还活着。请检查该进程是否已启动（workerbee-supervisor）。',
      variant: status.supervisor_connected ? 'ok' : 'warn',
    };
  }
  return {
    label: '会话托管：后台服务内部',
    detail:
      '任务会话由后台服务自己持有。重启后台服务会打断正在运行的任务，重启后需要核对状态，部分工作可能要重跑。如需要重启不丢任务，请改用独立进程托管。',
    variant: 'warn',
  };
}

let wired = false;

export function wireSystem(): void {
  if (wired) return;
  wired = true;
  // 推送到达通常意味着有状态变化，顺手刷新一次系统计数与降级声明。
  onKernelPush(() => {
    void useSystem.getState().refresh();
  });
  // 重连后同样重拉：降级声明与部署形态可能在断连期间变了（例如 supervisor 掉了）。
  onKernelReconnect(() => {
    void useSystem.getState().refresh();
  });
  void useSystem.getState().refresh();
}
