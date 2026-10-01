import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from './components/AppShell';
import { TokenGate } from './components/TokenGate';
import { WorkflowListPage } from './pages/WorkflowListPage';
import { WorkflowEditorPage } from './pages/WorkflowEditorPage';
import { TaskListPage } from './pages/TaskListPage';
import { TaskDetailPage } from './pages/TaskDetailPage';
import { ExecutionGraphPage } from './pages/ExecutionGraphPage';
import { RegistryPage } from './pages/RegistryPage';
import { CapturesPage } from './pages/CapturesPage';
import { CaptureDetailPage } from './pages/CaptureDetailPage';
import { SessionsPage } from './pages/SessionsPage';
import { TemplatesPage } from './pages/TemplatesPage';
import { TemplateGraphEditorPage } from './pages/TemplateGraphEditorPage';
import { StoragePage } from './pages/StoragePage';
import { NotFoundPage } from './pages/NotFoundPage';

export function App(): JSX.Element {
  return (
    <TokenGate>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/workflows" replace />} />
          <Route path="/workflows" element={<WorkflowListPage />} />
          <Route path="/workflows/:workflowId" element={<WorkflowEditorPage />} />
          {/* 流程捕获：用一个模型真实跑一遍任务，把执行过程整理成流程草案。 */}
          <Route path="/captures" element={<CapturesPage />} />
          <Route path="/captures/:runId" element={<CaptureDetailPage />} />
          <Route path="/tasks" element={<TaskListPage />} />
          <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
          {/* 执行图是运维时最常看的视图，单独给它一级入口。 */}
          <Route path="/execution" element={<ExecutionGraphPage />} />
          <Route path="/execution/:taskId" element={<ExecutionGraphPage />} />
          {/* 会话台账：排障时回答「任务还连着哪个 session、它还活着吗」。 */}
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/registry" element={<RegistryPage />} />
          <Route path="/templates" element={<TemplatesPage />} />
          <Route path="/templates/new" element={<TemplateGraphEditorPage />} />
          <Route path="/storage" element={<StoragePage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </TokenGate>
  );
}
