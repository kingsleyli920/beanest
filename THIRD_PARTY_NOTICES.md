# Third-party notices · 第三方说明

Beanest's own code and project-owned bean artwork are distributed under the [MIT License](LICENSE). Third-party software and services retain their own licenses and terms. The repository distributes source, dependency declarations, and lockfiles; it does not include installed `node_modules`, `.venv`, provider CLIs, Docker images, or a built Electron/Python distribution.

| Component | Declared version / purpose | License / source |
| --- | --- | --- |
| pathspec | 1.1.1; project ignore rules | MPL-2.0, [project](https://github.com/cpburnz/python-pathspec) |
| Electron | 44.9.3; local desktop runtime | MIT plus Chromium and other third-party notices, [license](https://github.com/electron/electron/blob/v44.9.3/LICENSE) |
| CPython | 3.13.9 standalone build; runtime inside the release app only | PSF-2.0 and bundled native-library licenses, [python-build-standalone](https://github.com/astral-sh/python-build-standalone) |
| @electron/packager | 20.3.0; development packaging | BSD-2-Clause, [project](https://github.com/electron/packager) |
| mcp | 2.3.0; optional Python MCP bridge | MIT, [SDK](https://github.com/modelcontextprotocol/python-sdk) |
| pydantic | 2.14.2; optional bridge validation | MIT, [project](https://github.com/pydantic/pydantic) |

The Python declarations are in `pyproject.toml`; desktop versions and transitive dependencies are in `desktop/package-lock.json`. Resolve and inspect the actual dependency set for your target environment rather than treating this table as a complete binary-distribution bill of materials.

### Redistributing builds

Preserve the licenses, copyright notices, and applicable NOTICE files of everything actually included. An Electron distribution requires its Electron license and Chromium third-party notices; a bundled Python runtime and native libraries require their own notices. Preserve pathspec's MPL notices and the source-access obligations that apply to any copies or modifications you distribute. Beanest does not modify or vendor pathspec.

The local desktop builder creates a checkout-dependent development app. `npm --prefix desktop run build:release` creates a standalone app that includes CPython, unmodified pathspec and Electron with their notices. It is ad-hoc signed, not notarized, and this release does not claim a completed binary-distribution audit.

### External tools and services

Claude Code, Codex, and Kimi Code are installed and authenticated by the user. Compatibility with their interfaces does not grant rights to their accounts, models, subscriptions, trademarks, or services, and does not imply official endorsement. No third-party brand logos are bundled as Beanest artwork.

Docker and any Python container image are optional, separately obtained tools. Their licenses and service terms apply independently. Installing this repository does not install Docker, accept its terms, or download execution images automatically.

## 简体中文

豆窝自有代码与项目自身的豆子图形采用 [MIT](LICENSE)。依赖和外部服务各自保留许可及条款。仓库仅发布源码、依赖声明和锁文件，不包含已安装依赖、Agent CLI、Docker 镜像或 Electron/Python 发行包。

上表列出直接依赖及来源，实际传递依赖以目标环境解析结果和桌面锁文件为准，不能把这张表当成完整二进制物料清单。再次分发构建产物时，须保留实际包含组件的完整许可、版权和适用 NOTICE；Electron 另含 Chromium 等告示，捆绑 Python 与原生库也有独立义务。分发或修改 pathspec 时应遵循 MPL-2.0；发布版 App 内置未修改的 pathspec，源码包不内置。

本机构建应用依赖当前源码目录。`npm --prefix desktop run build:release` 生成自带 CPython、pathspec 和 Electron 及其许可的独立 App，目前只做了临时签名，没有公证。本次源码开源不宣称已完成二进制发行审计。供应商账号、模型、会员和商标权益不由豆窝 MIT 授予，也不代表官方背书。Docker 与执行镜像由用户按需独立获取和管理。
