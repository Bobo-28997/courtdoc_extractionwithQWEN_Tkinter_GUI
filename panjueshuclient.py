import json
import os
import sys
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox, PanedWindow
import requests  # (新) 客户端只使用 requests

# --- 1. (新) 配置后端服务器地址 ---
# 【【【重要】】】
# 您必须将 '127.0.0.1' 替换为您运行 `backend_server.py` 那台服务器的【真实内网 IP】
BACKEND_SERVER_URL = "http://127.0.0.1:5000/extract"


# --- (已删除) ---
# 所有的 API Key, Prompt, AI 函数都已从客户端移除
# --- (已删除) ---


# --- 2. (新) 辅助函数：JSON 到“易读摘要”的转换器 ---
def format_json_for_display(data: dict) -> str:
    """(新) 将提取的 JSON 转换为易于复制的文本摘要"""
    lines = []

    # --- (V25.3 修复) ---
    # 我们将重写 'p' 辅助函数，使其能正确显示 null
    def p(key, value):
        # 1. (新) 如果值是 None (来自 JSON null)，则明确显示 "null"
        if value is None:
            lines.append(f"{key}: null")
        # 2. (不变) 如果值是 "真" (非空字符串, 数字等), 则显示该值
        elif value:
            lines.append(f"{key}: {value}")
        # 3. (如果 value 是 "" (空字符串)，此逻辑将保持省略，这通常是好的)

    p("涉诉类型", data.get("type"))
    p("案号", data.get("case_number"))
    p("案由", data.get("cause_of_action"))
    p("受理法院", data.get("court_name"))
    p("上诉法院", data.get("appeal_court"))
    p("收到裁判时间", data.get("date_received"))
    p("上诉截止日", data.get("appeal_deadline"))

    plaintiff = data.get("plaintiff", {})
    if plaintiff:
        lines.append("\n--- 原告信息 ---")
        p("  原告名称", plaintiff.get("name"))
        p("  原告地址", plaintiff.get("address"))
        p("  法定代表人", plaintiff.get("legal_rep"))
        p("  委托代理人", plaintiff.get("authorized_agent"))
        p("  代理费", plaintiff.get("agent_price"))

    defendant = data.get("defendant", {})
    if defendant:
        lines.append("\n--- 被告信息 ---")
        p("  被告名称", defendant.get("name"))
        p("  被告地址", defendant.get("address"))
        p("  法定代表人", defendant.get("legal_rep"))
        p("  委托代理人", defendant.get("authorized_agent"))

    lines.append("\n--- 其他信息 ---")
    p("第三人", data.get("third_party"))
    p("主审法官", data.get("presiding_judge"))
    p("执行法官", data.get("execution_judge"))

    if data.get("claims"):
        lines.append("\n--- 诉讼请求 ---")
        lines.append(data.get("claims"))
    if data.get("facts_and_reasons"):
        lines.append("\n--- 事实和理由 ---")
        lines.append(data.get("facts_and_reasons"))
    if data.get("judgment_main"):
        lines.append("\n--- 一审裁判主文 ---")
        lines.append(data.get("judgment_main"))

    # (新) 处理服务器返回的错误
    if data.get("error"):
        lines.append("\n--- 服务器错误 ---")
        lines.append(data.get("error"))

    return "\n".join(lines)


# --- 3. (不变) GUI 日志重定向 ---
class TextRedirector:
    def __init__(self, widget):
        self.widget = widget
        self.is_main_thread = threading.current_thread() is threading.main_thread()

    def write(self, str):
        if not self.is_main_thread:
            self.widget.after(0, self.thread_safe_write, str)
        else:
            self.thread_safe_write(str)

    def thread_safe_write(self, str):
        try:
            self.widget.config(state='normal')
            self.widget.insert(tk.END, str)
            self.widget.see(tk.END)
            self.widget.config(state='disabled')
        except tk.TclError:
            pass

    def flush(self):
        pass


