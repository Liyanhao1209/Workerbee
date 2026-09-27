import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { HashRouter } from 'react-router-dom';
import { App } from './App';
import './styles/global.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('缺少 #root 挂载点');
}

/**
 * 用 HashRouter 而不是 BrowserRouter：内核把 dist 作为静态目录托管时
 * 不一定配了 SPA fallback（history 路由会 404），hash 路由在两种托管方式下都能用。
 * 深链接形如 `http://127.0.0.1:8787/#/tasks/<id>`。
 */
createRoot(container).render(
  <StrictMode>
    <HashRouter>
      <App />
    </HashRouter>
  </StrictMode>,
);
