import test from 'node:test';
import assert from 'node:assert/strict';
import { fixedProgramKind, fixedProgramOrigin, fixedProgramTitle, nativeNext } from '../src/shared/fixed-program';

test('未知适配器也不能走研究表单，缺少适配器信息不伪造类型', () => {
  assert.equal(fixedProgramKind(null), '');
  assert.equal(fixedProgramKind({ adapter: [] }), '');
  assert.equal(fixedProgramKind({ adapter: { kind: 4 } }), '');
  assert.equal(fixedProgramKind({ adapter: { kind: 'future/v1' } }), 'future/v1');
  assert.equal(fixedProgramTitle({ adapter: { kind: 'future/v1' } }), '适配器任务');
  assert.equal(fixedProgramTitle({ adapter: { kind: 'langgraph_program/v1' } }), 'LangGraph 固定图');
});
test('来源优先使用固定录制或 Fork 的来源，不借用别的运行标识', () => {
  assert.equal(fixedProgramOrigin({ adapter: { recording: { source_run_id: 'recorded-source' }, fork: { source_run_id: 'fork-source' } }, origin_run_id: 'origin' }), 'recorded-source');
  assert.equal(fixedProgramOrigin({ adapter: { fork: { source_run_id: 'fork-source' } }, origin_run_id: 'origin' }), 'fork-source');
  assert.equal(fixedProgramOrigin({ adapter: { recording: [] }, origin_run_id: 'origin' }), 'origin');
});
test('没有原生检查点与没有后续节点分开，未知元数据不显示已完成', () => {
  assert.equal(nativeNext({ native_next: [] }), '尚无原生检查点');
  assert.equal(nativeNext({ native_ref: 'native' }), '未知');
  assert.equal(nativeNext({ native_ref: 'native', native_next: [] }), '无后续节点');
  assert.equal(nativeNext({ native_ref: 'native', native_next: ['review'] }), '审阅');
  assert.equal(nativeNext({ native_ref: 'native', native_next: ['other', 4] }), 'other → 未知节点');
});
