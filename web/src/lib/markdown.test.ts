/**
 * 助手消息的 markdown 渲染 + 消毒。模型输出是半可信内容，这里的断言是 XSS 防线：
 * 剥掉的东西必须真的被剥掉，正常排版必须真的渲染出来。
 */

import { describe, expect, it } from 'vitest';
import { renderMarkdown } from './markdown';

describe('renderMarkdown', () => {
  it('渲染常规 markdown：加粗、列表、代码块', () => {
    const html = renderMarkdown('**加粗**\n\n- 甲\n- 乙\n\n```js\nconst a = 1;\n```');
    expect(html).toContain('<strong>加粗</strong>');
    expect(html).toContain('<li>甲</li>');
    expect(html).toContain('<li>乙</li>');
    expect(html).toContain('<code');
    expect(html).toContain('const a = 1;');
  });

  it('单个换行渲染成 <br>（聊天场景的直觉，breaks: true）', () => {
    expect(renderMarkdown('第一行\n第二行')).toContain('<br');
  });

  it('剥掉 <script>，保留正文', () => {
    const html = renderMarkdown('正文<script>alert("xss")</script>');
    expect(html.toLowerCase()).not.toContain('<script');
    expect(html).toContain('正文');
  });

  it('剥掉事件属性（onerror 等），保留元素本身', () => {
    const html = renderMarkdown('<img src="x.png" onerror="alert(1)">');
    expect(html).toContain('<img');
    expect(html.toLowerCase()).not.toContain('onerror');
  });

  it('剥掉 javascript: 链接，链接文字还在', () => {
    const html = renderMarkdown('[点我](javascript:alert(1))');
    expect(html.toLowerCase()).not.toContain('javascript:');
    expect(html).toContain('点我');
  });
});
