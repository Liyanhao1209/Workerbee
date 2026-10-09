/**
 * layoutForest 的单元测试（v0.03 §6.3）：
 * 深度分层定 x、父节点居中于孩子区间、多根树纵向排布、
 * 不定分叉数、孤儿按树根兜底、确定性。
 */

import { describe, expect, it } from 'vitest';

import { FOREST_COL_W, FOREST_ROW_H, layoutForest, type ForestNodeLike } from './layoutForest';

function n(id: string, parentId: string | null = null): ForestNodeLike {
  return { id, parentId };
}

describe('layoutForest', () => {
  it('单链：逐层占列，纵向都在同一行', () => {
    const pos = layoutForest([n('a'), n('b', 'a'), n('c', 'b')]);
    expect(pos.get('a')).toEqual({ x: 0, y: 0 });
    expect(pos.get('b')).toEqual({ x: FOREST_COL_W, y: 0 });
    expect(pos.get('c')).toEqual({ x: 2 * FOREST_COL_W, y: 0 });
  });

  it('分叉：父节点居中于两个孩子之间', () => {
    const pos = layoutForest([n('r'), n('x', 'r'), n('y', 'r')]);
    expect(pos.get('x')!.y).toBe(0);
    expect(pos.get('y')!.y).toBe(FOREST_ROW_H);
    expect(pos.get('r')!.y).toBe(FOREST_ROW_H / 2);
  });

  it('不定分叉数：三个孩子时父节点与中间孩子同高', () => {
    const pos = layoutForest([n('r'), n('a', 'r'), n('b', 'r'), n('c', 'r')]);
    expect(pos.get('r')!.y).toBe(FOREST_ROW_H);
    expect(pos.get('b')!.y).toBe(FOREST_ROW_H);
  });

  it('深树 + 不对称分叉：x 按深度、兄弟子树不重叠', () => {
    // r ── a ── a1 ── a1x
    //  └── b ── b1
    //       └── b2
    const pos = layoutForest([
      n('r'), n('a', 'r'), n('a1', 'a'), n('a1x', 'a1'),
      n('b', 'r'), n('b1', 'b'), n('b2', 'b'),
    ]);
    expect(pos.get('a1x')!.x).toBe(3 * FOREST_COL_W);
    expect(pos.get('b1')!.x).toBe(2 * FOREST_COL_W);
    // a 的子树占 1 格（单叶），b 的子树占 2 格，依次排布不重叠。
    expect(pos.get('a')!.y).toBe(0);
    expect(pos.get('b1')!.y).toBe(FOREST_ROW_H);
    expect(pos.get('b2')!.y).toBe(2 * FOREST_ROW_H);
    expect(pos.get('b')!.y).toBe((FOREST_ROW_H + 2 * FOREST_ROW_H) / 2);
    // r 居中于整个孩子区间 [0, 2]。
    expect(pos.get('r')!.y).toBe(FOREST_ROW_H);
  });

  it('多根树纵向依次排布，树间空一格', () => {
    const pos = layoutForest([n('t1'), n('t1a', 't1'), n('t2')]);
    expect(pos.get('t1')!.y).toBe(0);
    expect(pos.get('t1a')!.y).toBe(0);
    expect(pos.get('t2')!.y).toBe(2 * FOREST_ROW_H);
    expect(pos.get('t2')!.x).toBe(0);
  });

  it('父指针缺失的孤儿按树根对待', () => {
    const pos = layoutForest([n('real'), n('orphan', 'ghost-parent')]);
    expect(pos.get('real')!.x).toBe(0);
    expect(pos.get('orphan')!.x).toBe(0);
    expect(pos.get('orphan')!.y).toBeGreaterThan(pos.get('real')!.y);
  });

  it('确定性：同样的输入两次布局结果一致，且与输入顺序无关于结构', () => {
    const nodes = [n('r'), n('b', 'r'), n('a', 'r'), n('a1', 'a')];
    const first = layoutForest(nodes);
    const second = layoutForest(nodes);
    expect([...first.entries()]).toEqual([...second.entries()]);
    // 孩子顺序 = 输入顺序：b 先出现，排在 a 之上。
    expect(first.get('b')!.y).toBeLessThan(first.get('a')!.y);
  });

  it('空森林与单节点', () => {
    expect(layoutForest([]).size).toBe(0);
    expect(layoutForest([n('solo')]).get('solo')).toEqual({ x: 0, y: 0 });
  });
});
