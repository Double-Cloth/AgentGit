import test from 'node:test';
import assert from 'node:assert/strict';
import { a2aCancel, a2aProgram, a2aRemote, a2aServer } from '../src/shared/a2a';
import { fixedProgramTitle } from '../src/shared/fixed-program';

test('A2A登记拒绝替代地址和秘密主体，未知状态不显示成功', () => {
  for (const endpoint of ['http://localhost:8123', 'http://127.0.0.1:0', 'http://127.0.0.1:65536', 'http://127.0.0.1:8123/', 'http://192.168.1.1:8123', 'https://127.0.0.1:8123']) assert.throws(() => a2aServer(endpoint, 'server', 'actor'));
  assert.throws(() => a2aServer('http://127.0.0.1:8123', 'server', 'Bearer secret'));
  assert.equal(a2aServer('http://127.0.0.1:8123', 'server', 'actor').protocol_version, '1.0');
  assert.equal(a2aRemote(null), '尚无确认回执');
  assert.equal(a2aRemote('future'), 'future');
  assert.equal(a2aRemote('TASK_STATE_WORKING'), '处理中');
  assert.equal(a2aCancel('not_confirmed'), '尚未确认');
  assert.equal(a2aCancel(undefined), '未知');
  assert.equal(fixedProgramTitle({ adapter: { kind: 'a2a_program/v1' } }), 'A2A 远端任务');
});

test('固定消息保留公开文本、独立messageId及canonical键序，无任意任务标识', () => {
  const binding = { schema_version: 1, server: {}, card_json: '{}' };
  let id = 0;
  const program = a2aProgram(binding, ['中文\n公开资料', '后续输入'], () => `message-${++id}`);
  const messages = program.messages as string[];
  assert.equal(messages[0], '{"configuration":{"acceptedOutputModes":["text/plain","application/json"],"returnImmediately":true},"message":{"messageId":"message-1","parts":[{"mediaType":"text/plain","text":"中文\\n公开资料"}],"role":"ROLE_USER"}}');
  assert.equal(JSON.parse(messages[1]!).message.messageId, 'message-2');
  assert.equal(program.max_polls, 16);
  for (const texts of [[], [''], Array.from({ length: 5 }, () => 'x'), ['x'.repeat(4001)]]) assert.throws(() => a2aProgram(binding, texts, () => 'id'));
  assert.throws(() => a2aProgram({}, ['text'], () => 'id'));
});
