/**
 * 工作区文件树的关键交互：
 * - 目录懒加载：展开才请求下一层；
 * - 隐藏文件默认折叠，切换后可见；
 * - 敏感文件如实标注且不可点击；
 * - 点击文件经 onPick 回调把路径交出去（插入 @引用）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError } from '../../api/client';
import type { FsEntry } from '../../api/types';

const api = vi.hoisted(() => ({
  fs: { list: vi.fn() },
}));

vi.mock('../../api/endpoints', () => api);

import { FileTree } from './FileTree';

function entry(partial: Partial<FsEntry> & { name: string; path: string }): FsEntry {
  return {
    type: 'file',
    size: 1,
    mtime: '2026-10-01T00:00:00Z',
    hidden: false,
    sensitive: false,
    ...partial,
  };
}

const ROOT_ENTRIES = [
  entry({ name: 'src', path: 'src', type: 'dir', size: null }),
  entry({ name: '.env', path: '.env', hidden: true, sensitive: true }),
  entry({ name: 'readme.md', path: 'readme.md' }),
];

beforeEach(() => {
  vi.clearAllMocks();
  api.fs.list.mockImplementation((path: string) => {
    if (path === '') return Promise.resolve({ path: '', entries: ROOT_ENTRIES, returned: 3 });
    if (path === 'src') {
      return Promise.resolve({
        path: 'src',
        entries: [entry({ name: 'main.ts', path: 'src/main.ts' })],
        returned: 1,
      });
    }
    return Promise.reject(new ApiError({ kind: 'http', status: 404, detail: '目录不存在', hint: null }));
  });
});

describe('FileTree', () => {
  it('挂载时只请求根层；目录展开才请求下一层', async () => {
    render(<FileTree />);

    // 根层条目出现（隐藏文件默认不显示）
    expect(await screen.findByText('readme.md')).toBeTruthy();
    expect(screen.queryByText('.env')).toBeNull();
    expect(api.fs.list).toHaveBeenCalledTimes(1);
    expect(api.fs.list).toHaveBeenCalledWith('', undefined);

    // 展开 src 目录 → 请求下一层
    await userEvent.click(screen.getByText('src/'));
    expect(await screen.findByText('main.ts')).toBeTruthy();
    expect(api.fs.list).toHaveBeenCalledWith('src', undefined);
  });

  it('切换「显示 .*」后隐藏文件可见，敏感文件不可点击', async () => {
    const onPick = vi.fn();
    render(<FileTree onPick={onPick} />);
    await screen.findByText('readme.md');

    await userEvent.click(screen.getByText('显示 .*'));
    const sensitive = await screen.findByText('.env');
    expect(sensitive).toBeTruthy();
    expect(screen.getByText('敏感')).toBeTruthy();

    // 敏感文件按钮禁用：点击不会触发 onPick
    await userEvent.click(sensitive.closest('button')!);
    expect(onPick).not.toHaveBeenCalled();
  });

  it('点击普通文件经 onPick 交出路径', async () => {
    const onPick = vi.fn();
    render(<FileTree onPick={onPick} />);

    await userEvent.click(await screen.findByText('readme.md'));
    expect(onPick).toHaveBeenCalledWith('readme.md');
  });

  it('根层读取失败时显示错误', async () => {
    api.fs.list.mockRejectedValue(
      new ApiError({ kind: 'http', status: 403, detail: '路径越出了当前工作区的边界', hint: null }),
    );
    render(<FileTree />);
    expect(await screen.findByText('路径越出了当前工作区的边界')).toBeTruthy();
  });
});
