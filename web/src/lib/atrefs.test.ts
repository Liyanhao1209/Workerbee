/** `@路径` 引用解析的单元测试。 */

import { describe, expect, it } from 'vitest';

import { extractAtRefs, stripAtRefs } from './atrefs';

describe('extractAtRefs', () => {
  it('提取 @路径 token', () => {
    expect(extractAtRefs('看看 @src/main.ts 和 @docs/a.md')).toEqual([
      'src/main.ts',
      'docs/a.md',
    ]);
  });

  it('单独的 @ 不是引用', () => {
    expect(extractAtRefs('@ 你好')).toEqual([]);
  });

  it('去重且保序', () => {
    expect(extractAtRefs('@a.txt 然后 @b.txt 再 @a.txt')).toEqual(['a.txt', 'b.txt']);
  });

  it('没有引用时返回空', () => {
    expect(extractAtRefs('普通消息')).toEqual([]);
  });

  it('换行也算分隔', () => {
    expect(extractAtRefs('@a.txt\n@b.txt')).toEqual(['a.txt', 'b.txt']);
  });
});

describe('stripAtRefs', () => {
  it('剥掉引用 token，保留正文', () => {
    expect(stripAtRefs('看看 @src/main.ts 这个文件')).toBe('看看 这个文件');
  });

  it('全是引用时返回空串', () => {
    expect(stripAtRefs('@a.txt @b.txt')).toBe('');
  });
});
