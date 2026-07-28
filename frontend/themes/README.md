# TokBrain 主题包

TokBrain 的主题是纯前端、设备本地的外观扩展。主题不会写入后端数据库，也不会改变导入与处理状态。

## 组成

- `registry.ts`：公开的 `ThemeDefinition`、主题文案、预览色板与资源声明。
- `ThemeProvider.tsx`：即时切换、`localStorage` 持久化和多标签同步。
- `styles/<theme-id>.css`：必须使用 `:root[data-theme="<theme-id>"]` 限定作用域。
- `public/themes/<theme-id>.webp`：可选的本地环境背景，不得包含文字或烘焙后的界面。

当前内置 13 套主题。`classic-night` 是无图片依赖的安全回退主题。

## 新增主题

1. 在 `THEME_IDS` 增加稳定、全小写的主题 ID。
2. 在 `THEMES` 注册一份完整 `ThemeDefinition`：
   - `preview` 用于设置页的迷你预览。
   - `copy` 只改变展示隐喻，不得改变业务含义。
   - `assets.background` 留空时会按 `/themes/<theme-id>.webp` 推导。
3. 复制 `styles/_template.css` 为 `styles/<theme-id>.css`，所有规则必须带主题作用域。
4. 在 `app/layout.tsx` 直接导入新样式。不要用 CSS `@import` 聚合主题；Next/Turbopack 在部分 Windows 中文路径下无法正确解析这类相对导入。
5. 如需背景图，使用无文字、无 UI、中央低对比的横向 WebP，建议控制在 250 KB 内。
6. 运行 `npm test`、`npm run lint` 和 `npm run build`。

## 设计约束

- 不从主题代码请求远程字体、脚本或图片。
- 不隐藏功能、不伪造业务状态、不改变后端接口。
- 亮色主题必须覆盖输入框、代码块、提示和危险状态的可读性。
- 动效必须支持 `prefers-reduced-motion`。
- 在 1100px 与 760px 断点下检查卡片、流程和导航。
- 主题化名称需要保留清晰的 `aria-label`，例如“知识巢”仍表示“知识库”。

生成背景所使用的最终资产说明见 [ASSET_PROMPTS.md](ASSET_PROMPTS.md)。
