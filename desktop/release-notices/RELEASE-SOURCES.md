# Beanest 0.3.0 release: source locations · 发布版源码位置

This file applies to the release app and ships in it at `Contents/Resources/licenses/RELEASE-SOURCES.md`.

## FFmpeg (LGPL-2.1-or-later)

The app includes `libffmpeg.dylib` at `Contents/Frameworks/Electron Framework.framework/Versions/A/Libraries/`. Its version string is `git-2026-07-15-6cfe2122b0`. It comes from the prebuilt Electron v44.9.3 for macOS arm64, is unchanged except for the code signature, and stays a separate dynamic library. Its license text is the `ffmpeg` entry in `Contents/Resources/LICENSES.chromium.html`.

The Intel (x64) app carries the same library from the prebuilt Electron v44.9.3 for macOS x64. Its version string is also `git-2026-07-15-6cfe2122b0`, so the source below applies to both apps.

The corresponding source is attached to the same GitHub Release as `beanest-0.3.0-ffmpeg-source.tar.gz` (SHA-256 `131930c4fa37f07352660bf997d86044ac0a57cebdc7b809a0142d49e0f0be5a`): Chromium's FFmpeg tree at the commit below, the one patch Electron applies to it, and Electron's build arguments, with rebuild notes in its `README.md`. The same source can also be reached in this order:

| What | Where |
| --- | --- |
| Electron `v44.9.3` tag, whose `DEPS` pins Chromium | https://github.com/electron/electron/tree/v44.9.3 |
| Chromium `152.0.7977.167`, whose `DEPS` pins FFmpeg | https://chromium.googlesource.com/chromium/src/+/refs/tags/152.0.7977.167 |
| Chromium's FFmpeg at `2b68d2babae73714846961fb0ee47e3b3d2e39a9` | https://chromium.googlesource.com/chromium/third_party/ffmpeg/+/2b68d2babae73714846961fb0ee47e3b3d2e39a9 |
| Upstream FFmpeg (`6cfe2122b0` is the upstream commit in the version string) | https://git.ffmpeg.org/ffmpeg.git |

## 简体中文

App 内的 `libffmpeg.dylib` 位于 `Contents/Frameworks/Electron Framework.framework/Versions/A/Libraries/`，版本串 `git-2026-07-15-6cfe2122b0`。它来自 macOS arm64 的 Electron v44.9.3 预编译包，除代码签名外没有改动，仍是单独的动态库。许可文本见 `Contents/Resources/LICENSES.chromium.html` 中的 `ffmpeg` 一项。

Intel（x64）版 App 里的这个库来自 macOS x64 的 Electron v44.9.3 预编译包，版本串同样是 `git-2026-07-15-6cfe2122b0`，对应源码和 arm64 版相同。

对应源码作为附件 `beanest-0.3.0-ffmpeg-source.tar.gz` 放在同一个 GitHub Release 里（SHA-256 同上），包含下述提交的 Chromium FFmpeg 源码、Electron 对它打的唯一补丁和 Electron 的编译参数，重新编译的方法见包内 `README.md`。也可以按上表自己查找：Electron `v44.9.3` tag 的 `DEPS` 指向 Chromium `152.0.7977.167`，Chromium 的 `DEPS` 指向 FFmpeg 提交 `2b68d2babae73714846961fb0ee47e3b3d2e39a9`。上游 FFmpeg 仓库是 https://git.ffmpeg.org/ffmpeg.git。

## Where each notice file came from · 各许可文件的来源

| File | Component | Source |
| --- | --- | --- |
| `openssl-LICENSE.txt` | OpenSSL 3.5.4 | https://github.com/openssl/openssl/blob/openssl-3.5.4/LICENSE.txt |
| `expat-COPYING.txt` | expat 2.6.3 | https://github.com/libexpat/libexpat/blob/R_2_6_3/expat/COPYING |
| `libffi-LICENSE.txt` | libffi 3.4.6 | https://github.com/libffi/libffi/blob/v3.4.6/LICENSE |
| `bzip2-LICENSE.txt` | bzip2 1.0.8 | https://sourceware.org/git/?p=bzip2.git;a=blob_plain;f=LICENSE;hb=refs/tags/bzip2-1.0.8 |
| `libmpdec-COPYRIGHT.txt` | libmpdec 4.0.0 | `COPYRIGHT.txt` in https://www.bytereef.org/software/mpdecimal/releases/mpdecimal-4.0.0.tar.gz |
| `libuuid-LICENSE.txt` | libuuid 1.0.3 | Header of `gen_uuid.c` in https://sourceforge.net/projects/libuuid/files/libuuid-1.0.3.tar.gz; the `COPYING` file there only points to a license file the archive does not contain |
| `mimalloc-LICENSE.txt` | mimalloc 2.1.2, the version vendored by CPython 3.13.9 | https://github.com/microsoft/mimalloc/blob/v2.1.2/LICENSE |
| `hacl-star-LICENSE.txt` | HACL* at the commit vendored by CPython 3.13.9 | https://github.com/hacl-star/hacl-star/blob/bb3d0dc8d9d15a5cd51094d5b69e70aa09005ff0/dist/LICENSE.txt |
| `mantle-LICENSE.md` | Mantle at the commit pinned by Electron v44.9.3 | https://github.com/Mantle/Mantle/blob/2a8e2123a3931038179ee06105c9e6ec336b12ea/LICENSE.md |
| `reactiveobjc-LICENSE.md` | ReactiveObjC at the commit pinned by Electron v44.9.3 | https://github.com/ReactiveCocoa/ReactiveObjC/blob/74ab5baccc6f7202c8ac69a8d1e152c29dc1ea76/LICENSE.md |
| `cpython-license.rst` | CPython 3.13.9 | https://github.com/python/cpython/blob/v3.13.9/Doc/license.rst |

The CPython build is python-build-standalone tag `20251028`, `aarch64-apple-darwin` in the arm64 app and `x86_64-apple-darwin` in the x64 app: https://github.com/astral-sh/python-build-standalone/tree/20251028
