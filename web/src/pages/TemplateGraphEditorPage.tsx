/**
 * 手动创建流程模板（/templates/new）：复用流程编辑器的画布与节点面板画拓扑。
 *
 * 与流程编辑器的差别只在「存到哪」：这里编辑的图不落成流程修订，而是作为
 * 模板载荷保存（kind=workflow）。模板不带运行状态；凭据只保留引用，
 * 同机实例化时自动绑定。
 */

import { useCallback, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { GraphSpec, NodeDefinition } from '../api/types';
import { registry as registryApi, templates as templatesApi } from '../api/endpoints';
import { emptyGraph, newLocalId } from '../api/guards';
import { useAsync, useSubmit } from '../hooks/useAsync';
import { WorkflowCanvas } from '../graph/WorkflowCanvas';
import { NodeInspector } from '../components/NodeInspector';
import { Banner, Empty, Field } from '../components/common';

export function TemplateGraphEditorPage(): JSX.Element {
  const navigate = useNavigate();
  const harnesses = useAsync(useCallback(() => registryApi.harnesses(), []), []);
  const credentials = useAsync(useCallback(() => registryApi.credentials(), []), []);
  const skills = useAsync(useCallback(() => registryApi.skills(), []), []);
  const tools = useAsync(useCallback(() => registryApi.tools(), []), []);
  const save = useSubmit();

  const [draft, setDraft] = useState<GraphSpec>(emptyGraph());
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [layoutNonce, setLayoutNonce] = useState(0);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [formError, setFormError] = useState<string | null>(null);

  const selectedNode = draft.nodes.find((n) => n.node_id === selectedNodeId) ?? null;

  const registryError =
    harnesses.error?.detail ?? credentials.error?.detail ?? skills.error?.detail ?? tools.error?.detail ?? null;

  const addNode = (): void => {
    const id = newLocalId();
    const node: NodeDefinition = {
      node_id: id,
      name: '新节点',
      role: null,
      description: null,
      enabled: true,
      system_prompt: null,
      profiles: [],
      skill_refs: [],
      tool_refs: [],
      required_inputs: [],
      ui_position: null,
    };
    setDraft((prev) => ({ ...prev, nodes: [...prev.nodes, node] }));
    setSelectedNodeId(id);
  };

  const saveTemplate = async (): Promise<void> => {
    if (!name.trim()) {
      setFormError('名称必填。');
      return;
    }
    if (draft.nodes.length === 0) {
      setFormError('至少需要一个节点：先点「+ 添加节点」再连线。');
      return;
    }
    const names = draft.nodes.map((n) => n.name.trim());
    if (names.some((n) => !n)) {
      setFormError('有节点还没填名称：点选它，在右侧面板填写。');
      return;
    }
    if (new Set(names).size !== names.length) {
      setFormError('节点名称不能重复。');
      return;
    }
    setFormError(null);

    // 模板边按节点**名称**索引（实例化时节点会拿到新 id）。
    const nameOf = new Map(draft.nodes.map((n) => [n.node_id, n.name.trim()]));
    const credentialLabel = new Map((credentials.data ?? []).map((c) => [c.credential_id, c.label]));
    const slots = draft.nodes.flatMap((node) =>
      node.profiles.flatMap((profile, idx) =>
        profile.credential_ref
          ? [
              {
                slot: `${node.name.trim()}.profiles[${idx}].credential_ref`,
                original_label: credentialLabel.get(profile.credential_ref) ?? profile.credential_ref,
                original_kind: null,
              },
            ]
          : [],
      ),
    );

    const created = await save.run(() =>
      templatesApi.create({
        name: name.trim(),
        description: description.trim() || null,
        kind: 'workflow',
        payload: {
          nodes: draft.nodes.map((node) => ({
            name: node.name.trim(),
            role: node.role,
            description: node.description,
            system_prompt: node.system_prompt,
            profiles: node.profiles,
            skill_refs: node.skill_refs,
            tool_refs: node.tool_refs,
            required_inputs: node.required_inputs,
          })),
          edges: draft.edges
            .filter((e) => nameOf.has(e.from_node) && nameOf.has(e.to_node))
            .map((e) => ({
              from_node: nameOf.get(e.from_node) ?? '',
              to_node: nameOf.get(e.to_node) ?? '',
              output_contract: e.output_contract,
              desc: e.desc,
            })),
          sensitive_slots: slots,
        },
      }),
    );
    if (created) navigate('/templates');
  };

  return (
    <div className="page" style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div className="page-head">
        <div className="page-head__titles">
          <h1>手动创建流程模板</h1>
          <div className="page-head__sub">
            和新建流程用的是同一个画布：加节点、连线、点选节点在右侧配置。保存后得到的是模板，不会出现在流程列表里。
          </div>
        </div>
        <div className="page-head__actions">
          <button type="button" className="btn btn--sm" onClick={() => navigate('/templates')}>
            返回模板列表
          </button>
          <button
            type="button"
            className="btn btn--sm btn--primary"
            disabled={save.busy}
            onClick={() => void saveTemplate()}
          >
            {save.busy ? '保存中…' : '保存为模板'}
          </button>
        </div>
      </div>

      {formError ? (
        <Banner variant="danger" title="无法保存">
          {formError}
        </Banner>
      ) : null}
      {save.error ? (
        <Banner variant="danger" title={save.error.unreachable ? '无法连接后台服务' : '保存模板失败'}>
          <span className="mono text-xs">{save.error.detail}</span>
        </Banner>
      ) : null}

      <div className="row" style={{ gap: 'var(--sp-3)', marginBottom: 'var(--sp-3)' }}>
        <Field label="模板名称" required>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="标准三节点流水线" />
        </Field>
        <Field label="描述">
          <input
            className="input"
            style={{ minWidth: 320 }}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="这个模板适用于什么场景"
          />
        </Field>
        <button type="button" className="btn btn--sm" onClick={addNode}>
          + 添加节点
        </button>
        <button type="button" className="btn btn--sm" onClick={() => setLayoutNonce((n) => n + 1)}>
          自动布局
        </button>
      </div>

      <div
        style={{
          flex: '1 1 auto',
          minHeight: 420,
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1fr) 372px',
          gap: 'var(--sp-3)',
        }}
      >
        <div
          style={{
            border: '1px solid var(--line)',
            borderRadius: 'var(--radius-lg)',
            overflow: 'hidden',
            minHeight: 0,
            position: 'relative',
          }}
        >
          {draft.nodes.length === 0 ? (
            <div style={{ padding: 'var(--sp-5)' }}>
              <Empty
                title="从添加节点开始"
                hint="点上方「+ 添加节点」，拖动节点摆位置，从节点右侧拖出线连到下一个节点表达依赖。"
              />
            </div>
          ) : null}
          <WorkflowCanvas
            graph={draft}
            selectedNodeId={selectedNodeId}
            onSelectNode={setSelectedNodeId}
            onGraphChange={(next) => setDraft(next)}
            focus={null}
            layoutNonce={layoutNonce}
          />
        </div>

        <div className="panel" style={{ minHeight: 0, overflow: 'auto' }}>
          <div className="panel__head">节点配置</div>
          {selectedNode ? (
            <div style={{ padding: 'var(--sp-3)' }}>
              <NodeInspector
                node={selectedNode}
                onChange={(next) =>
                  setDraft((prev) => ({
                    ...prev,
                    nodes: prev.nodes.map((n) => (n.node_id === next.node_id ? next : n)),
                  }))
                }
                onDelete={() => {
                  const id = selectedNode.node_id;
                  setDraft((prev) => ({
                    ...prev,
                    nodes: prev.nodes.filter((n) => n.node_id !== id),
                    edges: prev.edges.filter((e) => e.from_node !== id && e.to_node !== id),
                  }));
                  setSelectedNodeId(null);
                }}
                harnesses={harnesses.data ?? []}
                credentials={credentials.data ?? []}
                skills={skills.data ?? []}
                tools={tools.data ?? []}
                onCredentialsChanged={credentials.reload}
                registryError={registryError}
              />
            </div>
          ) : (
            <div className="panel__hint">点选画布上的节点后在这里配置；候选、凭据、提示词都在这一栏。</div>
          )}
        </div>
      </div>
    </div>
  );
}
