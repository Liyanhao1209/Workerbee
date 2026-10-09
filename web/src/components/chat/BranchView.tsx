/**
 * 分支视图（v0.03 §6.3-6.4）：会话的完整分支森林，全屏覆盖层。
 *
 * - 节点 = 消息摘要卡（角色着色、软删节点虚化），边 = 父子关系；
 * - 布局走 `graph/layoutForest.ts`（深度分层 + 子树宽度分配），
 *   与线性视图共用同一 REST 事实源（store 里的 tree）；
 * - 破坏性操作只出现在这里且全部带确认（原则 4）：删除子树、清空已删除；
 * - 合并 = 点选：选中子树根 → 「移动到…」→ 点选目标节点 → 确认；
 *   非法目标（自己/自己的后代）在选择阶段禁用并标红；
 * - 撤销按钮常驻工具条（撤销窗口 = 清空前）；
 * - 双击节点把线性视图切到以它为末端的分支并关闭分支视图。
 */

import { useCallback, useMemo, useState } from 'react';
import {
  Background,
  BackgroundVariant,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  type Edge as RFEdge,
  type Node as RFNode,
  type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import type { ChatNode } from '../../api/types';
import { layoutForest } from '../../graph/layoutForest';
import { useChat } from '../../store/chat';
import { Banner, Modal } from '../common';

const NODE_W = 220;

type BranchNodeData = {
  node: ChatNode;
  /** 移动模式下不可作为目标的节点（自己或自己的后代）。 */
  invalidTarget: boolean;
  /** 移动模式下的合法目标高亮。 */
  moveTargetable: boolean;
};

type BranchNode = RFNode<BranchNodeData, 'chatBranch'>;

const ROLE_LABELS: Record<string, string> = {
  user: '我',
  assistant: '助手',
  tool: '工具',
  system: '系统',
};

function snippet(node: ChatNode): string {
  const text = node.role === 'tool' ? `${node.tool_name ?? '工具'}：${node.content}` : node.content;
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > 60 ? `${flat.slice(0, 60)}…` : flat || '（空消息）';
}

function BranchNodeCard({ data, selected }: NodeProps<BranchNode>): JSX.Element {
  const { node } = data;
  const deleted = node.deleted_at !== null;
  const borderColor = data.invalidTarget
    ? 'var(--st-danger)'
    : selected
      ? 'var(--accent)'
      : node.role === 'user'
        ? 'var(--accent-dim)'
        : 'var(--line-strong)';
  return (
    <div
      style={{
        width: NODE_W,
        background: 'var(--bg-2)',
        border: `${selected || data.invalidTarget ? 2 : 1}px ${deleted || node.role === 'tool' ? 'dashed' : 'solid'} ${borderColor}`,
        borderRadius: 'var(--radius)',
        padding: '6px 8px',
        opacity: deleted ? 0.45 : 1,
        boxShadow: selected ? '0 0 0 3px rgba(77,163,255,0.20)' : undefined,
        cursor: data.invalidTarget ? 'not-allowed' : 'pointer',
      }}
      title={data.invalidTarget ? '不能移动到它自己或它的后代下面' : undefined}
    >
      <Handle type="target" position={Position.Left} style={{ background: 'var(--line-strong)' }} />
      <div className="row row--tight" style={{ gap: 4 }}>
        <span
          className="text-xs"
          style={{
            fontWeight: 600,
            color:
              node.role === 'user'
                ? 'var(--accent)'
                : node.role === 'assistant'
                  ? 'var(--fg-0)'
                  : 'var(--fg-2)',
          }}
        >
          {ROLE_LABELS[node.role] ?? node.role}
        </span>
        {deleted ? <span className="chip chip--off">已删除</span> : null}
        {data.moveTargetable ? <span className="chip">可移到这里</span> : null}
      </div>
      <div
        className="text-xs"
        style={{
          marginTop: 3,
          color: 'var(--fg-1)',
          overflow: 'hidden',
          display: '-webkit-box',
          WebkitLineClamp: 2,
          WebkitBoxOrient: 'vertical',
        }}
      >
        {snippet(node)}
      </div>
      <Handle type="source" position={Position.Right} style={{ background: 'var(--line-strong)' }} />
    </div>
  );
}

const nodeTypes = { chatBranch: BranchNodeCard };

/** 子树成员集合（含根自身）：移动模式下这些节点不能作为目标。 */
function subtreeOf(tree: ChatNode[], rootId: string): Set<string> {
  const children = new Map<string, string[]>();
  for (const n of tree) {
    if (n.parent_id) {
      const list = children.get(n.parent_id) ?? [];
      list.push(n.node_id);
      children.set(n.parent_id, list);
    }
  }
  const out = new Set<string>([rootId]);
  const stack = [rootId];
  while (stack.length > 0) {
    const id = stack.pop()!;
    for (const kid of children.get(id) ?? []) {
      if (!out.has(kid)) {
        out.add(kid);
        stack.push(kid);
      }
    }
  }
  return out;
}

export function BranchView({ onClose }: { onClose: () => void }): JSX.Element {
  return (
    <ReactFlowProvider>
      <BranchViewInner onClose={onClose} />
    </ReactFlowProvider>
  );
}

function BranchViewInner({ onClose }: { onClose: () => void }): JSX.Element {
  const tree = useChat((s) => s.tree);
  const undo = useChat((s) => s.undo);
  const treeError = useChat((s) => s.treeError);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [moveSourceId, setMoveSourceId] = useState<string | null>(null);
  const [moveTarget, setMoveTarget] = useState<ChatNode | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<ChatNode | null>(null);
  const [confirmPurge, setConfirmPurge] = useState(false);

  const selected = tree.find((n) => n.node_id === selectedId) ?? null;
  const deletedCount = useMemo(() => tree.filter((n) => n.deleted_at !== null).length, [tree]);
  const invalidTargets = useMemo(
    () => (moveSourceId ? subtreeOf(tree, moveSourceId) : new Set<string>()),
    [tree, moveSourceId],
  );

  const positions = useMemo(
    () =>
      layoutForest(tree.map((n) => ({ id: n.node_id, parentId: n.parent_id }))),
    [tree],
  );

  const rfNodes: BranchNode[] = useMemo(
    () =>
      tree.map((node) => ({
        id: node.node_id,
        type: 'chatBranch' as const,
        position: positions.get(node.node_id) ?? { x: 0, y: 0 },
        selected: node.node_id === selectedId,
        draggable: false,
        data: {
          node,
          invalidTarget: moveSourceId !== null && invalidTargets.has(node.node_id),
          moveTargetable:
            moveSourceId !== null &&
            !invalidTargets.has(node.node_id) &&
            node.deleted_at === null,
        },
      })),
    [tree, positions, selectedId, moveSourceId, invalidTargets],
  );

  const rfEdges: RFEdge[] = useMemo(() => {
    const ids = new Set(tree.map((n) => n.node_id));
    return tree
      .filter((n) => n.parent_id !== null && ids.has(n.parent_id))
      .map((n) => ({
        id: `${n.parent_id}->${n.node_id}`,
        source: n.parent_id as string,
        target: n.node_id,
        type: 'smoothstep',
        style: {
          stroke: 'var(--line-strong)',
          strokeWidth: 1.4,
          strokeDasharray: n.deleted_at !== null ? '4 3' : undefined,
        },
        markerEnd: { type: MarkerType.ArrowClosed, color: 'var(--line-strong)', width: 14, height: 14 },
      }));
  }, [tree]);

  const onNodeClick = useCallback(
    (_e: unknown, node: BranchNode) => {
      if (moveSourceId) {
        // 点选合并的目标选择：非法目标（自己/后代/已删除）直接拒绝在选择阶段。
        if (invalidTargets.has(node.id) || node.data.node.deleted_at !== null) return;
        setMoveTarget(node.data.node);
        return;
      }
      setSelectedId(node.id);
    },
    [moveSourceId, invalidTargets],
  );

  const onKeyDown = (e: React.KeyboardEvent): void => {
    if (e.key === 'Escape') {
      if (moveSourceId) setMoveSourceId(null);
      else onClose();
      return;
    }
    if ((e.key === 'Delete' || e.key === 'Backspace') && selected && !selected.deleted_at) {
      setConfirmDelete(selected);
    }
  };

  const run = (action: Promise<boolean>, close: () => void): void => {
    void action.then((ok) => {
      if (ok) close();
    });
  };

  return (
    // 全屏覆盖层：分支视图与文件树分据两处，不同时展开（§6.4 原则 5）。
    <div
      className="branchview"
      role="dialog"
      aria-label="分支视图"
      tabIndex={-1}
      onKeyDown={onKeyDown}
      ref={(el) => el?.focus()}
    >
      <div className="branchview__head">
        <strong>分支视图</strong>
        <span className="chat-meta">双击节点切到该分支；删除/移动只在这里发生，都可撤销</span>
        <span className="spacer" />
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          disabled={!undo}
          title={undo ? '撤销最近一次删除或移动' : '没有可撤销的操作'}
          onClick={() => void useChat.getState().undoLast()}
        >
          撤销
        </button>
        {moveSourceId ? (
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => setMoveSourceId(null)}
          >
            取消移动
          </button>
        ) : (
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            disabled={!selected || selected.deleted_at !== null}
            title={selected ? '把这棵子树移到别的消息下面' : '先选中一个节点'}
            onClick={() => selected && setMoveSourceId(selected.node_id)}
          >
            移动到…
          </button>
        )}
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          disabled={!selected || selected.deleted_at !== null}
          onClick={() => selected && setConfirmDelete(selected)}
        >
          删除子树
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          disabled={!selected || selected.deleted_at === null}
          onClick={() => selected && run(useChat.getState().restoreSubtree(selected.node_id), () => setSelectedId(null))}
        >
          恢复
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          disabled={deletedCount === 0}
          title="永久删除全部已删除的消息，不可恢复"
          onClick={() => setConfirmPurge(true)}
        >
          清空已删除{deletedCount > 0 ? `（${deletedCount}）` : ''}
        </button>
        <button type="button" className="btn btn--sm" onClick={onClose}>
          关闭
        </button>
      </div>

      {treeError ? (
        <Banner variant="danger" title={treeError.detail} hint={treeError.hint ?? undefined} />
      ) : null}
      {moveSourceId ? (
        <Banner
          variant="info"
          title="移动模式：点选一个新父节点"
          hint="红色节点（它自己或它的后代）不能选；Esc 或「取消移动」退出。"
        />
      ) : null}

      <div className="branchview__canvas">
        {tree.length === 0 ? (
          <div className="chatpage__sessions-empty">还没有消息</div>
        ) : (
          <ReactFlow
            nodes={rfNodes}
            edges={rfEdges}
            nodeTypes={nodeTypes}
            onNodeClick={onNodeClick}
            onPaneClick={() => setSelectedId(null)}
            onNodeDoubleClick={(_e, node) => {
              if (moveSourceId) return; // 移动模式下双击不跳分支
              void useChat.getState().openBranchAt(node.id);
              onClose();
            }}
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable
            deleteKeyCode={null}
            proOptions={{ hideAttribution: false }}
            fitView
            minZoom={0.15}
            maxZoom={2}
            style={{ background: 'var(--bg-0)' }}
          >
            <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="#1f2630" />
            <MiniMap
              pannable
              zoomable
              style={{ background: 'var(--bg-1)', border: '1px solid var(--line)', width: 132, height: 88 }}
              maskColor="rgba(6,8,12,0.7)"
              nodeColor={(n) => {
                const data = n.data as BranchNodeData | undefined;
                if (data?.node.deleted_at) return '#39414e';
                return data?.node.role === 'user' ? '#2f6ea8' : '#4a5568';
              }}
            />
          </ReactFlow>
        )}
      </div>

      {confirmDelete ? (
        <Modal
          title="删除子树"
          onClose={() => setConfirmDelete(null)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setConfirmDelete(null)}>
                取消
              </button>
              <button
                type="button"
                className="btn btn--primary"
                onClick={() =>
                  run(useChat.getState().deleteSubtree(confirmDelete.node_id), () => {
                    setConfirmDelete(null);
                    if (selectedId && subtreeOf(tree, confirmDelete.node_id).has(selectedId)) {
                      setSelectedId(null);
                    }
                  })
                }
              >
                确认删除
              </button>
            </>
          }
        >
          <p style={{ margin: 0 }}>
            「{snippet(confirmDelete)}」及其后面的整棵分支会被标记为已删除，
            默认视图不再显示；清空之前可以用「撤销」或「恢复」找回。
          </p>
        </Modal>
      ) : null}

      {moveTarget && moveSourceId ? (
        <Modal
          title="移动子树"
          onClose={() => setMoveTarget(null)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setMoveTarget(null)}>
                取消
              </button>
              <button
                type="button"
                className="btn btn--primary"
                onClick={() =>
                  run(useChat.getState().moveSubtree(moveSourceId, moveTarget.node_id), () => {
                    setMoveTarget(null);
                    setMoveSourceId(null);
                  })
                }
              >
                确认移动
              </button>
            </>
          }
        >
          <p style={{ margin: 0 }}>
            把选中的子树移动到「{snippet(moveTarget)}」下面。移动后可以用「撤销」移回去。
          </p>
        </Modal>
      ) : null}

      {confirmPurge ? (
        <Modal
          title="清空已删除"
          onClose={() => setConfirmPurge(false)}
          footer={
            <>
              <button type="button" className="btn" onClick={() => setConfirmPurge(false)}>
                取消
              </button>
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => run(useChat.getState().purgeDeleted(), () => setConfirmPurge(false))}
              >
                永久删除
              </button>
            </>
          }
        >
          <p style={{ margin: 0 }}>
            全部 {deletedCount} 条已删除的消息会被永久移除，不可恢复。当前对话内容不受影响。
          </p>
        </Modal>
      ) : null}
    </div>
  );
}
