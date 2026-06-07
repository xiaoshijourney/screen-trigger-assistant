# -*- coding: utf-8 -*-
"""
=========================

功能:
  1. 框选屏幕上一个区域作为监控目标（比如微信群的聊天区域）
  2. 持续监控该区域的画面变化
  3. 当检测到画面变化（新消息出现），自动切换到微信窗口
  4. 将预设的接龙内容粘贴到聊天框并回车发送

依赖库 (pip install):
  pip install mss numpy Pillow pyautogui pygetwindow pyperclip pywin32 keyboard

运行方式:
  python screen-trigger-assistant.py
"""

import json
import os
import time
import threading
import ctypes
from datetime import datetime

import mss
import numpy as np
import pyautogui
import pyperclip
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

# ---------------------------------------------------------------------------
# Windows API (用于可靠的窗口激活)
# ---------------------------------------------------------------------------
try:
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

# ---------------------------------------------------------------------------
# 全局热键 (可选，用于紧急停止)
# ---------------------------------------------------------------------------
try:
    import keyboard
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False

# ---------------------------------------------------------------------------
# DPI 感知设置 (避免高 DPI 下坐标偏移)
# ---------------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PerMonitorV2
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ===========================================================================
#                              配置管理
# ===========================================================================

CONFIG_FILE = "screen-trigger-assistant.json"

DEFAULT_CONFIG = {
    "monitor_region": None,          # [left, top, width, height]
    "click_position": None,         # [x, y] 动作执行前鼠标点击的位置
    "message": "",                   # 预设的发送内容
    "sensitivity": 0.03,            # 灵敏度: 变化像素占比阈值 (0.0~1.0)
    "cooldown_seconds": 60.0,       # 触发后的冷却时间(秒)
    "check_interval": 0.2,          # 监控检测间隔(秒)
    # ---- 触发模式 ----
    "trigger_mode": "change",        # "change"=画面变动触发  "ocr"=文字识别触发
    "trigger_keywords": "",          # 触发关键词（逗号分隔，任一匹配即触发）
    "block_keywords": "",            # 屏蔽关键词（逗号分隔，任一匹配则不触发）
    # ---- 执行动作 ----
    "click_enabled": False,          # 是否执行鼠标点击
    "action_enabled": True,          # 是否执行发送消息
}


def load_config():
    """从文件加载配置，不存在则返回默认值"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # 迁移旧版配置项
            if "wechat_window_keyword" in cfg:
                del cfg["wechat_window_keyword"]
            # 旧版双复选框 → 新版下拉框
            old_ocr = cfg.pop("ocr_trigger_enabled", cfg.pop("ocr_enabled", None))
            _ = cfg.pop("change_trigger_enabled", None)
            if old_ocr is not None:
                cfg["trigger_mode"] = "ocr" if old_ocr else "change"
            # 合并默认值，避免新增字段缺失
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    """保存配置到文件"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[警告] 配置保存失败: {e}")


# ===========================================================================
#                          区域框选器
# ===========================================================================

class RegionSelector:
    """
    全屏半透明遮罩，用户拖拽鼠标来框选监控区域。
    使用 Toplevel 依附主窗口，避免双 Tk 实例导致 wm 崩溃。
    用法:
        selector = RegionSelector(parent_root)
        region = selector.run()   # 返回 (left, top, width, height) 或 None
    """

    def __init__(self, parent, initial_region=None):
        self.parent = parent          # 主窗口的 Tk 根对象
        self.initial_region = initial_region
        self.result = None

    def run(self):
        """启动选区界面，阻塞直到用户选好或取消"""

        # 获取屏幕尺寸用于全屏
        screen_w = self.parent.winfo_screenwidth()
        screen_h = self.parent.winfo_screenheight()

        top = tk.Toplevel(self.parent)
        top.title("框选检测区域")
        top.geometry(f"{screen_w}x{screen_h}+0+0")
        top.attributes("-alpha", 0.35)
        top.attributes("-topmost", True)
        top.overrideredirect(True)     # 去掉标题栏，实现真正全屏
        top.configure(bg="gray20")
        top.config(cursor="cross")

        # 提示文字
        lb = tk.Label(
            top,
            text="拖拽鼠标框选检测区域（聊天区域），按 ESC 取消",
            font=("Microsoft YaHei", 18, "bold"),
            fg="white",
            bg="gray20",
        )
        lb.place(relx=0.5, rely=0.05, anchor="n")

        canvas = tk.Canvas(top, bg="gray20", highlightthickness=0)
        canvas.place(x=0, y=0, relwidth=1, relheight=1)

        start_x = tk.IntVar()
        start_y = tk.IntVar()

        def on_press(event):
            start_x.set(event.x_root)
            start_y.set(event.y_root)
            canvas.delete("selection_rect")

        def on_drag(event):
            canvas.delete("selection_rect")
            x1 = min(start_x.get(), event.x_root)
            y1 = min(start_y.get(), event.y_root)
            x2 = max(start_x.get(), event.x_root)
            y2 = max(start_y.get(), event.y_root)
            canvas.create_rectangle(
                x1, y1, x2, y2,
                outline="#00FF00", width=3, dash=(8, 4),
                tags="selection_rect",
            )
            w = x2 - x1
            h = y2 - y1
            canvas.delete("size_text")
            canvas.create_text(
                (x1 + x2) // 2, y1 - 20 if y1 > 40 else y2 + 20,
                text=f"{w} × {h}",
                fill="#00FF00",
                font=("Microsoft YaHei", 14, "bold"),
                tags="size_text",
            )

        def on_release(event):
            x1 = min(start_x.get(), event.x_root)
            y1 = min(start_y.get(), event.y_root)
            x2 = max(start_x.get(), event.x_root)
            y2 = max(start_y.get(), event.y_root)
            w = x2 - x1
            h = y2 - y1
            if w < 10 or h < 10:
                messagebox.showwarning("区域太小", "请框选一个更大的区域（至少 10×10 像素）", parent=top)
                return
            self.result = (x1, y1, w, h)
            top.destroy()

        def on_escape(event):
            self.result = None
            top.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        top.bind("<Escape>", on_escape)

        # 如果有上次保存的区域，绘制提示框
        if self.initial_region:
            l, t, w, h = self.initial_region
            r = l + w
            b = t + h
            canvas.create_rectangle(
                l, t, r, b,
                outline="#FFD700", width=3, dash=(8, 4),
                tags="selection_rect",
            )
            canvas.create_text(
                (l + r) // 2, t - 20 if t > 40 else b + 20,
                text=f"上次区域: {w}×{h}  (可重新框选)",
                fill="#FFD700",
                font=("Microsoft YaHei", 12),
                tags="size_text",
            )

        # 将焦点设到 Toplevel，确保按键事件生效
        top.focus_force()
        top.grab_set()  # 模态：阻塞主窗口操作

        # 阻塞等待 Toplevel 关闭
        self.parent.wait_window(top)
        return self.result


