# Third-party notices · 第三方说明

Beanest's own code and project-owned bean artwork are distributed under the [MIT License](LICENSE). Third-party software and services retain their own licenses and terms. The repository distributes source, dependency declarations, and lockfiles; it does not include installed `node_modules`, `.venv`, provider CLIs, Docker images, or a built Electron/Python distribution.

| Component | Declared version / purpose | License / source |
| --- | --- | --- |
| pathspec | 1.1.1; project ignore rules | MPL-2.0, [project](https://github.com/cpburnz/python-pathspec) |
| Electron | 44.9.3; local desktop runtime | MIT plus Chromium and other third-party notices, [license](https://github.com/electron/electron/blob/v44.9.3/LICENSE) |
| CPython | 3.13.9 standalone build; runtime inside the release app only | PSF-2.0 and bundled native-library licenses, [python-build-standalone](https://github.com/astral-sh/python-build-standalone) |
| @electron/packager | 20.3.0; development packaging | BSD-2-Clause, [project](https://github.com/electron/packager) |
| mcp | 2.3.0; optional Python MCP bridge, not in the release app | MIT, [SDK](https://github.com/modelcontextprotocol/python-sdk) |
| pydantic | 2.14.2; optional bridge validation, not in the release app | MIT, [project](https://github.com/pydantic/pydantic) |

The Python declarations are in `pyproject.toml`; desktop versions and transitive dependencies are in `desktop/package-lock.json`. This table covers the source tree. Resolve and inspect the dependency set for your own target environment; what the release app contains is listed below.

### Redistributing builds

Preserve the licenses, copyright notices, and applicable NOTICE files of everything actually included. An Electron distribution requires its Electron license and Chromium third-party notices; a bundled Python runtime and native libraries require their own notices. Preserve pathspec's MPL notices and the source-access obligations that apply to any copies or modifications you distribute. Beanest does not modify or vendor pathspec.

The local desktop builder creates a checkout-dependent development app. `npm --prefix desktop run build:release` creates a standalone app containing the components listed in the next section. It is ad-hoc signed, not notarized.

### What the release app contains

Paths are under `Contents/Resources/` inside the app. Components from OpenSSL through HACL* below, together with SQLite and liblzma, are linked into `python/lib/libpython3.13.dylib`.

| Component | Version | License | Notice in the app |
| --- | --- | --- | --- |
| Beanest | 0.3.0 | MIT | `LICENSE`, `THIRD_PARTY_NOTICES.md` |
| Electron | 44.9.3 | MIT | `ELECTRON-LICENSE` |
| Chromium and the code it includes, such as V8 and Squirrel.Mac | 152.0.7977.167 | Per component | `LICENSES.chromium.html` |
| FFmpeg (`libffmpeg.dylib`) | `git-2026-07-15-6cfe2122b0` | LGPL-2.1-or-later | `LICENSES.chromium.html`; source: `licenses/RELEASE-SOURCES.md` |
| Mantle.framework | commit `2a8e2123`, pinned by Electron v44.9.3 | MIT | `licenses/mantle-LICENSE.md` |
| ReactiveObjC.framework | commit `74ab5bac`, pinned by Electron v44.9.3 | MIT | `licenses/reactiveobjc-LICENSE.md` |
| CPython | 3.13.9, python-build-standalone tag `20251028` | PSF-2.0 | `python/lib/python3.13/LICENSE.txt`, `licenses/cpython-license.rst` |
| OpenSSL | 3.5.4 | Apache-2.0 | `licenses/openssl-LICENSE.txt` |
| expat | 2.6.3 | MIT | `licenses/expat-COPYING.txt` |
| libffi | 3.4.6 | MIT | `licenses/libffi-LICENSE.txt` |
| bzip2 | 1.0.8 | bzip2 license | `licenses/bzip2-LICENSE.txt` |
| libmpdec | 4.0.0 | BSD-2-Clause | `licenses/libmpdec-COPYRIGHT.txt` |
| libuuid | 1.0.3 | BSD-3-Clause | `licenses/libuuid-LICENSE.txt` |
| mimalloc | 2.1.2, vendored by CPython | MIT | `licenses/mimalloc-LICENSE.txt` |
| HACL* | commit `bb3d0dc8`, vendored by CPython | MIT | `licenses/hacl-star-LICENSE.txt` |
| SQLite | 3.50.4 | Public domain | None required |
| liblzma | 5.2.12 | Public domain | None required |
| pathspec | 1.1.1, unmodified | MPL-2.0 | `backend/pathspec-1.1.1.dist-info/licenses/LICENSE`; source: https://github.com/cpburnz/python-pathspec/tree/v1.1.1 |

`zlib`, `libedit` and `ncurses` come from macOS itself and are not included. `mcp` and `pydantic` are not in the app, and neither are the two optional MCP server modules that use them (`mcp_server.py`, `room_mcp.py`).

Changes to the bundled Python: the standard library is trimmed of development and GUI parts, the `libpython` install name is rewritten to `@rpath`, build-machine paths in `_sysconfigdata` are replaced with `/beanest-python`, and `.pyc` files are compiled in advance.

FFmpeg is LGPL-2.1-or-later and stays a separate dynamic library. It comes unchanged, apart from the code signature, from the prebuilt Electron `v44.9.3`. Its version string is `git-2026-07-15-6cfe2122b0`. Source: the [Electron tag](https://github.com/electron/electron/tree/v44.9.3) pins [Chromium 152.0.7977.167](https://chromium.googlesource.com/chromium/src/+/refs/tags/152.0.7977.167), which pins [its FFmpeg commit](https://chromium.googlesource.com/chromium/third_party/ffmpeg/+/2b68d2babae73714846961fb0ee47e3b3d2e39a9); upstream is https://git.ffmpeg.org/ffmpeg.git. Details are in `desktop/release-notices/RELEASE-SOURCES.md`, and in the app at `licenses/RELEASE-SOURCES.md`.

### External tools and services

Claude Code, Codex, and Kimi Code are installed and authenticated by the user. Compatibility with their interfaces does not grant rights to their accounts, models, subscriptions, trademarks, or services, and does not imply official endorsement. No third-party brand logos are bundled as Beanest artwork.

Docker and any Python container image are optional, separately obtained tools. Their licenses and service terms apply independently. Installing this repository does not install Docker, accept its terms, or download execution images automatically.

## 简体中文

豆窝自有代码与项目自身的豆子图形采用 [MIT](LICENSE)。依赖和外部服务各自保留许可及条款。仓库仅发布源码、依赖声明和锁文件，不包含已安装依赖、Agent CLI、Docker 镜像或 Electron/Python 发行包。

上表列出直接依赖及来源，实际传递依赖以目标环境解析结果和桌面锁文件为准，不能把这张表当成完整二进制物料清单。再次分发构建产物时，须保留实际包含组件的完整许可、版权和适用 NOTICE；Electron 另含 Chromium 等告示，捆绑 Python 与原生库也有独立义务。分发或修改 pathspec 时应遵循 MPL-2.0；发布版 App 内置未修改的 pathspec，源码包不内置。

本机构建应用依赖当前源码目录。`npm --prefix desktop run build:release` 生成的独立 App 包含下一节列出的组件，目前只做了临时签名，没有公证。供应商账号、模型、会员和商标权益不由豆窝 MIT 授予，也不代表官方背书。Docker 与执行镜像由用户按需独立获取和管理。

### 发布版 App 实际内容

下面的路径都在 App 的 `Contents/Resources/` 之下。表中从 OpenSSL 到 HACL* 的组件以及 SQLite、liblzma 都链接在 `python/lib/libpython3.13.dylib` 里。

| 组件 | 版本 | 许可 | App 内的许可文件 |
| --- | --- | --- | --- |
| 豆窝 | 0.3.0 | MIT | `LICENSE`、`THIRD_PARTY_NOTICES.md` |
| Electron | 44.9.3 | MIT | `ELECTRON-LICENSE` |
| Chromium 及其包含的代码（如 V8、Squirrel.Mac） | 152.0.7977.167 | 各组件不同 | `LICENSES.chromium.html` |
| FFmpeg（`libffmpeg.dylib`） | `git-2026-07-15-6cfe2122b0` | LGPL-2.1-or-later | `LICENSES.chromium.html`；源码位置见 `licenses/RELEASE-SOURCES.md` |
| Mantle.framework | 提交 `2a8e2123`（Electron v44.9.3 锁定） | MIT | `licenses/mantle-LICENSE.md` |
| ReactiveObjC.framework | 提交 `74ab5bac`（Electron v44.9.3 锁定） | MIT | `licenses/reactiveobjc-LICENSE.md` |
| CPython | 3.13.9，python-build-standalone 标签 `20251028` | PSF-2.0 | `python/lib/python3.13/LICENSE.txt`、`licenses/cpython-license.rst` |
| OpenSSL | 3.5.4 | Apache-2.0 | `licenses/openssl-LICENSE.txt` |
| expat | 2.6.3 | MIT | `licenses/expat-COPYING.txt` |
| libffi | 3.4.6 | MIT | `licenses/libffi-LICENSE.txt` |
| bzip2 | 1.0.8 | bzip2 许可 | `licenses/bzip2-LICENSE.txt` |
| libmpdec | 4.0.0 | BSD-2-Clause | `licenses/libmpdec-COPYRIGHT.txt` |
| libuuid | 1.0.3 | BSD-3-Clause | `licenses/libuuid-LICENSE.txt` |
| mimalloc | 2.1.2（CPython 内置） | MIT | `licenses/mimalloc-LICENSE.txt` |
| HACL* | 提交 `bb3d0dc8`（CPython 内置） | MIT | `licenses/hacl-star-LICENSE.txt` |
| SQLite | 3.50.4 | 公有领域 | 无需附带 |
| liblzma | 5.2.12 | 公有领域 | 无需附带 |
| pathspec | 1.1.1，未修改 | MPL-2.0 | `backend/pathspec-1.1.1.dist-info/licenses/LICENSE`；源码：https://github.com/cpburnz/python-pathspec/tree/v1.1.1 |

`zlib`、`libedit`、`ncurses` 用的是 macOS 系统自带的，不在包内。`mcp` 和 `pydantic` 不在 App 内，使用它们的两个可选 MCP 服务模块（`mcp_server.py`、`room_mcp.py`）也不在。

对内置 Python 的改动：裁掉标准库里开发和图形界面相关的部分，把 `libpython` 的 install name 改成 `@rpath`，把 `_sysconfigdata` 里的构建机路径换成 `/beanest-python`，并预先编译 `.pyc`。

FFmpeg 按 LGPL-2.1-or-later 授权，仍是单独的动态库，来自 Electron `v44.9.3` 预编译包，除代码签名外没有改动，版本串 `git-2026-07-15-6cfe2122b0`。源码这样找：[Electron 的 tag](https://github.com/electron/electron/tree/v44.9.3) 锁定 [Chromium 152.0.7977.167](https://chromium.googlesource.com/chromium/src/+/refs/tags/152.0.7977.167)，Chromium 再锁定[这个 FFmpeg 提交](https://chromium.googlesource.com/chromium/third_party/ffmpeg/+/2b68d2babae73714846961fb0ee47e3b3d2e39a9)，上游仓库是 https://git.ffmpeg.org/ffmpeg.git。详见 `desktop/release-notices/RELEASE-SOURCES.md`，App 内对应 `licenses/RELEASE-SOURCES.md`。
