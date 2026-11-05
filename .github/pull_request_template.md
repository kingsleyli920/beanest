## What changed · 改了什么

<!-- The problem and the behavior after this change. · 要解决的问题，以及改完后的表现。 -->

## How it was verified · 怎么验证的

<!-- Tests you ran. Say which parts used provider substitutes and which used a real agent. · 跑了哪些测试；哪些用的是替身，哪些接了真实 Agent。 -->

- [ ] `.venv/bin/python -m unittest discover -s tests`
- [ ] `node --test tests/room_ui.test.cjs tests/room_i18n.test.cjs`
- [ ] `npm --prefix desktop test`

## Remaining limits · 还没覆盖的

## Checklist · 自查

- [ ] Interface text is updated in both Chinese and English. · 界面文案中英文都改了。
- [ ] No credentials, runtime data, local paths, or personal screenshots. · 没有凭据、运行数据、本机路径或带个人信息的截图。
- [ ] New dependencies or assets list their source and license. · 新依赖或素材注明了来源和许可证。