# ===========================================================================
#                          点击位置选择器
# ===========================================================================

class PointSelector:
    """
    全屏半透明遮罩，用户单击鼠标来选择一个坐标点。
    用法:
        selector = PointSelector(parent_root, initial_point=(x,y))
        point = selector.run()   # 返回 (x, y) 或 None
    """

    def __init__(self, parent, initial_point=None):
        self.parent = parent
        self.initial_point = initial_point
        self.result = None

    def run(self):
        """启动点击选点界面，阻塞直到用户点击或按 ESC 取消"""
        screen_w = self.parent.winfo_screenwidth()
        screen_h = self.parent.winfo_screenheight()

        top = tk.Toplevel(self.parent)
        top.title("选择点击位置")
        top.geometry(f"{screen_w}x{screen_h}+0+0")
        top.attributes("-alpha", 0.3)
        top.attributes("-topmost", True)
        top.overrideredirect(True)
        top.configure(bg="gray20")
        top.config(cursor="crosshair")

        # 提示文字
        tk.Label(
            top,
            text="单击鼠标选择发送前要点击的位置（如输入框），按 ESC 取消",
            font=("Microsoft YaHei", 18, "bold"),
            fg="white",
            bg="gray20",
        ).place(relx=0.5, rely=0.05, anchor="n")

        canvas = tk.Canvas(top, bg="gray20", highlightthickness=0)
        canvas.place(x=0, y=0, relwidth=1, relheight=1)

        # 画十字准星
        def draw_crosshair(x, y):
            canvas.delete("all")
            canvas.create_line(x, 0, x, screen_h, fill="#00FF00", width=1, dash=(4, 4))
            canvas.create_line(0, y, screen_w, y, fill="#00FF00", width=1, dash=(4, 4))
            canvas.create_oval(x - 12, y - 12, x + 12, y + 12, outline="#00FF00", width=2)
            canvas.create_text(
                x, y - 28 if y > 60 else y + 28,
                text=f"({x}, {y})",
                fill="#00FF00",
                font=("Microsoft YaHei", 14, "bold"),
                tags="coord_text",
            )

        def on_click(event):
            self.result = (event.x_root, event.y_root)
            top.destroy()

        def on_move(event):
            draw_crosshair(event.x_root, event.y_root)

        def on_escape(event):
            self.result = None
            top.destroy()

        canvas.bind("<Motion>", on_move)
        canvas.bind("<Button-1>", on_click)
        top.bind("<Escape>", on_escape)

        # 如果之前有保存的位置，画个小标记
        if self.initial_point:
            px, py = self.initial_point
            canvas.create_oval(
                px - 8, py - 8, px + 8, py + 8,
                outline="#FFD700", width=2,
            )
            canvas.create_text(
                px, py - 22 if py > 50 else py + 22,
                text=f"上次位置: ({px}, {py})",
                fill="#FFD700",
                font=("Microsoft YaHei", 11),
            )

        top.focus_force()
        top.grab_set()
        self.parent.wait_window(top)
        return self.result


# ===========================================================================
#                          动作执行器
# ===========================================================================

class ActionExecutor:
    """
    动作执行器：纯键盘鼠标操作，不依赖特定窗口。
    支持：鼠标点击 + 粘贴文本并回车发送
    """

    def execute(self, message="", click_pos=None):
        """
        执行动作：
          1. 如果指定了 click_pos，先鼠标点击该位置
          2. 如果有消息内容，粘贴并回车发送
        返回: (success: bool, info: str)
        """
        try:
            if click_pos:
                pyautogui.click(click_pos[0], click_pos[1])
                time.sleep(0.15)

            if message:
                try:
                    original_clipboard = pyperclip.paste()
                except Exception:
                    original_clipboard = ""

                try:
                    pyperclip.copy(message)
                    time.sleep(0.1)
                    pyautogui.hotkey("ctrl", "v")
                    time.sleep(0.1)

                    if HAS_WIN32:
                        import win32api
                        win32api.keybd_event(0x0D, 0, 0, 0)
                        time.sleep(0.05)
                        win32api.keybd_event(0x0D, 0, win32con.KEYEVENTF_KEYUP, 0)
                        time.sleep(0.1)
                    else:
                        pyautogui.press("enter")
                        time.sleep(0.1)
                finally:
                    try:
                        pyperclip.copy(original_clipboard)
                    except Exception:
                        pass

            return True, "动作已执行"
        except Exception as e:
            return False, str(e)



