# -*- coding: utf-8 -*-
"""全局快捷键服务：监听系统级「双击 Ctrl」手势，将当前任意软件中
选中的文字复制到剪贴板，并发送到 ToyMDict 搜索框执行搜索。

工作流程（行业通用的「划词取词」做法）：
    1. 在后台（不抢焦点）监听全局键盘事件；
    2. 用户快速双击 Ctrl 键 -> 判定为取词手势；
    3. 向「当前聚焦的其它窗口」发送复制组合键（Ctrl+C / macOS Cmd+C），
       把选中文字送入系统剪贴板；
    4. 读取剪贴板内容，随后还原用户原来的剪贴板；
    5. 把取到的文字写入 ToyMDict 搜索框并触发搜索，同时把窗口带到前台。

依赖（均为可选，缺失时优雅降级，不影响主程序启动）：
    - pynput：全局键盘监听与模拟按键；
    - pyperclip：跨平台剪贴板读写。
"""
import sys
import time
import threading
import json
import traceback


class GlobalHotkeyService:
    def __init__(self, window, enabled=True, interval_ms=400, restore_clipboard=True):
        self.window = window
        self.enabled = bool(enabled)
        # 双击时间间隔阈值（秒），由毫秒换算并做下限保护
        self.interval = max(50, int(interval_ms)) / 1000.0
        self.restore_clipboard = bool(restore_clipboard)

        self._last_ctrl_time = 0.0
        self._lock = threading.Lock()
        self._capturing = False   # 模拟按键期间屏蔽自身监听，防止回环触发
        self._running = False     # 单次取词流程进行中，避免并发重入
        self._ctrl_down = False   # 跟踪 Ctrl 物理按下状态，过滤系统自动重复

        self._listener = None
        self._kb_controller = None
        self._Key = None
        self._pyperclip = None
        self._available = False

        # 窗口状态跟踪：区分「最小化」与「最大化」，避免 restore() 把最大化窗口还原成普通大小
        self._state_lock = threading.Lock()
        self._window_minimized = False
        self._window_maximized = False
        self._state_events_bound = False

    # ==================== 生命周期 ====================
    def start(self):
        """启动全局键盘监听。依赖缺失或不支持时打印警告并禁用。"""
        try:
            from pynput import keyboard
            from pynput.keyboard import Key, Controller, Listener
        except Exception as e:
            print(f"[全局快捷键] 未安装 pynput，双击Ctrl取词功能已禁用: {e}")
            return

        try:
            import pyperclip
        except Exception as e:
            print(f"[全局快捷键] 未安装 pyperclip，取词功能将不可用: {e}")
            pyperclip = None

        self._Key = Key
        self._pyperclip = pyperclip
        self._kb_controller = Controller()
        self._listener = Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.daemon = True
        self._listener.start()
        self._available = True
        self._bind_window_state_events()
        print("[全局快捷键] 已启动：双击 Ctrl 取词（将选中文字发送到搜索框）")

    def stop(self):
        """停止监听并释放资源。"""
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    def set_enabled(self, enabled):
        """运行时开关（供 UI 复选框调用）。"""
        self.enabled = bool(enabled)

    # ==================== 键盘监听 ====================
    def _is_ctrl(self, key):
        return key in (self._Key.ctrl, self._Key.ctrl_l, self._Key.ctrl_r)

    def _on_press(self, key):
        if not self.enabled or not self._available:
            return
        # 模拟复制产生的合成 Ctrl 事件需被忽略，否则会自我触发
        if self._capturing:
            return
        if not self._is_ctrl(key):
            return
        # 过滤系统自动重复：同一物理按键被按住不放时，OS 会持续发送
        # keydown 事件，必须等到按键释放后再次按下才算一次有效「单击」
        if self._ctrl_down:
            return
        self._ctrl_down = True

        now = time.perf_counter()
        trigger = False
        with self._lock:
            if now - self._last_ctrl_time <= self.interval:
                self._last_ctrl_time = 0.0
                trigger = True
            else:
                self._last_ctrl_time = now

        if trigger:
            self._on_double_ctrl()

    def _on_release(self, key):
        # 仅在 Ctrl 松开后才允许下一次单击被计数
        if self._is_ctrl(key):
            self._ctrl_down = False

    def _on_double_ctrl(self):
        with self._lock:
            if self._running:
                return
            self._running = True
        try:
            t = threading.Thread(target=self._capture_and_search, daemon=True)
            t.start()
        except Exception:
            with self._lock:
                self._running = False

    # ==================== 剪贴板 ====================
    def _get_clipboard(self):
        if self._pyperclip is None:
            return None
        try:
            return self._pyperclip.paste()
        except Exception:
            return None

    def _set_clipboard(self, text):
        if self._pyperclip is None or text is None:
            return
        try:
            self._pyperclip.copy(text)
        except Exception:
            pass

    # ==================== 取词与搜索 ====================
    def _copy_selection(self):
        """向当前聚焦窗口发送复制组合键，把选中文字送入剪贴板。

        注意：发送前不要抢焦点，否则会复制到本程序自己的窗口（为空）。
        macOS 的复制修饰键是 Cmd，其余平台是 Ctrl。
        """
        copy_mod = self._Key.cmd if sys.platform == "darwin" else self._Key.ctrl
        self._capturing = True
        try:
            with self._kb_controller.pressed(copy_mod):
                self._kb_controller.press("c")
                self._kb_controller.release("c")
        except Exception as e:
            print(f"[全局快捷键] 模拟复制失败: {e}")
        finally:
            self._capturing = False
        # 等待目标应用把内容写入剪贴板
        time.sleep(0.2)

    def _capture_and_search(self):
        try:
            old = self._get_clipboard()
            self._copy_selection()
            new = self._get_clipboard()

            # 候选文字判定：
            #   优先使用复制动作之后的剪贴板内容（new），它对应「当前选中文字」；
            #   若复制未改变剪贴板（例如用户早已手动 Ctrl+C 过该文字），则回退到
            #   复制前的剪贴板内容（old）。这样「选中后直接双击 Ctrl」与
            #   「先 Ctrl+C 再双击 Ctrl」两种用法都能命中。
            candidate = new if (new and new.strip()) else old
            text = candidate.strip() if (candidate and candidate.strip()) else None

            # 还原用户原来的剪贴板内容
            if self.restore_clipboard and old is not None:
                self._set_clipboard(old)

            if not text:
                return

            # 先把窗口带到前台，再写入搜索框并搜索
            self._bring_to_front()
            try:
                js = "externalSearch(%s)" % json.dumps(text, ensure_ascii=False)
                self.window.evaluate_js(js)
            except Exception as e:
                print(f"[全局快捷键] 写入搜索框失败: {e}")
        except Exception:
            traceback.print_exc()
        finally:
            with self._lock:
                self._running = False

    # ==================== 窗口状态跟踪 ====================
    def _bind_window_state_events(self):
        """注册 pywebview 窗口状态事件，实时跟踪最小化/最大化状态。

        pywebview 的 window.restore() 在 Qt / WinForms 后端会把窗口状态置为
        「普通」（Qt: setWindowState(WindowNoState)，WinForms: WindowState=Normal），
        这会把已最大化的窗口还原成普通大小。因此必须先区分当前是「最小化」还是
        「最大化」，仅在真正最小化时才调用 restore()。
        """
        if self._state_events_bound:
            return
        try:
            events = getattr(self.window, "events", None)
            if events is None:
                return

            def _on_minimized():
                with self._state_lock:
                    self._window_minimized = True
                    self._window_maximized = False

            def _on_maximized():
                with self._state_lock:
                    self._window_maximized = True
                    self._window_minimized = False

            def _on_restored():
                with self._state_lock:
                    self._window_minimized = False
                    self._window_maximized = False

            # pywebview 事件支持 += 注册回调，状态变化时会在独立线程回调
            events.minimized += _on_minimized
            events.maximized += _on_maximized
            events.restored += _on_restored
            self._state_events_bound = True
        except Exception as e:
            print(f"[全局快捷键] 绑定窗口状态事件失败: {e}")

    def _is_window_minimized(self):
        """判断窗口是否处于最小化状态。

        优先使用事件跟踪的状态；若事件未绑定或不可靠，则回退到通过原生窗口
        对象直接查询（跨平台）。
        """
        if not self._state_events_bound:
            return self._query_native_minimized()
        with self._state_lock:
            return self._window_minimized

    def _query_native_minimized(self):
        """通过原生窗口对象跨平台查询最小化状态（事件未绑定时的回退方案）。"""
        native = getattr(self.window, "native", None)
        if native is None:
            return False
        try:
            # Qt：QWidget.isMinimized()
            if hasattr(native, "isMinimized"):
                return bool(native.isMinimized())
            # Cocoa：NSWindow.isMiniaturized
            if hasattr(native, "isMiniaturized"):
                return bool(native.isMiniaturized)
            # WinForms：Form.WindowState == Minimized(2)
            state = getattr(native, "WindowState", None)
            if state is not None:
                return int(state) == 2
            # GTK：Gdk.WindowState.ICONIFIED(2)
            get_state = getattr(native, "get_state", None)
            if callable(get_state):
                return bool(get_state() & 2)
        except Exception:
            pass
        return False

    def _bring_to_front(self):
        """把主窗口带到前台。

        关键修复：仅当窗口真正处于「最小化」状态时才调用 restore()。
        否则（正常/最大化）restore() 会把最大化窗口还原成普通大小，
        导致取词后窗口尺寸被意外改变。
        """
        if self._is_window_minimized():
            # 最小化：restore() 会取消最小化并置顶
            try:
                self.window.restore()
            except Exception:
                pass
        else:
            # 非最小化（正常或最大化）：仅 show 并置顶，避免破坏最大化状态
            try:
                self.window.show()
            except Exception:
                pass
            self._raise_native_window()

    def _raise_native_window(self):
        """将非最小化窗口置顶并激活（不改变窗口状态）。

        pywebview 的 show() 在 Qt 后端仅调用 QWidget.show()，对已可见窗口
        不会置顶，因此需要直接调用原生方法。为线程安全起见，仅做最佳努力。
        """
        native = getattr(self.window, "native", None)
        if native is None:
            return
        try:
            # Qt：raise_() + activateWindow() 置顶并激活
            if hasattr(native, "raise_") and hasattr(native, "activateWindow"):
                native.raise_()
                native.activateWindow()
                return
            # WinForms：Activate + BringToFront
            if hasattr(native, "Activate"):
                native.Activate()
            if hasattr(native, "BringToFront"):
                native.BringToFront()
        except Exception:
            pass
