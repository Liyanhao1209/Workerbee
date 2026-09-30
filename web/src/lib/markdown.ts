/**
 * 助手消息的 markdown 渲染：marked 解析 + dompurify 消毒。
 *
 * 只用于**助手**消息气泡（模型输出是半可信内容，必须消毒）；
 * 用户消息保持纯文本渲染，不经过这里。
 *
 * `breaks: true`：聊天场景里单个换行就该换行，与用户对「打了一大段话」
 * 的直觉一致。GFM（表格、删除线、任务列表）marked 默认开启。
 */

import DOMPurify from 'dompurify';
import { marked } from 'marked';

marked.use({ breaks: true, gfm: true });

/** 渲染并消毒 markdown，返回可安全注入的 HTML 字符串。 */
export function renderMarkdown(text: string): string {
  const html = marked.parse(text, { async: false });
  return DOMPurify.sanitize(html);
}
