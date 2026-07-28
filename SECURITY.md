# 安全策略 / Security policy

## 中文

### 支持版本

安全修复只应用于默认分支的最新版本。旧快照和派生仓库不在支持范围内。

### 报告安全漏洞

对于可能导致下列信息或能力暴露的问题，请使用 GitHub 私有漏洞报告功能：

- API Key、F2 Cookie、由 DPAPI 保护的值或本地数据库内容；
- 任意本地文件、内部网络地址或不安全重定向；
- 属于其他用户的已下载媒体；
- 远程代码执行、命令注入或不受限制的文件上传；
- 绕过下载权限、速率限制或风险熔断控制。

请勿在公开 Issue 中提交真实密钥、Cookie、私密视频、数据库、日志压缩包或创作者
内容。请使用人工构造的数据，并遮盖 URL 和标识符。维护者应在公开仓库之前启用
**Settings → Security → Private vulnerability reporting**。

### 本地安全模型

TokBrain 的设计目标是在一台 Windows 电脑上由一名可信用户本地使用。后端：

- 只绑定 `127.0.0.1`/`localhost`；
- 限制 Host、Origin 和 CORS 值；
- 不实现多用户身份认证或权限控制；
- 在本地保存可变数据，并使用 Windows DPAPI 保护已配置的秘密。

不要通过局域网、反向代理、隧道、容器宿主机或公网暴露 3000、8000 端口。托管或
多用户部署需要重新进行安全设计，并补充身份认证、权限控制、CSRF 防护、租户隔离、
审计日志、数据保留控制和法律审查。

### 敏感文件

绝不要提交 `.env`、`data/`、`logs/`、`backups/`、SQLite 文件、已下载媒体、
字幕、关键帧、导出笔记或浏览器/会话凭据。每次公开发布前都应运行
`scripts/prepublish_check.ps1`。

---

## English

### Supported version

Security fixes are applied to the latest revision of the default branch. Older
snapshots and forks are not supported.

### Reporting a vulnerability

Please use GitHub Private Vulnerability Reporting for issues that could expose:

- API keys, F2 cookies, DPAPI-protected values, or local database contents;
- arbitrary local files, internal network addresses, or unsafe redirects;
- downloaded media belonging to another user;
- remote code execution, command injection, or unrestricted file upload;
- a bypass of the download-permission, rate-limit, or risk-circuit controls.

Do not include a real secret, Cookie, private video, database, log archive, or
creator content in a public issue. Use synthetic data and redact URLs and
identifiers. Maintainers should enable **Settings → Security → Private
vulnerability reporting** before making the repository public.

### Local security model

TokBrain is designed for one trusted user on one Windows machine. The backend:

- binds to `127.0.0.1`/`localhost`;
- restricts Host, Origin, and CORS values;
- does not implement multi-user authentication or authorization;
- stores mutable data locally and protects configured secrets with Windows
  DPAPI.

Do not expose ports 3000 or 8000 to a LAN, reverse proxy, tunnel, container
host, or the public Internet. A hosted or multi-user deployment requires a
separate security design, authentication, authorization, CSRF protection,
tenant isolation, audit logging, retention controls, and legal review.

### Sensitive files

Never commit `.env`, `data/`, `logs/`, `backups/`, SQLite files, downloaded
media, subtitles, keyframes, exported notes, or browser/session credentials.
Run `scripts/prepublish_check.ps1` before every public release.
