/**
 * 「需处理」入口（OBS-05）。
 *
 * 状态：这是顶部常驻徽标的数据源，必须**在断连后仍能找回**（AC-12）——
 * 因此订阅推送之外，每次 WebSocket 重连成功都会主动拉一次 REST。
 */

import { create } from 'zustand';
import type { Approval, Task, TaskStage } from '../api/types';
import { system } from '../api/endpoints';
import { ApiError } from '../api/client';
import { onKernelPush, onKernelReconnect } from './connection';

interface AttentionStore {
  approvals: Approval[];
  failedTasks: Task[];
  lostStages: TaskStage[];
  unresolvedResources: Record<string, unknown>[];
  startupNotes: string[];
  loaded: boolean;
  error: ApiError | null;
  loading: boolean;
  refresh: () => Promise<void>;
}

export const useAttention = create<AttentionStore>((set, get) => ({
  approvals: [],
  failedTasks: [],
  lostStages: [],
  unresolvedResources: [],
  startupNotes: [],
  loaded: false,
  error: null,
  loading: false,

  refresh: async () => {
    if (get().loading) return;
    set({ loading: true });
    try {
      const data = await system.attention();
      set({
        approvals: data.approvals ?? [],
        failedTasks: data.failed_tasks ?? [],
        lostStages: data.lost_stages ?? [],
        unresolvedResources: data.unresolved_resources ?? [],
        startupNotes: data.startup_notes ?? [],
        loaded: true,
        error: null,
        loading: false,
      });
    } catch (err) {
      set({ error: err instanceof ApiError ? err : null, loading: false, loaded: true });
    }
  },
}));

/** 需要用户动作的项：待处理与回注失败的审批。 */
export function actionableApprovals(approvals: Approval[]): Approval[] {
  return approvals.filter((a) => a.status === 'pending' || a.status === 'undeliverable');
}

export function attentionCount(state: {
  approvals: Approval[];
  failedTasks: Task[];
  lostStages: TaskStage[];
  unresolvedResources: unknown[];
}): number {
  return (
    actionableApprovals(state.approvals).length +
    state.failedTasks.length +
    state.lostStages.length +
    state.unresolvedResources.length
  );
}

let wired = false;

/** 订阅推送：审批出现时刷新徽标；重连成功后重新对账（AC-12）。 */
export function wireAttention(): void {
  if (wired) return;
  wired = true;
  onKernelPush((push) => {
    if (push.kind === 'attention') void useAttention.getState().refresh();
  });
  // 断连期间出现的审批不会有推送补给我们（推送是尽力而为的），重连后必须重拉一次。
  onKernelReconnect(() => {
    void useAttention.getState().refresh();
  });
  void useAttention.getState().refresh();
}