# ===========================================================================
#                          OCR 文字识别引擎
# ===========================================================================

class OcrEngine:
    """
    封装 easyocr，实现截图文字识别。
    延迟加载模型（首次调用时才初始化），避免启动卡顿。
    """

    def __init__(self):
        self._reader = None
        self._loaded = False

    def _ensure_loaded(self):
        if not self._loaded:
            import easyocr
            self._reader = easyocr.Reader(["ch_sim", "en"], gpu=False)
            self._loaded = True

    def recognize(self, frame_rgb):
        """
        识别图片中的文字
        frame_rgb: numpy 数组 (H, W, 3), RGB 格式
        返回: 识别到的文本（用换行拼接），识别失败返回 ""
        """
        try:
            self._ensure_loaded()
            results = self._reader.readtext(frame_rgb, detail=0)
            return "\n".join(results)
        except Exception as e:
            print(f"[OCR 错误] {e}")
            return ""

    def check_keywords(self, text, trigger_kw, block_kw):
        """
        检查文字是否满足触发条件
        trigger_kw: 逗号分隔的关键词，任一匹配即可
        block_kw:   逗号分隔的关键词，任一匹配则屏蔽
        返回: (should_trigger: bool, matched: str)
        """
        if not text.strip():
            return False, ""

        # 解析关键词
        triggers = [k.strip() for k in trigger_kw.split(",") if k.strip()]
        blocks = [k.strip() for k in block_kw.split(",") if k.strip()]

        # 检查屏蔽词（优先，一旦匹配就阻止触发）
        for kw in blocks:
            if kw in text:
                return False, f"屏蔽词[{kw}]"

        # 没有触发词 → 任意文字都触发
        if not triggers:
            return True, "任意文字"

        # 检查触发词
        for kw in triggers:
            if kw in text:
                return True, f"触发词[{kw}]"

        return False, ""


# ===========================================================================
#                          屏幕监控器 (OCR 增强版)
# ===========================================================================

class ScreenMonitor:
    """
    持续截取指定区域并与上一帧比较，检测画面是否发生变化。
    可选：检测到变化后执行 OCR 文字识别 + 关键词匹配。
    """

    def __init__(self, region, sensitivity=0.03, cooldown=5.0, interval=0.3,
                 ocr_trigger_enabled=False, trigger_keywords="", block_keywords=""):
        """
        region: (left, top, width, height)
        sensitivity: 变化像素占比阈值
        cooldown: 触发后冷却时间(秒)
        interval: 检测间隔(秒)
        ocr_trigger_enabled: 是否启用 OCR 文字识别触发（否则仅画面变动）
        trigger_keywords: 触发关键词（逗号分隔）
        block_keywords: 屏蔽关键词（逗号分隔）
        """
        self.region = region
        self.sensitivity = sensitivity
        self.cooldown = cooldown
        self.interval = interval
        self.ocr_trigger_enabled = ocr_trigger_enabled
        self.trigger_keywords = trigger_keywords
        self.block_keywords = block_keywords
        self.last_frame = None
        self.last_trigger_time = 0
        self.running = False
        self._on_change_callback = None
        self._ocr = OcrEngine() if ocr_trigger_enabled else None

    def set_on_change(self, callback):
        """设置画面变化时的回调函数"""
        self._on_change_callback = callback

    def _capture_region(self, sct, left, top, width, height):
        """截取指定区域，返回 numpy 数组 (height, width, 3)"""
        monitor = {"left": left, "top": top, "width": width, "height": height}
        img = sct.grab(monitor)
        # mss 返回 BGRA，取 BGR 转为 RGB
        arr = np.array(img, dtype=np.uint8)
        return arr[:, :, :3]  # 去掉 alpha 通道

    def _detect_change(self, current):
        """比较当前帧与上一帧，返回变化像素占比"""
        if self.last_frame is None:
            return 0.0
        if current.shape != self.last_frame.shape:
            return 1.0
        # 计算像素差异
        diff = np.abs(current.astype(np.int16) - self.last_frame.astype(np.int16))
        # 任一通道差异超过阈值即认为变化
        threshold = 30
        changed = np.any(diff > threshold, axis=2)
        ratio = np.mean(changed)
        return float(ratio)

    def start(self):
        """启动监控（在后台线程中运行）"""
        self.running = True
        self.last_frame = None
        self.last_trigger_time = 0

        def _loop():
            with mss.MSS() as sct:
                left, top, width, height = self.region
                while self.running:
                    try:
                        current = self._capture_region(sct, left, top, width, height)
                        change_ratio = self._detect_change(current)

                        # 首次运行，只记录帧，不触发
                        if self.last_frame is not None:
                            now = time.time()
                            if (
                                change_ratio >= self.sensitivity
                                and (now - self.last_trigger_time) >= self.cooldown
                            ):
                                # ---- OCR 关键词检查 ----
                                ocr_text = ""
                                matched_info = ""
                                should_trigger = True

                                if self.ocr_trigger_enabled and self._ocr:
                                    ocr_text = self._ocr.recognize(current)
                                    should_trigger, matched_info = self._ocr.check_keywords(
                                        ocr_text, self.trigger_keywords, self.block_keywords
                                    )
                                    if should_trigger and self._on_change_callback:
                                        self.last_trigger_time = now
                                        self._on_change_callback(change_ratio, ocr_text, matched_info)
                                    elif not should_trigger and self._on_change_callback:
                                        self._on_change_callback(change_ratio, ocr_text,
                                                                 f"忽略: {matched_info}")
                                elif self._on_change_callback:
                                    self.last_trigger_time = now
                                    self._on_change_callback(change_ratio, "", "")

                        self.last_frame = current.copy()
                    except Exception as e:
                        print(f"[检测错误] {e}")

                    time.sleep(self.interval)

        thread = threading.Thread(target=_loop, daemon=True)
        thread.start()

    def stop(self):
        """停止监控"""
        self.running = False

    def update_config(self, region=None, sensitivity=None, cooldown=None, interval=None,
                      ocr_trigger_enabled=None, trigger_keywords=None, block_keywords=None):
        """动态更新监控参数"""
        if region is not None:
            self.region = region
        if sensitivity is not None:
            self.sensitivity = sensitivity
        if cooldown is not None:
            self.cooldown = cooldown
        if interval is not None:
            self.interval = interval
        if ocr_trigger_enabled is not None:
            self.ocr_trigger_enabled = ocr_trigger_enabled
            self._ocr = OcrEngine() if ocr_trigger_enabled else None
        if trigger_keywords is not None:
            self.trigger_keywords = trigger_keywords
        if block_keywords is not None:
            self.block_keywords = block_keywords
        # 重置帧缓存以重新适配
        self.last_frame = None


