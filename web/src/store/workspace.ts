/**
 * 当前工作区（v0.03 §3）。
 *
 * 选择来源的优先级：
 * 1. 本地记忆（localStorage「workerbee.workspace」）——用户上次的选择，
 *    空字符串也是合法选择（「全部工作区」）；
 * 2. 服务端记录的「serve 启动时匹配/注册的工作区」（current_workspace_id）——
 *    首次打开、本地尚无记忆时用它定位；
 * 3. 默认工作区 'default'；
 * 4. 列表里的第一个；
 * 5. 都没有就是 ''（不过滤）。
 *
 * 失效的本地记忆（指向已被删除的工作区）不能留着——那会把列表过滤成一张
 * 看似「空的」表，而事实是「筛选指向了一个不存在的范围」。
 */

import { create } from 'zustand';
import type { Workspace } from '../api/types';
import { workspaces as workspaceApi } from '../api/endpoints';
import { ApiError } from '../api/client';
import { onKernelPush, onKernelReconnect } from './connection';

const STORAGE_KEY = 'workerbee.workspace';

/** '' 表示「全部工作区」（不过滤）。 */
export type WorkspaceFilter = string;

interface WorkspaceStore {
  workspaces: Workspace[];
  /** 服务端记录的当前工作区（serve 启动时写入）；没有记录为 null。 */
  serverCurrent: string | null;
  /** 生效的筛选：'' = 全部。 */
  currentId: WorkspaceFilter;
  error: ApiError | null;
  loaded: boolean;
  loading: boolean;
  refresh: () => Promise<void>;
  setCurrent: (id: WorkspaceFilter) => void;
}

/**
 * 决定生效的工作区筛选。纯函数，单独可测。
 *
 * `stored === null` 表示本地没有记忆；空字符串是用户显式选过「全部」，
 * 与「没有记忆」是两回事——前者要尊重，后者才回落到服务端记录。
 */
export function resolveCurrentWorkspaceId(
  stored: string | null,
  serverCurrent: string | null,
  workspaces: Workspace[],
): WorkspaceFilter {
  if (stored !== null) {
    if (stored === '') return '';
    if (workspaces.some((w) => w.workspace_id === stored)) return stored;
    // 记忆里的工作区已被删除：回落，不把失效筛选当事实
  }
  if (serverCurrent && workspaces.some((w) => w.workspace_id === serverCurrent)) {
    return serverCurrent;
  }
  if (workspaces.some((w) => w.workspace_id === 'default')) return 'default';
  return workspaces[0]?.workspace_id ?? '';
}

/** 按当前筛选过滤带 workspace_id 的列表项；'' 不过滤。归档不影响过滤（归档只是冻结发射）。 */
export function filterByWorkspace<T>(items: T[], currentId: WorkspaceFilter, pick: (item: T) => string): T[] {
  if (currentId === '') return items;
  return items.filter((item) => pick(item) === currentId);
}

function readStored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export const useWorkspace = create<WorkspaceStore>((set, get) => ({
  workspaces: [],
  serverCurrent: null,
  currentId: readStored() ?? '',
  error: null,
  loaded: false,
  loading: false,

  refresh: async () => {
    if (get().loading) return;
    set({ loading: true });
    try {
      const resp = await workspaceApi.list();
      const list = resp.workspaces;
      set({
        workspaces: list,
        serverCurrent: resp.current_workspace_id,
        currentId: resolveCurrentWorkspaceId(readStored(), resp.current_workspace_id, list),
        error: null,
        loaded: true,
        loading: false,
      });
    } catch (err) {
      set({
        error: err instanceof ApiError ? err : null,
        loaded: true,
        loading: false,
      });
    }
  },

  setCurrent: (id: WorkspaceFilter) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // localStorage 不可用（隐私模式等）：选择只在本次会话内生效
    }
    set({ currentId: id });
  },
}));

let wired = false;

export function wireWorkspace(): void {
  if (wired) return;
  wired = true;
  // 工作区增删/归档、流程迁移都会推送事件，顺手刷新一次列表。
  onKernelPush(() => {
    void useWorkspace.getState().refresh();
  });
  onKernelReconnect(() => {
    void useWorkspace.getState().refresh();
  });
  void useWorkspace.getState().refresh();
}
