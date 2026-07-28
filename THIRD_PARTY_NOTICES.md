# 第三方软件声明 / Third-party notices

## 中文

TokBrain 包含基于下列项目修改、构建或参考设计的原创工作。开源许可证只处理软件
著作权，不授予访问第三方平台的权限，也不授予处理、再分发或商业使用平台内容的
许可。

### via007/bilibili-rag

- 源代码：<https://github.com/via007/bilibili-rag>
- 参考版本：`1fa75303e1036894fbf03b41319375ea9835e277`
- 获取日期：2026-07-19
- 许可证：MIT
- 版权声明：Copyright (c) 2026 via007

上游版权声明和 MIT 授权声明已保留在根目录的英文
[`LICENSE`](LICENSE) 文件中；另提供不具替代效力的
[`LICENSE.zh-CN`](LICENSE.zh-CN) 中文参考译文。TokBrain 已对平台集成、持久化、
处理流水线、安全边界和用户界面作出实质性修改。

### Johnserf-Seed/f2

- 源代码：<https://github.com/Johnserf-Seed/f2>
- 运行时包：`f2==0.0.1.7`
- 许可证：Apache License 2.0
- 上游公布的版权声明：Copyright (c) 2023 JohnserfSeed

F2 作为外部 Python 依赖下载，不提交到本仓库。由于 0.0.1.7 的包元数据固定了
过期的精确依赖版本并包含不必要的开发工具，安装时使用 `--no-deps` 将 F2 自身放入
被 Git 忽略的本地 `.vendor/` 目录；经审计的兼容运行时依赖则单独声明在
`requirements.txt` 中。F2 为 TokBrain 有界、由用户主动触发的抖音单链接解析提供
非官方平台请求和响应处理。F2 的软件许可证和上游免责声明不代表已获得抖音或
字节跳动授权。

### 其他依赖

直接使用的 Python 和 JavaScript 依赖声明在
[`requirements.txt`](requirements.txt) 和
[`frontend/package-lock.json`](frontend/package-lock.json) 中。这些依赖声明的
许可证包括 MIT、Apache-2.0、BSD 系列、HPND 和 LGPL 系列。分发者如果一并提供
依赖或编译后的二进制文件，必须保留各依赖许可证要求的全部声明。

### 主题背景资产

`frontend/public/themes/` 中的 12 张 WebP 环境背景是为 TokBrain 生成的原创 AI
辅助资产；生成时要求不包含人物、文字、品牌、Logo 或水印。参考图和其他源图像均未
提交到本仓库。在相关权利可由本项目许可的范围内，这些最终背景资产与仓库代码一并按
MIT License 提供。该说明不对生成式模型训练材料或不可由本项目控制的第三方权利作出
保证；如认为某项资产侵犯权利，请通过 GitHub 的私有漏洞报告功能联系维护者。

TokBrain、via007/bilibili-rag 和 F2 均不隶属于抖音、字节跳动、阿里云或任何内容
创作者，也未获得其认可或背书。

---

## English

TokBrain includes modifications and original work built from, or designed with
reference to, the following projects. Open-source licenses apply to software
copyright only; they do not grant access to third-party platforms or permission
to process, redistribute, or commercially use platform content.

### via007/bilibili-rag

- Source: <https://github.com/via007/bilibili-rag>
- Reference revision: `1fa75303e1036894fbf03b41319375ea9835e277`
- Retrieved: 2026-07-19
- License: MIT
- Copyright: Copyright (c) 2026 via007

The upstream copyright and MIT permission notice are preserved in the root
English [`LICENSE`](LICENSE) file. A non-substitutive Chinese reference
translation is available in [`LICENSE.zh-CN`](LICENSE.zh-CN). TokBrain has
substantially changed the platform integration, persistence, processing
pipeline, security boundaries, and user interface.

### Johnserf-Seed/f2

- Source: <https://github.com/Johnserf-Seed/f2>
- Runtime package: `f2==0.0.1.7`
- License: Apache License 2.0
- Copyright notice published by upstream: Copyright (c) 2023 JohnserfSeed

F2 is downloaded as an external Python dependency and is not committed to this
repository. Its package is installed with `--no-deps` into the ignored local
`.vendor/` directory because version 0.0.1.7 declares obsolete exact dependency
pins and unnecessary development tools. Audited compatible runtime dependencies
are declared separately in `requirements.txt`. F2 provides the non-official
platform request and response handling used by TokBrain's bounded,
user-triggered Douyin link resolver. F2's software license and upstream
disclaimer do not imply authorization from Douyin or ByteDance.

### Other dependencies

Direct Python and JavaScript dependencies are declared in
[`requirements.txt`](requirements.txt) and
[`frontend/package-lock.json`](frontend/package-lock.json). Their declared
licenses include MIT, Apache-2.0, BSD-family, HPND, and LGPL-family licenses.
A distributor that ships dependencies or compiled binaries must preserve all
notices required by the corresponding dependency licenses.

### Theme background assets

The 12 WebP environment backgrounds in `frontend/public/themes/` are original,
AI-assisted assets generated for TokBrain under prompts that exclude people,
text, brands, logos, and watermarks. No reference or source images are included
in this repository. To the extent the relevant rights can be licensed by this
project, the final backgrounds are provided under the MIT License together
with the repository code. This statement makes no representation about model
training materials or third-party rights outside the project's control. A
rights concern may be reported privately through GitHub Private Vulnerability
Reporting.

TokBrain, via007/bilibili-rag, and F2 are not affiliated with or endorsed by
Douyin, ByteDance, Alibaba Cloud, or any content creator.
