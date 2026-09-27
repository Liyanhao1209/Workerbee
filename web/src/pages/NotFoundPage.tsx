/**
 * 404。路由没命中时给出**可走的一级入口**，而不是一句「页面不存在」就完了。
 */

import { Link } from 'react-router-dom';
import { Empty } from '../components/common';

/** 与 AppShell 顶栏的一级入口保持一致（顶层路由清单的唯一展示处）。 */
const TOP_LEVEL_ROUTES: { to: string; label: string; hint: string }[] = [
  { to: '/workflows', label: '流程', hint: '定义与修订' },
  { to: '/tasks', label: '任务', hint: '提交与生命周期' },
  { to: '/execution', label: '执行图', hint: '实际执行的节点与连线' },
  { to: '/registry', label: '注册表', hint: 'harness / 凭据 / 技能 / 工具' },
  { to: '/templates', label: '模板', hint: '可复用的流程与节点' },
  { to: '/storage', label: '存储', hint: '数据目录与清理' },
];

export function NotFoundPage(): JSX.Element {
  return (
    <div className="page">
      <Empty
        title="页面不存在"
        hint={
          <>
            <div>地址写错了，或者它指向的资源已被删除。可用的顶层入口：</div>
            <div
              className="row row--tight"
              style={{ justifyContent: 'center', marginTop: 'var(--sp-3)' }}
            >
              {TOP_LEVEL_ROUTES.map((route) => (
                <Link key={route.to} className="navlink" to={route.to} title={route.hint}>
                  {route.label}
                </Link>
              ))}
            </div>
          </>
        }
        action={
          <Link className="btn btn--primary" to="/workflows">
            返回流程列表
          </Link>
        }
      />
    </div>
  );
}