# ===========================================================================
#                          主控制面板
# ===========================================================================

class ControlPanel:
    """程序主界面"""

    def __init__(self):
        self.config = load_config()
        self.monitor = None
        self.executor = ActionExecutor()
        self.running = False

        self._build_ui()
        self._update_status_display()
        self._register_hotkey()

    # ---- UI 构建 ----

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("灵犀助手")
        self.root.geometry("920x880")
        self.root.minsize(780, 700)
        self.root.configure(bg="#f3f3f3")

        # ---- 窗口图标 ----
        try:
            logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
            if os.path.exists(logo_path):
                logo_img = tk.PhotoImage(file=logo_path)
                self.root.iconphoto(True, logo_img)
                self._logo_img = logo_img  # 保持引用防止被 GC
        except Exception:
            pass

        # ---- ttk 风格 ----
        style = ttk.Style()
        style.theme_use("vista")
        style.configure(".", font=("Microsoft YaHei", 9, "bold"))
        style.configure("Card.TCheckbutton", font=("Microsoft YaHei", 10, "bold"))
        style.configure("Card.TSpinbox", font=("Microsoft YaHei", 10, "bold"), padding=3)
        style.configure("Card.TEntry", font=("Microsoft YaHei", 10, "bold"), padding=4)
        style.configure("Primary.TButton", font=("Microsoft YaHei", 10, "bold"), padding=(18, 5))
        style.configure("Success.TButton", font=("Microsoft YaHei", 11, "bold"), padding=(12, 6))
        style.configure("Danger.TButton", font=("Microsoft YaHei", 11, "bold"), padding=(12, 6))
        style.configure("Secondary.TButton", font=("Microsoft YaHei", 9), padding=(12, 4))

        # ---- 主内容区: 左设置 + 右日志 ----
        main = tk.Frame(self.root, bg="#f3f3f3")
        main.pack(fill="both", expand=True, padx=16, pady=(16, 8))

        # ======== 左侧面板（可滚动）========
        left_container = tk.Frame(main, bg="#f3f3f3", width=420)
        left_container.pack(side="left", fill="y")
        left_container.pack_propagate(False)

        left_canvas = tk.Canvas(left_container, bg="#f3f3f3", highlightthickness=0)
        left_scrollbar = tk.Scrollbar(left_container, orient="vertical", command=left_canvas.yview)
        left_scrollbar.pack(side="right", fill="y")
        left_canvas.pack(side="left", fill="both", expand=True)
        left_canvas.configure(yscrollcommand=left_scrollbar.set)

        left = tk.Frame(left_canvas, bg="#f3f3f3")
        self._left_window_id = left_canvas.create_window((0, 0), window=left, anchor="nw")

        def _on_left_configure(event):
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))
        def _on_canvas_configure(event):
            left_canvas.itemconfig(self._left_window_id, width=event.width)

        left.bind("<Configure>", _on_left_configure)
        left_canvas.bind("<Configure>", _on_canvas_configure)

        # 鼠标滚轮滚动
        def _on_mousewheel(event):
            left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        def _bind_scroll(e):
            left_canvas.bind_all("<MouseWheel>", _on_mousewheel)
        def _unbind_scroll(e):
            left_canvas.unbind_all("<MouseWheel>")
        left_canvas.bind("<Enter>", _bind_scroll)
        left_canvas.bind("<Leave>", _unbind_scroll)

        # ---- 卡片工厂（微软商店风格）----
        def _card(parent, title, emoji=""):
            card = tk.Frame(parent, bg="white",
                            highlightbackground="#e4e4e4", highlightthickness=1)
            card.pack(fill="x", pady=(0, 10))

            hdr = tk.Frame(card, bg="#fafafa", height=34)
            hdr.pack(fill="x")
            hdr.pack_propagate(False)
            tk.Label(
                hdr,
                text=f"  {emoji}  {title}" if emoji else f"  {title}",
                font=("Microsoft YaHei", 10, "bold"),
                fg="#333333", bg="#fafafa", anchor="w",
            ).pack(side="left", padx=10, pady=3)

            tk.Frame(card, bg="#eeeeee", height=1).pack(fill="x")
            body = tk.Frame(card, bg="white")
            body.pack(fill="x", padx=16, pady=(10, 12))
            return body

        # ----- 监控区域 -----
        b1 = _card(left, "监控区域", "📌")
        self.region_label = tk.Label(
            b1, text="未设置",
            font=("Microsoft YaHei", 10, "bold"), fg="#E81123",
            bg="white", anchor="w",
        )
        self.region_label.pack(fill="x", pady=(0, 8))
        self.select_btn = ttk.Button(
            b1, text="  框选监控区域  ", command=self._on_select_region,
            style="Primary.TButton",
        )
        self.select_btn.pack()

        # ----- 触发条件 -----
        b2 = _card(left, "触发条件", "🎯")

        # 触发模式下拉框（显示中文，存英文值）
        self._mode_display = {"change": "画面变动触发", "ocr": "文字识别触发"}
        self._mode_value = {v: k for k, v in self._mode_display.items()}
        mode_frame = tk.Frame(b2, bg="white")
        mode_frame.pack(fill="x", pady=(0, 4))
        tk.Label(mode_frame, text="触发方式",
                 bg="white", fg="#555",
                 font=("Microsoft YaHei", 9, "bold")).pack(anchor="w")
        self.trigger_mode_var = tk.StringVar()
        mode_key = self.config.get("trigger_mode", "change")
        self.trigger_mode_var.set(self._mode_display[mode_key])
        self.trigger_mode_combo = ttk.Combobox(
            mode_frame,
            textvariable=self.trigger_mode_var,
            values=list(self._mode_display.values()),
            state="readonly",
            font=("Microsoft YaHei", 10, "bold"),
        )
        self.trigger_mode_combo.pack(fill="x", ipady=2, pady=(2, 0))
        self.trigger_mode_combo.bind("<<ComboboxSelected>>", self._on_trigger_mode_change)

        # 模式说明标签
        self.mode_desc = tk.Label(
            b2, text="",
            bg="white", fg="#888",
            font=("Microsoft YaHei", 9, "bold"), anchor="w",
        )
        self.mode_desc.pack(fill="x")

        # OCR 参数区域（选 "ocr" 时才显示）
        self.ocr_frame = tk.Frame(b2, bg="white")
        tk.Label(self.ocr_frame, text="触发关键词（逗号分隔）",
                 bg="white", fg="#555",
                 font=("Microsoft YaHei", 9, "bold")).pack(anchor="w", pady=(6, 0))
        self.trigger_kw_entry = ttk.Entry(
            self.ocr_frame, style="Card.TEntry",
        )
        self.trigger_kw_entry.insert(0, self.config.get("trigger_keywords", ""))
        self.trigger_kw_entry.pack(fill="x", pady=(2, 6))
        tk.Label(self.ocr_frame, text="屏蔽关键词（逗号分隔）",
                 bg="white", fg="#555",
                 font=("Microsoft YaHei", 9, "bold")).pack(anchor="w")
        self.block_kw_entry = ttk.Entry(
            self.ocr_frame, style="Card.TEntry",
        )
        self.block_kw_entry.insert(0, self.config.get("block_keywords", ""))
        self.block_kw_entry.pack(fill="x", pady=(2, 0))

        # ----- 执行动作 -----
        b3 = _card(left, "执行动作", "⚡")

        # 鼠标点击开关
        self.click_var = tk.BooleanVar(
            value=self.config.get("click_enabled", False)
        )
        self.click_cb = ttk.Checkbutton(
            b3, text="鼠标点击（执行前先点击指定位置）", variable=self.click_var,
            style="Card.TCheckbutton",
            command=self._on_click_toggle,
        )
        self.click_cb.pack(fill="x", pady=(0, 4))

        # 点击位置区域（勾选后才显示）
        self.click_frame = tk.Frame(b3, bg="white")
        self.click_label = tk.Label(
            self.click_frame, text="未设置",
            font=("Microsoft YaHei", 10, "bold"), fg="#999",
            bg="white", anchor="w",
        )
        self.click_label.pack(fill="x", pady=(2, 6))
        row = tk.Frame(self.click_frame, bg="white")
        row.pack()
        self.click_select_btn = ttk.Button(
            row, text="选择位置", command=self._on_select_click,
            style="Primary.TButton",
        )
        self.click_select_btn.pack(side="left", padx=2)
        self.click_clear_btn = ttk.Button(
            row, text="清除", command=self._on_clear_click,
            style="Secondary.TButton",
        )
        self.click_clear_btn.pack(side="left", padx=2)

        # 发送消息开关
        self.action_var = tk.BooleanVar(
            value=self.config.get("action_enabled", True)
        )
        self.action_cb = ttk.Checkbutton(
            b3, text="发送消息（粘贴内容并回车）", variable=self.action_var,
            style="Card.TCheckbutton",
            command=self._on_action_toggle,
        )
        self.action_cb.pack(fill="x", pady=(8, 4))

        # 消息内容区域（勾选后才显示）
        self.msg_frame = tk.Frame(b3, bg="white")
        self.msg_entry = ttk.Entry(self.msg_frame, style="Card.TEntry")
        self.msg_entry.insert(0, self.config.get("message", ""))
        self.msg_entry.pack(fill="x")

        # ----- 检测参数 -----
        b4 = _card(left, "检测参数", "⚙️")

        # 灵敏度标题行（标题 + 实时数值）
        sens_row = tk.Frame(b4, bg="white")
        sens_row.pack(fill="x")
        tk.Label(sens_row, text="灵敏度（变化像素占比）",
                 bg="white", fg="#555",
                 font=("Microsoft YaHei", 9, "bold")).pack(side="left")
        self.sensitivity_value_label = tk.Label(
            sens_row, text=f"{self.config.get('sensitivity', 0.03):.3f}",
            bg="white", fg="#0078D4",
            font=("Microsoft YaHei", 13, "bold"),
        )
        self.sensitivity_value_label.pack(side="right")

        self.sensitivity_var = tk.DoubleVar(value=self.config.get("sensitivity", 0.03))

        def _on_slider_change(val):
            self.sensitivity_value_label.config(text=f"{float(val):.3f}")

        ttk.Scale(
            b4, from_=0.005, to=0.3,
            orient="horizontal", variable=self.sensitivity_var,
            command=_on_slider_change,
        ).pack(fill="x", pady=(2, 0))
        tk.Label(b4, text="数值越小越敏感",
                 bg="white", fg="#b0b0b0",
                 font=("Microsoft YaHei", 8)).pack(anchor="w")

        # 冷却 & 间隔
        pr = tk.Frame(b4, bg="white")
        pr.pack(fill="x", pady=(8, 0))

        cl = tk.Frame(pr, bg="white")
        cl.pack(side="left", fill="x", expand=True)
        tk.Label(cl, text="冷却（秒）",
                 bg="white", fg="#555",
                 font=("Microsoft YaHei", 9, "bold")).pack(anchor="w")
        self.cooldown_var = tk.DoubleVar(value=self.config.get("cooldown_seconds", 60.0))
        ttk.Spinbox(
            cl, from_=0.5, to=300, increment=5.0,
            textvariable=self.cooldown_var,
            font=("Microsoft YaHei", 11, "bold"), width=8,
        ).pack(anchor="w", pady=(2, 0))

        cr = tk.Frame(pr, bg="white")
        cr.pack(side="right", fill="x", expand=True)
        tk.Label(cr, text="间隔（秒）",
                 bg="white", fg="#555",
                 font=("Microsoft YaHei", 9, "bold")).pack(anchor="w")
        self.interval_var = tk.DoubleVar(value=self.config.get("check_interval", 0.2))
        ttk.Spinbox(
            cr, from_=0.1, to=5.0, increment=0.1,
            textvariable=self.interval_var,
            font=("Microsoft YaHei", 11, "bold"), width=8,
        ).pack(anchor="w", pady=(2, 0))

        # ----- 控制按钮 -----
        ctrl = tk.Frame(left, bg="#f3f3f3")
        ctrl.pack(fill="x", pady=(10, 6))

        self.start_btn = ttk.Button(
            ctrl, text="▶  开始检测", command=self._on_start,
            style="Success.TButton", width=14,
        )
        self.start_btn.pack(side="left", padx=2)

        self.stop_btn = ttk.Button(
            ctrl, text="■  停止检测", command=self._on_stop,
            style="Danger.TButton", width=14,
        )
        self.stop_btn.state(["disabled"])
        self.stop_btn.pack(side="left", padx=2)

        # ======== 右侧面板: 日志 ========
        right = tk.Frame(main, bg="#f3f3f3")
        right.pack(side="right", fill="both", expand=True, padx=(12, 0))

        log_card = tk.Frame(right, bg="white",
                            highlightbackground="#e4e4e4", highlightthickness=1)
        log_card.pack(fill="both", expand=True)

        log_hdr = tk.Frame(log_card, bg="#fafafa", height=34)
        log_hdr.pack(fill="x")
        log_hdr.pack_propagate(False)
        tk.Label(
            log_hdr,
            text="  📋  运行日志",
            font=("Microsoft YaHei", 10, "bold"),
            fg="#333333", bg="#fafafa", anchor="w",
        ).pack(side="left", padx=10)

        tk.Frame(log_card, bg="#eeeeee", height=1).pack(fill="x")

        log_body = tk.Frame(log_card, bg="white")
        log_body.pack(fill="both", expand=True, padx=8, pady=8)

        self.log_text = scrolledtext.ScrolledText(
            log_body, font=("Consolas", 10),
            bg="#1e1e2e", fg="#cdd6f4",
            relief="flat", borderwidth=0,
            insertbackground="#cdd6f4",
            highlightthickness=1, highlightbackground="#e8e8e8",
        )
        self.log_text.pack(fill="both", expand=True)

        # ======== 状态栏 ========
        bar = tk.Frame(self.root, bg="#fafafa", height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        tk.Frame(bar, bg="#e0e0e0", height=1).pack(fill="x")

        self.status_var = tk.StringVar(value="●  就绪 — 请先框选监控区域")
        tk.Label(
            bar, textvariable=self.status_var,
            font=("Microsoft YaHei", 9, "bold"),
            fg="#616161", bg="#fafafa",
            anchor="w",
        ).pack(fill="x", side="left", padx=14, pady=2)

        # ---- 初始显隐状态 ----
        self._update_mode_desc()
        self._update_ocr_ui_state()
        self._update_click_ui_state()
        self._update_action_ui_state()

    # ---- 日志 ----

    def _log(self, msg):
        """写入日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {msg}\n")
        self.log_text.see("end")

    def _set_status(self, text):
        self.status_var.set(text)

    def _update_status_display(self):
        """更新区域和点击位置显示"""
        region = self.config.get("monitor_region")
        if region:
            l, t, w, h = region
            self.region_label.config(
                text=f"左={l},  顶={t},  宽={w},  高={h}",
                fg="#27ae60",
            )
        else:
            self.region_label.config(text="未设置", fg="#e74c3c")

        click_pos = self.config.get("click_position")
        if click_pos:
            x, y = click_pos
            self.click_label.config(
                text=f"X={x},  Y={y}",
                fg="#27ae60",
            )
        else:
            self.click_label.config(
                text="未设置",
                fg="#888",
            )

    # ---- 触发方式显隐 ----

    def _on_trigger_mode_change(self, event=None):
        """下拉框切换 -> 更新说明、显隐关键词输入框"""
        display = self.trigger_mode_var.get()
        mode_key = self._mode_value.get(display, "change")
        self._update_mode_desc()
        self._update_ocr_ui_state()
        self.config["trigger_mode"] = mode_key
        save_config(self.config)

    def _update_mode_desc(self):
        """更新触发方式说明文字"""
        display = self.trigger_mode_var.get()
        if self._mode_value.get(display) == "ocr":
            self.mode_desc.config(text="画面变化后 OCR 识别，关键词匹配才触发",
                                  fg="#0067c0")
        else:
            self.mode_desc.config(text="画面有变化即触发",
                                  fg="#888")

    def _update_ocr_ui_state(self):
        """选择 OCR 模式时显示关键词输入框，否则隐藏"""
        display = self.trigger_mode_var.get()
        if self._mode_value.get(display) == "ocr":
            self.ocr_frame.pack(fill="x", pady=(2, 0))
        else:
            self.ocr_frame.pack_forget()

    def _on_click_toggle(self):
        """鼠标点击开关 -> 显示/隐藏点击位置控件"""
        self._update_click_ui_state()
        self.config["click_enabled"] = self.click_var.get()
        save_config(self.config)

    def _update_click_ui_state(self):
        """勾选鼠标点击时显示位置控件，否则隐藏"""
        if self.click_var.get():
            self.click_frame.pack(fill="x", pady=(4, 0))
        else:
            self.click_frame.pack_forget()

    def _on_action_toggle(self):
        """发送消息开关 -> 显示/隐藏消息输入框"""
        self._update_action_ui_state()
        self.config["action_enabled"] = self.action_var.get()
        save_config(self.config)

    def _update_action_ui_state(self):
        """勾选发送消息时显示输入框，否则隐藏"""
        if self.action_var.get():
            self.msg_frame.pack(fill="x", pady=(4, 0))
        else:
            self.msg_frame.pack_forget()

    # ---- 点击位置 ----

    def _on_select_click(self):
        """打开点击位置选择界面"""
        if self.running:
            messagebox.showwarning("请先停止", "请先停止检测再选择点击位置")
            return

        self.root.withdraw()
        time.sleep(0.15)

        try:
            initial = self.config.get("click_position")
            selector = PointSelector(self.root, initial_point=initial)
            point = selector.run()
        finally:
            try:
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            except Exception:
                pass

        if point:
            self.config["click_position"] = point
            save_config(self.config)
            self._update_status_display()
            self._log(f"点击位置已设置: ({point[0]}, {point[1]})")
        else:
            self._log("点击位置选择已取消")

    def _on_clear_click(self):
        """清除点击位置"""
        self.config["click_position"] = None
        save_config(self.config)
        self._update_status_display()
        self._log("点击位置已清除")

    # ---- 区域选择 ----

    def _on_select_region(self):
        """打开框选界面"""
        if self.running:
            messagebox.showwarning("请先停止", "请先停止检测再重新框选区域")
            return

        self.root.withdraw()
        time.sleep(0.15)

        try:
            initial = self.config.get("monitor_region")
            selector = RegionSelector(self.root, initial_region=initial)
            region = selector.run()
        finally:
            try:
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            except Exception:
                pass

        if region:
            self.config["monitor_region"] = region
            save_config(self.config)
            self._update_status_display()
            self._log(f"检测区域已设置: ({region[0]}, {region[1]}) {region[2]}×{region[3]}")
            self._set_status(f"● 就绪 — 检测区域: {region[2]}×{region[3]}")
        else:
            self._log("框选已取消")

    # ---- 启动 / 停止 ----

    def _on_start(self):
        """开始检测"""
        region = self.config.get("monitor_region")
        if not region:
            messagebox.showwarning("未设置区域", "请先框选检测区域")
            return

        if not self.click_var.get() and not self.action_var.get():
            messagebox.showwarning("未启用动作", "请至少勾选一种执行动作")
            return

        # 保存当前设置
        trigger_mode = self._mode_value.get(self.trigger_mode_var.get(), "change")
        self.config["trigger_mode"] = trigger_mode
        self.config["message"] = self.msg_entry.get()
        self.config["sensitivity"] = self.sensitivity_var.get()
        self.config["cooldown_seconds"] = self.cooldown_var.get()
        self.config["check_interval"] = self.interval_var.get()
        self.config["trigger_keywords"] = self.trigger_kw_entry.get()
        self.config["block_keywords"] = self.block_kw_entry.get()
        self.config["click_enabled"] = self.click_var.get()
        self.config["action_enabled"] = self.action_var.get()
        save_config(self.config)

        # 创建检测器
        self.monitor = ScreenMonitor(
            region=region,
            sensitivity=self.config["sensitivity"],
            cooldown=self.config["cooldown_seconds"],
            interval=self.config["check_interval"],
            ocr_trigger_enabled=(trigger_mode == "ocr"),
            trigger_keywords=self.config["trigger_keywords"],
            block_keywords=self.config["block_keywords"],
        )
        self.monitor.set_on_change(self._on_screen_change)
        self.monitor.start()
        self.running = True

        # 锁定所有控件
        for child in self.root.winfo_children():
            self._disable_all_widgets(child)
        self.stop_btn.state(["!disabled"])

        mode_name = "画面变动+OCR" if trigger_mode == "ocr" else "画面变动"
        self._log(f"▶ 检测已启动")
        self._log(f"  触发: {mode_name} | "
                  f"动作: {'点击' if self.config['click_enabled'] else ''}"
                  f"{'+发送' if self.config['action_enabled'] else ''}")
        if trigger_mode == "ocr":
            kw = self.config["trigger_keywords"] or "(任意文字)"
            bk = self.config["block_keywords"] or "(无)"
            self._log(f"  OCR 触发词:{kw} | 屏蔽词:{bk}")
        self._set_status("● 检测中…")

    def _disable_all_widgets(self, parent):
        """递归禁用所有交互控件（除了 stop 按钮和日志）"""
        for child in parent.winfo_children():
            if child == self.stop_btn:
                continue
            if isinstance(child, (ttk.Button, ttk.Entry, ttk.Scale,
                                  ttk.Checkbutton, ttk.Combobox, ttk.Spinbox)):
                try:
                    child.state(["disabled"])
                except Exception:
                    pass
            self._disable_all_widgets(child)

    def _enable_all_widgets(self, parent):
        """递归启用所有交互控件"""
        for child in parent.winfo_children():
            if isinstance(child, (ttk.Button, ttk.Entry, ttk.Scale,
                                  ttk.Checkbutton, ttk.Combobox, ttk.Spinbox)):
                try:
                    child.state(["!disabled"])
                    if isinstance(child, ttk.Combobox):
                        child.config(state="readonly")
                except Exception:
                    pass
            self._enable_all_widgets(child)

    def _on_stop(self):
        """停止检测"""
        self.running = False
        if self.monitor:
            self.monitor.stop()
            self.monitor = None

        self._enable_all_widgets(self.root)
        self.stop_btn.state(["disabled"])
        # 恢复显隐状态
        self._update_ocr_ui_state()
        self._update_click_ui_state()
        self._update_action_ui_state()
        self._log("■ 检测已停止")
        self._set_status("● 已停止")

    # ---- 画面变化回调 ----

    def _on_screen_change(self, change_ratio, ocr_text="", matched_info=""):
        """画面变化回调（在检测线程中调用）"""
        def _do_send():
            self._log(f"⚠ 检测到画面变化 (变化率: {change_ratio:.1%})")

            # OCR 识别结果
            if self.config.get("trigger_mode") == "ocr":
                display_text = ocr_text[:80] + "..." if len(ocr_text) > 80 else ocr_text
                if ocr_text:
                    self._log(f"  🔍 OCR 识别: \"{display_text}\"")
                else:
                    self._log(f"  🔍 OCR 未识别到文字")

            # 关键词未匹配 -> 不执行动作
            if matched_info and "忽略" in matched_info:
                reason = matched_info.replace("忽略: ", "")
                if reason:
                    self._log(f"  ⛔ {reason}，不触发")
                else:
                    self._log(f"  ⛔ 关键词不匹配，不触发")
                self._set_status("● 监视中 — 关键词未匹配")
                return

            if matched_info:
                self._log(f"  ✓ {matched_info}匹配")

            # 收集要执行的动作
            click_pos = self.config.get("click_position") if self.config.get("click_enabled") else None
            message = self.config.get("message", "") if self.config.get("action_enabled") else ""

            if not click_pos and not message:
                self._log("  · 未启用任何动作（无点击、无发送）")
                return

            self._log(f"  执行动作: "
                      f"{'点击 ' if click_pos else ''}"
                      + (f'发送 "{message}"' if message else ''))
            success, info = self.executor.execute(message=message, click_pos=click_pos)
            if success:
                self._log(f"  ✓ {info}")
                self._set_status("● 检测中 — 上次触发: " + datetime.now().strftime("%H:%M:%S"))
            else:
                self._log(f"  ✗ {info}")
                self._set_status(f"⚠ {info}")

        self.root.after(0, _do_send)

    # ---- 热键 ----

    def _register_hotkey(self):
        """注册 Ctrl+Shift+Q 为紧急停止热键"""
        if HAS_KEYBOARD:
            try:
                keyboard.add_hotkey("ctrl+shift+q", self._hotkey_stop)
                self._log("热键已注册: Ctrl+Shift+Q = 紧急停止")
            except Exception as e:
                self._log(f"热键注册失败（可能需要管理员权限）: {e}")

    def _hotkey_stop(self):
        """热键回调"""
        if self.running:
            self.root.after(0, self._on_stop)

    # ---- 生命周期 ----

    def run(self):
        """启动主界面"""
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        """关闭窗口"""
        if self.running and self.monitor:
            self.monitor.stop()
        if HAS_KEYBOARD:
            try:
                keyboard.unhook_all()
            except Exception:
                pass
        self.root.destroy()


# ===========================================================================
#                              入口
# ===========================================================================

def main():
    # 禁用 pyautogui 的 fail-safe（鼠标移到角落不会中断）
    pyautogui.FAILSAFE = False

    app = ControlPanel()
    app.run()


if __name__ == "__main__":
    main()
