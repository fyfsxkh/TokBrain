# TokBrain 主题系统

TokBrain 的主题由“基础配色 + 壁纸媒体”组成。13 套内置静态主题仍由前端注册表和限定
作用域的 CSS 提供；3 套内置动态主题复用对应基础配色，并加载随应用发布的本地视频。
自定义主题的元数据和媒体由本机后端保存，不接受远程 URL、CSS、HTML 或 SVG。

## 组成

- `registry.ts`：固定的 `ThemeId`、13 套静态主题、3 套动态主题、12 项语义色板和预览信息。
- `ThemeProvider.tsx`：主题选择、旧设置迁移、`localStorage` 持久化和多标签同步。
- `ThemeBackdrop.tsx`：全路由共享的静态/动态媒体层，以及减少动态效果和小屏降级。
- `styles/<theme-id>.css`：使用 `:root[data-theme="<theme-id>"]` 限定作用域的基础主题样式。
- `public/themes/*.webp`：静态背景与动态主题封面。
- `public/themes/dynamic/*.{mp4,webm}`：离线、无声、可循环的内置动态视频。
- `data/theme-assets/`：用户上传媒体；数据库只保存 UUID 路径和经过校验的元数据。

`classic-night` 是无图片依赖的安全回退主题。自定义主题必须选择一个有效的内置主题作为
基础配色，因此首屏脚本即使尚未读取自定义媒体，也能稳定显示完整界面。

## 新增内置静态主题

1. 在 `THEME_IDS` 增加稳定、全小写的主题 ID。
2. 在 `THEMES` 注册完整的 `ThemeDefinition`、预览和 12 项 `THEME_PALETTES`。
3. 复制 `styles/_template.css` 为 `styles/<theme-id>.css`，并在 `app/layout.tsx` 直接导入。
4. 如需背景图，使用无文字、无 UI、中央低对比的横向 WebP，建议控制在 250 KB 内。
5. 运行 `npm test`、`npm run lint`、`npm run typecheck` 和 `npm run build`。

## 新增内置动态主题

1. 为每套动态主题提供 WebP 封面和 WebM、H.264 MP4 两种同内容视频源。
2. 在 `DYNAMIC_THEMES` 注册稳定 ID、基础主题、封面和源列表。
3. 建议 1920×1080、30 fps 以内、20 秒以内的无声无缝循环，每种编码不超过 8 MB。
4. 设置页卡片只显示封面；只有当前主题创建一个 `<video preload="metadata">`。
5. 同时验证视频元数据、循环接缝、`prefers-reduced-motion`、760 px 断点和后台暂停。

## 自定义上传边界

- 图片/动画图片支持 JPEG、PNG、WebP 和 GIF，最大 30 MB；动画图片最长 60 秒。
- 视频支持 H.264 MP4 和 VP8/VP9/AV1 WebM，最大 256 MB、最长 5 分钟、最高 4K。
- 服务端按魔数、真实解码、ffprobe 元数据和 ffmpeg 完整性校验，不信任文件名或浏览器 MIME。
- 写入、修改和删除要求本机网页的 `Origin`；媒体通过 UUID 路由返回，不直接挂载数据目录。
- 调色只接受固定的 12 个 `#RRGGBB` 语义颜色，低对比度提示不改变业务控件的状态语义。

## 设计与可访问性约束

- 不从主题代码请求远程字体、脚本、图片或视频。
- 不隐藏功能、不伪造业务状态、不改变后端接口含义。
- 亮色主题必须覆盖输入框、代码块、提示和危险状态的可读性。
- 动态媒体必须 `muted`、`loop`、`playsInline`、`aria-hidden` 且不可获取焦点。
- 减少动态效果、强制色彩、小屏和后台标签页显示封面且不继续播放视频。
- 在 1100 px、760 px 和 375 px 下检查卡片、编辑器、流程和导航。

生成背景所使用的最终资产说明见 [ASSET_PROMPTS.md](ASSET_PROMPTS.md)。
