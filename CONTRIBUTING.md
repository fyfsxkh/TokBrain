# 贡献指南 / Contributing

## 中文

TokBrain 欢迎能够保持“仅本地运行、由用户主动触发、处理范围有界”这一模型的修复和
改进。

### 不可突破的边界

Pull Request 不得增加或帮助实现下列能力：

- 抓取账号、收藏夹、关注列表、推荐流、评论或个人资料；
- 自动登录、提取浏览器 Cookie、破解验证码、绕过风控、伪造设备指纹或研究签名；
- 提高平台访问并发、移除每日限额/冷却时间，或在平台拒绝访问后自动重试；
- 在作者禁止下载、权限缺失或含义不明确时处理完整视频；
- 记录或返回 Cookie、带签名的媒体 URL、响应正文、API Key、本地路径或用户私密数据；
- 再分发已下载的创作者媒体，或使用包含个人数据的真实平台响应作为测试 fixture。

涉及平台访问、媒体权限处理、重定向、DNS 检查、文件上传、秘密存储或进程执行的
修改，必须增加有针对性的回归测试；如果风险状况发生变化，还必须更新
`OPEN_SOURCE_RISK_REPORT.md`。

### 开发检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
cd frontend
npm test
npm run lint
npm run build
npm audit --omit=dev --audit-level=high
```

平台相关测试必须使用人工构造的离线 fixture 和模拟传输层。CI 绝不能连接真实抖音，
也不能下载真实创作者内容。

### 贡献许可

提交代码、文档或资产即表示贡献者确认自己有权提交相关内容，并同意按照本仓库的
MIT License 对贡献进行许可。贡献不得包含未经授权的第三方代码、媒体、个人数据、
商业秘密或受保密义务约束的材料。

### Pull Request 检查清单

- 不包含密钥、数据库、日志、媒体或个人数据。
- 新增网络行为必须由用户主动触发、范围有界，并采用失败关闭策略。
- 在适用时，测试覆盖拒绝、超时、响应格式错误、重定向和取消路径。
- 已同步更新文档和第三方声明。
- 修改不会暗示与任何平台存在隶属关系或已经获得平台授权。

---

## English

TokBrain welcomes fixes and improvements that preserve its local-only,
user-triggered and bounded processing model.

### Non-negotiable boundaries

Pull requests must not add or facilitate:

- account, favorites, following, recommendation-feed, comment, or profile
  scraping;
- automatic login, browser-cookie extraction, CAPTCHA solving, risk-control
  bypass, device fingerprint spoofing, or signature research;
- higher platform concurrency, removal of daily limits/cooldowns, or automatic
  retry after a platform denial;
- full-video processing when author download permission is denied, missing, or
  ambiguous;
- logging or returning cookies, signed media URLs, response bodies, API keys,
  local paths, or private user data;
- redistribution of downloaded creator media or test fixtures copied from a
  real platform response when they contain personal data.

Changes to platform access, media permission handling, redirects, DNS checks,
file uploads, secret storage, or process execution require focused regression
tests and an update to `OPEN_SOURCE_RISK_REPORT.md` when the risk profile
changes.

### Development checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
cd frontend
npm test
npm run lint
npm run build
npm audit --omit=dev --audit-level=high
```

Platform tests must use synthetic offline fixtures and mocked transports. CI
must never contact Douyin or download real creator content.

### Contribution license

By submitting code, documentation, or assets, a contributor represents that
they have the right to submit the material and agrees to license the
contribution under this repository's MIT License. Contributions must not
contain unauthorized third-party code, media, personal data, trade secrets, or
material subject to a confidentiality obligation.

### Pull request checklist

- No secret, database, log, media, or personal data is included.
- New network behavior is user-triggered, bounded, and fail-closed.
- Tests cover denial, timeout, malformed response, redirect, and cancellation
  paths where applicable.
- Documentation and third-party notices are updated.
- The change does not imply affiliation with or authorization from a platform.
