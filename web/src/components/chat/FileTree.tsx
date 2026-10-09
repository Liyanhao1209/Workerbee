/**
 * 工作区文件树（v0.03 §5.4）：目录懒加载（点开会才请求下一层）、
 * 隐藏文件默认折叠、敏感文件如实标注但不可打开。
 *
 * 点击文件 = 把 `@路径` 引用插入聊天输入框（经 onPick 回调）。
 * 树的每一次展开都重新请求 REST——推送不驱动这棵树，磁盘才是事实源。
 */

import { useEffect, useState } from 'react';
import { ApiError } from '../../api/client';
import { fs as fsApi } from '../../api/endpoints';
import type { FsEntry } from '../../api/types';

interface FileTreeProps {
  /** 当前工作区 id；空串表示交给服务端解析（当前/默认工作区）。 */
  workspaceId?: string;
  /** 点击文件时回调（通常是把 @路径 插进输入框）。 */
  onPick?: (path: string) => void;
}

export function FileTree({ workspaceId, onPick }: FileTreeProps): JSX.Element {
  const [reloadKey, setReloadKey] = useState(0);
  const [showHidden, setShowHidden] = useState(false);
  const [rootError, setRootError] = useState<string | null>(null);

  return (
    <div className="filetree">
      <div className="filetree__head">
        <strong>工作区文件</strong>
        <span className="spacer" />
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          title={showHidden ? '隐藏点开头的文件' : '显示点开头的文件'}
          onClick={() => setShowHidden((v) => !v)}
        >
          {showHidden ? '隐藏 .*' : '显示 .*'}
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          title="重新读取目录"
          onClick={() => setReloadKey((k) => k + 1)}
        >
          刷新
        </button>
      </div>
      <div className="filetree__body">
        {rootError ? (
          <div className="filetree__error">{rootError}</div>
        ) : (
          <TreeLevel
            key={reloadKey}
            path=""
            depth={0}
            workspaceId={workspaceId}
            showHidden={showHidden}
            onPick={onPick}
            onError={setRootError}
          />
        )}
      </div>
    </div>
  );
}

function TreeLevel({
  path,
  depth,
  workspaceId,
  showHidden,
  onPick,
  onError,
}: {
  path: string;
  depth: number;
  workspaceId?: string;
  showHidden: boolean;
  onPick?: (path: string) => void;
  onError?: (detail: string) => void;
}): JSX.Element {
  const [entries, setEntries] = useState<FsEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 挂载即加载本层（懒加载由父级的「展开才挂载」保证）。
  useEffect(() => {
    let cancelled = false;
    fsApi
      .list(path, workspaceId)
      .then((data) => {
        if (!cancelled) setEntries(data.entries ?? []);
      })
      .catch((err) => {
        const detail = err instanceof ApiError ? err.detail : '读取目录失败。';
        if (cancelled) return;
        setError(detail);
        // 根层失败上浮给整个面板展示；深层失败就地显示。
        if (path === '' && onError) onError(detail);
      });
    return () => {
      cancelled = true;
    };
  }, [path, workspaceId, onError]);

  if (error !== null && path !== '') {
    return <div className="filetree__error" style={{ paddingLeft: depth * 14 }}>{error}</div>;
  }
  if (entries === null) {
    return (
      <div className="filetree__loading" style={{ paddingLeft: depth * 14 }}>
        读取中…
      </div>
    );
  }
  const visible = showHidden ? entries : entries.filter((e) => !e.hidden);
  if (visible.length === 0) {
    return (
      <div className="filetree__empty" style={{ paddingLeft: depth * 14 }}>
        （空目录）
      </div>
    );
  }
  return (
    <>
      {visible.map((entry) => (
        <TreeNode
          key={entry.path}
          entry={entry}
          depth={depth}
          workspaceId={workspaceId}
          showHidden={showHidden}
          onPick={onPick}
        />
      ))}
    </>
  );
}

function TreeNode({
  entry,
  depth,
  workspaceId,
  showHidden,
  onPick,
}: {
  entry: FsEntry;
  depth: number;
  workspaceId?: string;
  showHidden: boolean;
  onPick?: (path: string) => void;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const indent = { paddingLeft: depth * 14 };

  if (entry.type === 'dir') {
    return (
      <>
        <button
          type="button"
          className="filetree__row filetree__row--dir"
          style={indent}
          onClick={() => setOpen((v) => !v)}
          title={entry.path}
        >
          <span className="filetree__icon">{open ? '▾' : '▸'}</span>
          <span className="filetree__name">{entry.name}/</span>
        </button>
        {open ? (
          <TreeLevel
            path={entry.path}
            depth={depth + 1}
            workspaceId={workspaceId}
            showHidden={showHidden}
            onPick={onPick}
          />
        ) : null}
      </>
    );
  }
  return (
    <button
      type="button"
      className="filetree__row"
      style={indent}
      title={
        entry.sensitive
          ? `${entry.path}（敏感文件，不允许读取或引用）`
          : `${entry.path}（点击插入 @引用）`
      }
      disabled={entry.sensitive}
      onClick={() => onPick?.(entry.path)}
    >
      <span className="filetree__icon">{entry.sensitive ? '🔒' : '·'}</span>
      <span className="filetree__name">{entry.name}</span>
      {entry.sensitive ? <span className="filetree__badge">敏感</span> : null}
    </button>
  );
}
