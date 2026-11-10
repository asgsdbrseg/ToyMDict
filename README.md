# ToyMDict
一个MDict玩具，用于查询mdx，mdd。


代码：智谱清言，TRAE，hy3，hy4

参考：
1. https://github.com/raymanzhang/mdict
2. https://bitbucket.org/xwang/mdict-analysis/src
3. https://github.com/KnIfER/PlainDictionaryAPP

特点：
1. 支持异体字关联搜索（可自行修改`variants.json`，重启程序后生效）
2. 强制先分组，后查询
3. 支持多`mdd`文件
4. 支持自定义css（可自行修改`custom.css`，重启程序生效）


缺点：
1. 不支持标记语言
2. 缺少高级功能：如模糊搜索，全文搜索
3. 未考虑中文以外的语言
4. 不支持外挂字体（字体不在`mdd`内，与`mdx`在相同文件夹内）

# 全局快捷键：双击 Ctrl 取词

无论 ToyMDict 在前台还是后台（已最小化），在**任意软件中选中文字**后，
**快速双击 Ctrl 键**，程序就会把选中的文字复制到剪贴板并自动填入搜索框执行搜索，同时把窗口带到前台。

- 该功能**默认常开，不可关闭**。
- 取词原理：在后台向当前聚焦窗口发送复制组合键（Windows/Linux 为 `Ctrl+C`，macOS 为 `Cmd+C`），读取剪贴板后再还原你原来的剪贴板内容，因此不会影响你正在复制的东西。
- 若双击 Ctrl 时没有任何选中文字（剪贴板内容未变化），则不会触发搜索。

## 依赖与平台说明
- 新增依赖：`pynput`（全局键盘监听/模拟）、`pyperclip`（跨平台剪贴板）。
- **Windows**：开箱即用。
- **macOS**：需在「系统设置 → 隐私与安全性 → 辅助功能 / 输入监控」中授予终端或启动器相应权限。
- **Linux**：需在 X11 会话下运行（剪贴板依赖 `xclip` 或 `xsel`，请用 `sudo apt install xclip` 安装）；纯 Wayland 会话可能无法全局监听键盘。