import os
import subprocess
import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox
import sys

try:
    import pystray
    from pystray import MenuItem as item
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

os.chdir(os.path.dirname(os.path.abspath(__file__)))

LANG_STRINGS = {
    'zh': {
        'title': "Codex Slack 后台服务面板",
        'start': "▶ 启动服务",
        'stop': "⏹ 停止服务",
        'env_edit': "⚙ 编辑环境配置",
        'lang': "🌐 English",
        'status_unrun': "状态: 未运行",
        'status_run': "状态: 运行中",
        'log_label': "运行日志:",
        'tray_enabled': "系统托盘(Background Tray)功能已启用。关闭本窗口将隐藏至右下角托盘。",
        'tray_disabled': "未检测到 pystray，托盘功能已禁用。关闭窗口将直接退出。",
        'start_fail': "启动失败",
        'server_started': "--- 服务已启动 ---",
        'server_stopped': "--- 服务已停止 ---",
        'server_stopping': "正在终止服务...",
        'menu_show': '显示面板',
        'menu_start': '启动服务',
        'menu_stop': '停止服务',
        'menu_quit': '安全退出',
        'env_title': ".env 配置编辑",
        'env_save': "💾 保存配置",
        'env_save_success': "配置保存成功！"
    },
    'en': {
        'title': "Codex Slack Background Service Panel",
        'start': "▶ Start Service",
        'stop': "⏹ Stop Service",
        'env_edit': "⚙ Edit .env Config",
        'lang': "🌐 中文",
        'status_unrun': "Status: Not Running",
        'status_run': "Status: Running",
        'log_label': "Execution Logs:",
        'tray_enabled': "Background Tray feature enabled. Closing this window will minimize it to the tray.",
        'tray_disabled': "pystray not detected. Tray disabled. Closing window will exit.",
        'start_fail': "Failed to start",
        'server_started': "--- Service Started ---",
        'server_stopped': "--- Service Stopped ---",
        'server_stopping': "Stopping service...",
        'menu_show': 'Show Panel',
        'menu_start': 'Start Service',
        'menu_stop': 'Stop Service',
        'menu_quit': 'Quit Safely',
        'env_title': ".env Configuration Editor",
        'env_save': "💾 Save Configuration",
        'env_save_success': "Configuration saved successfully!"
    }
}

def create_tray_image():
    image = Image.new('RGB', (64, 64), color='#f0f0f0')
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill='#4CAF50')
    return image

class CodexSlackGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Codex Slack 后台服务面板")
        self.root.geometry("650x450")
        
        self.current_lang = 'zh'
        self.process = None
        self.icon = None
        
        btn_frame = tk.Frame(root, padx=10, pady=10)
        btn_frame.pack(fill=tk.X)
        self.start_btn = tk.Button(btn_frame, command=self.start_server, bg="lightgreen", width=15)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = tk.Button(btn_frame, command=self.stop_server, bg="pink", state=tk.DISABLED, width=15)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.env_btn = tk.Button(btn_frame, command=self.edit_env, bg="lightblue", width=15)
        self.env_btn.pack(side=tk.LEFT, padx=5)
        
        self.lang_btn = tk.Button(btn_frame, command=self.toggle_lang, width=10)
        self.lang_btn.pack(side=tk.RIGHT, padx=5)
        self.status_lbl = tk.Label(btn_frame, fg="gray", font=("Arial", 10, "bold"))
        self.status_lbl.pack(side=tk.RIGHT, padx=5)

        log_frame = tk.Frame(root, padx=10, pady=5)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_lbl = tk.Label(log_frame)
        self.log_lbl.pack(anchor=tk.W)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, bg="black", fg="white", font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        
        self.update_ui_text()
        
        s = LANG_STRINGS[self.current_lang]
        if HAS_TRAY:
            self.setup_tray()
            self.log(s['tray_enabled'])
        else:
            self.log(s['tray_disabled'])

    def toggle_lang(self):
        self.current_lang = 'en' if self.current_lang == 'zh' else 'zh'
        self.update_ui_text()

    def update_ui_text(self):
        s = LANG_STRINGS[self.current_lang]
        self.root.title(s['title'])
        self.start_btn.config(text=s['start'])
        self.stop_btn.config(text=s['stop'])
        self.env_btn.config(text=s['env_edit'])
        self.lang_btn.config(text=s['lang'])
        self.log_lbl.config(text=s['log_label'])
        
        if self.process:
            self.status_lbl.config(text=s['status_run'])
        else:
            self.status_lbl.config(text=s['status_unrun'])
            
        if self.icon:
            menu = pystray.Menu(
                item(s['menu_show'], self.action_show_window, default=True),
                item(s['menu_start'], self.action_start_server),
                item(s['menu_stop'], self.action_stop_server),
                item(s['menu_quit'], self.action_quit)
            )
            self.icon.menu = menu

    def edit_env(self):
        s = LANG_STRINGS[self.current_lang]
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        content = ""
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                content = f.read()

        top = tk.Toplevel(self.root)
        top.title(s['env_title'])
        top.geometry("550x450")
        
        text_area = scrolledtext.ScrolledText(top, wrap=tk.WORD, font=("Consolas", 10))
        text_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        text_area.insert(tk.END, content)
        
        def save_env():
            new_content = text_area.get(1.0, tk.END)
            # Retain original newline to prevent blank accumulation
            new_content = new_content.rstrip('\n') + '\n'
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            messagebox.showinfo("Success", s['env_save_success'], parent=top)
            top.destroy()
            
        save_btn = tk.Button(top, text=s['env_save'], bg="lightblue", command=save_env, width=20)
        save_btn.pack(pady=10)

    def setup_tray(self):
        s = LANG_STRINGS[self.current_lang]
        # 创建系统托盘菜单
        menu = pystray.Menu(
            item(s['menu_show'], self.action_show_window, default=True),
            item(s['menu_start'], self.action_start_server),
            item(s['menu_stop'], self.action_stop_server),
            item(s['menu_quit'], self.action_quit)
        )
        self.icon = pystray.Icon("codex_slack", create_tray_image(), "Codex Slack", menu)
        # 另起线程运行 tray，防止阻塞主线程
        threading.Thread(target=self.icon.run, daemon=True).start()

    def action_show_window(self, icon, item):
        self.root.after(0, self.root.deiconify)

    def action_start_server(self, icon, item):
        self.root.after(0, self.start_server)

    def action_stop_server(self, icon, item):
        self.root.after(0, self.stop_server)

    def action_quit(self, icon, item):
        self.icon.stop()
        self.root.after(0, self.quit_app)

    def hide_window(self):
        if HAS_TRAY:
            self.root.withdraw()
            # 可以在此处可选的触发系统的 Toast 提示
        else:
            self.quit_app()

    def quit_app(self):
        self.stop_server()
        if self.icon:
            self.icon.stop()
        self.root.destroy()

    def log(self, msg):
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)

    def start_server(self):
        if self.process is not None:
            return
        executable = sys.executable or "python"
        try:
            self.process = subprocess.Popen(
                [executable, "server.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                text=True,
                encoding="utf-8",
                errors="replace"
            )
        except Exception as e:
            messagebox.showerror(LANG_STRINGS[self.current_lang]['start_fail'], str(e))
            return
            
        self.status_lbl.config(text=LANG_STRINGS[self.current_lang]['status_run'], fg="green")
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.env_btn.config(state=tk.DISABLED)
        self.log(LANG_STRINGS[self.current_lang]['server_started'])
        
        threading.Thread(target=self.read_output, daemon=True).start()

    def read_output(self):
        if not self.process:
            return
        while True:
            line = self.process.stdout.readline()
            if not line:
                break
            self.root.after(0, self.log, line.strip())
            
        # 只有在非自己手动停止的情况下才会被迫退出。
        self.process.wait()
        self.process = None
        self.root.after(0, self.on_process_exit)

    def on_process_exit(self):
        self.status_lbl.config(text=LANG_STRINGS[self.current_lang]['status_unrun'], fg="gray")
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.env_btn.config(state=tk.NORMAL)
        self.log(LANG_STRINGS[self.current_lang]['server_stopped'])

    def stop_server(self):
        if self.process:
            self.log(LANG_STRINGS[self.current_lang]['server_stopping'])
            self.process.terminate()

if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    
    lock_path = Path(tempfile.gettempdir()) / ".codex-slack-launcher.pid"
    try:
        lock_handle = lock_path.open("a+", encoding="utf-8")
        if os.name == 'nt':
            import msvcrt
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (PermissionError, OSError, IOError):
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("提示", "Codex Slack 控制面板已经在后台运行。")
        sys.exit(0)

    root = tk.Tk()
    app = CodexSlackGUI(root)
    root.mainloop()
