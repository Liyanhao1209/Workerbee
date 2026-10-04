// 每个测试文件都会先跑这里：把 toBeInTheDocument / toHaveTextContent 等
// jest-dom 匹配器挂到 vitest 的 expect 上，并做通用的 jsdom 补丁。
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// globals: false 时 testing-library 不会自动注册清理，这里手动挂上：
// 不清理的话上一个用例渲染的 DOM 会留在 document 里污染下一个用例。
afterEach(() => cleanup());

// jsdom 不实现 scrollTo；聊天面板自动滚动会触到。
// 给空实现即可——测试断言的是内容，不是滚动位置。
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = () => undefined;
}
