'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const asset = path.join(__dirname, '../src/local_agent_coordinator/room_ui');
function runtime({ language = 'en-CA', stored, denyStorage = false, desktop, hash = '' } = {}) {
  const storage = new Map(stored ? [['beanest:locale', stored]] : []);
  const context = vm.createContext({ window: { location: { hash }, ...(desktop ? { beanestDesktop: desktop } : {}) }, navigator: { language },
    document: { querySelectorAll: () => [], documentElement: {}, title: '' },
    localStorage: { getItem: key => { if (denyStorage) throw Error('denied'); return storage.get(key); }, setItem: (key,value) => { if (denyStorage) throw Error('denied'); storage.set(key,value); } },
    sessionStorage: { getItem: () => null } });
  for (const file of ['i18n-en-a.js','i18n-en-b.js','i18n-en-c.js','i18n.js']) vm.runInContext(fs.readFileSync(path.join(asset,file),'utf8'),context);
  return { api: context.window.BeanestI18n, dictionary: context.window.BeanestI18nEnglish, context, storage };
}
test('English catalog covers all three source shards without blank copy or changed template parameters', () => {
  const {dictionary} = runtime(); assert.ok(Object.keys(dictionary).length > 1800);
  for (const [key,value] of Object.entries(dictionary)) {
    assert.equal(typeof value, 'string',key); assert.ok(value.trim(),key); assert.doesNotMatch(value,/[一-龥]/,key);
    const params = text => [...text.matchAll(/\{p\d+\}/g)].map(match=>match[0]).sort();
    assert.deepEqual(params(value),params(key),key);
  }
});
test('localization substitutes authored parameters verbatim and translates only seeded labels', () => {
  const {api} = runtime();
  const opaque = '新任务 <script>不会执行</script> {p1}';
  assert.ok(api.t('第{p0}阶段 · ',{p0:opaque}).includes(opaque));
  const original = { system: '新任务' }; const labels = api.labels(original);
  assert.equal(labels.system,'New task'); original.user = '新任务'; assert.equal(labels.user,'新任务');
  original.system = '用户的新任务'; assert.equal(labels.system,'用户的新任务');
  assert.equal(api.t('未登记的用户文字'),'未登记的用户文字');
});
test('storage denial does not prevent system-language defaults or session switching, and invalid locale is ignored', () => {
  const {api} = runtime({denyStorage:true}); assert.equal(api.getLocale(),'en');
  api.setLocale('zh-CN'); assert.equal(api.getLocale(),'zh-CN'); api.setLocale('fr'); assert.equal(api.getLocale(),'zh-CN');
  assert.equal(runtime({language:'en-US',stored:'zh-CN'}).api.getLocale(),'zh-CN');
  assert.equal(runtime({language:'en-US',hash:'#room=existing'}).api.getLocale(),'zh-CN');
});
test('every English entry is still used by the source, so renamed copy cannot leave stale translations behind', () => {
  // Catches dead keys like the nine left by the 交付文件 → 工作快照对照 rename (copy audit §1.9).
  const {dictionary} = runtime();
  const root = path.join(__dirname, '..');
  const sources = [path.join(asset, 'app.js'), path.join(asset, 'markdown.js'), path.join(asset, 'index.html'),
    ...fs.readdirSync(path.join(root, 'src/local_agent_coordinator')).filter(name => name.endsWith('.py')).map(name => path.join(root, 'src/local_agent_coordinator', name))]
    .map(file => fs.readFileSync(file, 'utf8')).join('\n');
  const literal = key => [JSON.stringify(key), `'${key}'`, JSON.stringify(key).slice(1, -1)].some(form => sources.includes(form));
  const unused = Object.keys(dictionary).filter(key => !literal(key));
  assert.deepEqual(unused, []);
});
test('English sentences built from parts keep their spaces and never splice a verb into another sentence', () => {
  // Catches the audit's glued renders: "New goalTask permissions", "Partner configuration Disable.", "Claude started Task".
  const {api} = runtime();
  assert.equal(api.t('{p0}任务权限', { p0: api.t('新目标 · ') }), 'New goal · Task permissions');
  assert.equal(api.t('伙伴已停用。{p0}', { p0: api.t('已有聊天和成果保留。') }).startsWith('Partner disabled. '), true);
  assert.equal(api.t('{p0} 开始处理审核任务', { p0: 'Claude' }), 'Claude started a review');
  assert.equal(api.t('{p0} 向 {p1} 发起求助', { p0: 'Claude', p1: 'Codex' }), 'Claude asked Codex for help');
  assert.equal(api.t('正在{p0}', { p0: 'Read file' }), 'In progress: Read file');
  assert.equal(api.t('本轮已交付') === api.t('阶段已交付'), false, 'a round and a stage read differently');
});
