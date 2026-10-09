/**
 * 聊天输入里的 `@路径` 引用解析（v0.03 §5）。
 *
 * 规则故意简单：以空白分词，形如 `@some/path` 的 token 是一个引用
 * （单独的 `@` 不是）。返回去重后的引用清单（保持出现顺序）。
 * 路径里想带空格时不支持——引用是快捷方式，正经需求走文件树挑选。
 */

/** 从输入文本中提取 `@路径` 引用清单（去重、保序）。 */
export function extractAtRefs(text: string): string[] {
  const refs: string[] = [];
  for (const token of text.split(/\s+/)) {
    if (!token.startsWith('@') || token.length <= 1) continue;
    const path = token.slice(1);
    if (!refs.includes(path)) refs.push(path);
  }
  return refs;
}

/** 从输入文本里剥掉 `@路径` token，返回剩下的正文（多余空白折叠）。 */
export function stripAtRefs(text: string): string {
  return text
    .split(/\s+/)
    .filter((token) => !(token.startsWith('@') && token.length > 1))
    .join(' ')
    .trim();
}