# --- 4. (不变) Tkinter 应用主类 (V24) ---
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("法律文书提取工具 (V25-C/S 演示版)")
        self.root.geometry("1000x700")
        self.filepath = None

        self.top_frame = tk.Frame(root, padx=10, pady=10)
        self.top_frame.pack(fill=tk.X)
        self.file_frame = tk.Frame(self.top_frame)
        self.file_frame.pack(fill=tk.X)
        self.select_btn = tk.Button(self.file_frame, text="1. 选择 PDF 文件", command=self.select_file, width=15)
        self.select_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.file_label = tk.Label(self.file_frame, text="未选择文件", anchor="w", relief="sunken", bg="white", padx=5)
        self.file_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.run_btn = tk.Button(self.top_frame, text="2. 开始提取 (文本型或扫描型)",
                                 command=self.start_extraction_thread, state="disabled", bg="#c0f0c0")
        self.run_btn.pack(fill=tk.X, pady=5)

        self.paned_window = PanedWindow(root, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=5)
        self.paned_window.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.summary_frame = tk.Frame(self.paned_window, width=500)
        self.summary_label = tk.Label(self.summary_frame, text="摘要 (易于复制):")
        self.summary_label.pack(anchor="w")
        self.summary_widget = scrolledtext.ScrolledText(self.summary_frame, height=25, state="disabled", wrap=tk.WORD,
                                                        font=("Arial", 9))
        self.summary_widget.pack(fill=tk.BOTH, expand=True)
        self.paned_window.add(self.summary_frame, minsize=200)

        self.log_frame = tk.Frame(self.paned_window, width=500)
        self.log_label = tk.Label(self.log_frame, text="原始日志 (含 JSON):")
        self.log_label.pack(anchor="w")
        self.log_widget = scrolledtext.ScrolledText(self.log_frame, height=25, state="disabled", wrap=tk.WORD,
                                                    bg="black", fg="lightgreen", font=("Consolas", 9))
        self.log_widget.pack(fill=tk.BOTH, expand=True)
        self.paned_window.add(self.log_frame, minsize=200)

        self.redirector = TextRedirector(self.log_widget)
        sys.stdout = self.redirector
        sys.stderr = self.redirector

        print("--- 欢迎使用 V25 客户端 ---")
        print(f"--- 后端服务器已设置为: {BACKEND_SERVER_URL} ---")
        print("请点击按钮选择一个 PDF 文件开始。\n")

    def select_file(self):
        self.filepath = filedialog.askopenfilename(
            title="请选择一个 PDF 文书",
            filetypes=[("PDF Files", "*.pdf")]
        )
        if self.filepath:
            self.file_label.config(text=f"已选择: {Path(self.filepath).name}")
            self.run_btn.config(state="normal")
            print(f"--- 用户已选择文件: {self.filepath} ---\n")
        else:
            self.file_label.config(text="未选择文件")
            self.run_btn.config(state="disabled")

    def start_extraction_thread(self):
        if not self.filepath:
            print("【错误】：请先选择一个文件。\n")
            return

        self.run_btn.config(state="disabled", text="正在发送到服务器...请稍候...")
        self.log_widget.config(state='normal')
        self.log_widget.delete(1.0, tk.END)
        self.log_widget.config(state='disabled')
        self.summary_widget.config(state='normal')
        self.summary_widget.delete(1.0, tk.END)
        self.summary_widget.config(state='disabled')
        print("--- 开始新任务 ---")

        threading.Thread(target=self.run_extraction_logic, daemon=True).start()

    # (新) 线程安全的 GUI 更新函数
    def display_results(self, data, chunk_name=""):
        """(新) 线程安全地更新两个窗口"""
        if not data:
            print(f"--- {chunk_name} 未返回有效数据。---")
            return

        try:
            formatted_text = format_json_for_display(data)
            self.summary_widget.config(state='normal')
            if chunk_name:
                self.summary_widget.insert(tk.END, f"--- {chunk_name} 的摘要 ---\n")
            self.summary_widget.insert(tk.END, formatted_text + "\n\n")
            self.summary_widget.see(tk.END)
            self.summary_widget.config(state='disabled')
        except Exception as e:
            print(f"【GUI 错误】 格式化摘要时出错: {e}\n")

        title = f" 原始 JSON {chunk_name} "
        print("\n" + f" {title.center(48, '=')} ")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print("\n" + "=" * 50)

    def reset_button(self):
        self.run_btn.config(state="normal", text="2. 开始提取 (文本型或扫描型)")

    # --- 5. (重大修改) 核心逻辑 (现在只负责“发送”) ---
    def run_extraction_logic(self):
        """
        (V25 核心) 这是在“后台线程”中运行的。
        它只负责发送文件，不包含任何 AI 逻辑。
        """
        try:
            INPUT_FILE_PATH = self.filepath
            print(f"--- 正在打开文件: {Path(INPUT_FILE_PATH).name} ---")

            with open(INPUT_FILE_PATH, 'rb') as f:
                files_payload = {'file': (Path(INPUT_FILE_PATH).name, f, 'application/pdf')}
                print(f"--- 正在上传文件到: {BACKEND_SERVER_URL} ---")

                # (VLM 可能需要非常久, 15分钟超时)
                response = requests.post(BACKEND_SERVER_URL, files=files_payload, timeout=900)

                response.raise_for_status()

                # (新) 服务器现在【总是】返回一个列表
                results_list = response.json()

            print(f"\n--- 后端服务器成功返回 {len(results_list)} 个结果 ---")

            # (新) 循环打印所有结果
            for i, result_data in enumerate(results_list):
                chunk_name = ""
                if len(results_list) > 1:
                    chunk_name = f"(批次 {i + 1}/{len(results_list)})"

                # (新) 线程安全地调用 GUI 更新
                self.root.after(0, self.display_results, result_data, chunk_name)

            print("\n【任务结束】")

        except requests.exceptions.ConnectionError:
            print(f"【客户端错误】: 无法连接到后端服务器 {BACKEND_SERVER_URL}")
            print("         请检查：")
            print("         1. 后端服务器 (backend_server.py) 是否已在运行？")
            print("         2. 客户端的 IP 地址和端口是否设置正确？")
            print("         3. 公司 VPN 或防火墙是否已连接/允许？\n")
        except requests.exceptions.HTTPError as e:
            print(f"【服务器返回错误】: {e.response.status_code}")
            try:
                error_json = e.response.json()
                print(f"         错误详情: {error_json.get('error', '未知错误')}\n")
            except:
                print(f"         原始响应: {e.response.text}\n")
        except requests.exceptions.Timeout:
            print("【客户端错误】: 请求超时（超过 15 分钟）。")
        except Exception as e:
            print(f"【未捕获的客户端错误】: {e}\n")

        finally:
            self.root.after(0, self.reset_button)


# --- 6. (新) 启动 Tkinter 应用 ---
if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()