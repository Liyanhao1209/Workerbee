/**
 * 状态文案映射（OBS-01：后端枚举 ↔ 中文文案一一对应）。
 *
 * 关键的两条防线：
 * - 已知枚举：文案 / tone / 过渡与终态标记正确；
 * - 未知枚举：原样显示英文，绝不能变成 undefined 把页面打成白屏。
 */

import { describe, expect, it } from 'vitest';
import { STAGE_STATES, TASK_STATES } from './api/types';
import {
  EVENT_TYPE_LABELS,
  originOpLabel,
  STAGE_STATE_OPTIONS,
  stageStateLabel,
  TASK_STATE_OPTIONS,
  taskStateLabel,
} from './labels';

describe('taskStateLabel', () => {
  it('已知状态有中文文案，稳定态与过渡态区分', () => {
    expect(taskStateLabel('running').text).toBe('运行中');
    expect(taskStateLabel('pausing').transitioning).toBe(true);
    expect(taskStateLabel('succeeded').terminal).toBe(true);
    expect(taskStateLabel('queued').terminal).toBe(false);
  });

  it('未知状态原样显示英文并附说明，不返回 undefined', () => {
    const label = taskStateLabel('some_future_state');
    expect(label.text).toBe('some_future_state');
    expect(label.hint).toBeTruthy();
    expect(label.terminal).toBe(false);
  });
});

describe('stageStateLabel', () => {
  it('lost 是「状态不明」的过渡态（需要人工核对，不是终态）', () => {
    const label = stageStateLabel('lost');
    expect(label.text).toBe('状态不明');
    expect(label.transitioning).toBe(true);
    expect(label.terminal).toBe(false);
  });
});

describe('筛选器选项', () => {
  it('选项的取值与后端枚举逐一对应、顺序一致', () => {
    expect(TASK_STATE_OPTIONS.map((o) => o.value)).toEqual([...TASK_STATES]);
    expect(STAGE_STATE_OPTIONS.map((o) => o.value)).toEqual([...STAGE_STATES]);
  });
});

describe('事件与操作文案', () => {
  it('已知事件类型有中文名', () => {
    expect(EVENT_TYPE_LABELS['task.submitted']).toBe('任务已提交');
  });

  it('originOpLabel 未收录的取值原样返回', () => {
    expect(originOpLabel('pause')).toBe('暂停');
    expect(originOpLabel('unknown_op')).toBe('unknown_op');
  });
});
