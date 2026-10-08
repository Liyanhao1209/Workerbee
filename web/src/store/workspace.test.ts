/**
 * 工作区筛选的纯逻辑测试（v0.03 §3）。
 *
 * 关键的两条防线：
 * - 本地记忆的优先级高于服务端记录，但失效记忆（工作区已删）必须回落，
 *   不能把一个不存在的筛选当事实（那会把列表过滤成看似「空」的表）；
 * - ''（全部）是合法的用户选择，与「没有记忆」（null）是两回事。
 */

import { describe, expect, it } from 'vitest';
import type { Workspace } from '../api/types';
import { filterByWorkspace, resolveCurrentWorkspaceId } from './workspace';

function ws(id: string, archived = false): Workspace {
  return {
    workspace_id: id,
    name: `工作区 ${id}`,
    root_dir: `/data/${id}`,
    archived,
    created_at: '2026-01-01T00:00:00+00:00',
  };
}

const LIST = [ws('default'), ws('ws-a'), ws('ws-b', true)];

describe('resolveCurrentWorkspaceId', () => {
  it('本地记忆优先，且空字符串（全部）也是被尊重的选择', () => {
    expect(resolveCurrentWorkspaceId('ws-a', 'default', LIST)).toBe('ws-a');
    expect(resolveCurrentWorkspaceId('', 'default', LIST)).toBe('');
  });

  it('记忆里的工作区已被删除时回落到服务端记录', () => {
    expect(resolveCurrentWorkspaceId('gone', 'ws-a', LIST)).toBe('ws-a');
  });

  it('没有本地记忆时用服务端的当前工作区', () => {
    expect(resolveCurrentWorkspaceId(null, 'ws-a', LIST)).toBe('ws-a');
  });

  it('服务端记录失效时回落到 default，再不行就取第一个', () => {
    expect(resolveCurrentWorkspaceId(null, 'gone', LIST)).toBe('default');
    expect(resolveCurrentWorkspaceId(null, null, [ws('x')])).toBe('x');
    expect(resolveCurrentWorkspaceId(null, null, [])).toBe('');
  });

  it('归档的工作区仍是合法选择（归档只冻结发射，不是消失）', () => {
    expect(resolveCurrentWorkspaceId('ws-b', 'default', LIST)).toBe('ws-b');
  });
});

describe('filterByWorkspace', () => {
  const items = [
    { name: '甲', workspace_id: 'default' },
    { name: '乙', workspace_id: 'ws-a' },
    { name: '丙', workspace_id: 'ws-b' },
  ];

  it('空筛选不过滤', () => {
    expect(filterByWorkspace(items, '', (i) => i.workspace_id)).toHaveLength(3);
  });

  it('按工作区过滤', () => {
    const kept = filterByWorkspace(items, 'ws-a', (i) => i.workspace_id);
    expect(kept.map((i) => i.name)).toEqual(['乙']);
  });

  it('归档的工作区照样能过滤出它的条目', () => {
    const kept = filterByWorkspace(items, 'ws-b', (i) => i.workspace_id);
    expect(kept.map((i) => i.name)).toEqual(['丙']);
  });
});
