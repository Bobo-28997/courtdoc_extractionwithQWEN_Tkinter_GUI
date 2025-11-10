import json
import os
import sys
import tempfile
from pathlib import Path
from dotenv import load_dotenv
import openai
from openai import OpenAI
import dashscope
import fitz  # PyMuPDF
from PIL import Image
import time
import requests
from http.client import RemoteDisconnected
import threading  # (新) 导入线程，防止GUI卡死
import tkinter as tk  # (新) 导入 Tkinter
from tkinter import filedialog, scrolledtext, messagebox, PanedWindow

# --- 1. 加载所有 Keys (不变) ---
load_dotenv()
API_KEY = xx

if not API_KEY:
    messagebox.showerror("API Key 错误",
                         "【错误】: 环境变量 'DASHSCOPE_API_KEY' 未设置。\n请创建 .env 文件并写入 DASHSCOPE_API_KEY=sk-xxxx...")
    sys.exit(1)

# --- 2. 初始化两个客户端 (不变) ---
QWEN_TEXT_CLIENT = OpenAI(
    api_key=API_KEY,
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)
QWEN_MODEL_NAME_TEXT = "qwen-plus-2025-01-25"
dashscope.api_key = API_KEY
QWEN_MODEL_NAME_VISION = "qwen-vl-max"

# --- 3. (V22 优化) VLM 处理常量 (不变) ---
IMAGE_COMPRESSION_QUALITY = 85
VLM_PAGE_CHUNK_SIZE = 3  # (3 页批次更稳定)
MAX_RETRIES = 3
API_TIMEOUT = 600  # 10 分钟

# --- 4. (V23 - 最终版) 您的 "完整提取" Prompt ---
SYSTEM_PROMPT = """你是一个极其严谨、注重细节的法律文书提取机器人。
你的【唯一】任务是，且【仅仅】是：严格按照“JSON Output”范例的【结构(Schema)】，从“法律文书原文”中提取信息。

--- 关键规则 (必须严格遵守) ---
1. 你必须严格模仿我给出的示例1的【JSON结构】（包括嵌套对象）。
2. 【案号陷阱】：`case_number` (案号) 常常【没有'案号：'这样的标签】，而是作为单独一行【出现在文书的最顶部】。
3. 【法院陷阱】：`court_name` (受理法院) 是指文书【顶部】或【正文开头】的主要法院。`appeal_court` (上诉法院) 【仅仅】是指在文书【末尾】“如不服本判决”部分提到的法院。如果正文开头没有 `court_name`，它【必须】为 `null`。
4. 【多重字段陷阱】：如果有多个“被告”，请将他们的 `name` 用 `、` (顿号) 连接合并。
5. 【案由陷阱】：`cause_of_action` 必须提取原文中 `...一案` 的描述。
6. 【请求/理由陷阱】：`claims` 和 `facts_and_reasons` 必须提取对应标题（'诉讼请求：' 或 '事实和理由：'）之后的【完整段落或列表】。
7. 【上诉陷阱】：`appeal_deadline` 必须提取原文中关于“上诉期”的描述，通常是 `...送达之日起XX日内`。
8. 【裁判主文陷阱】：`judgment_main` 必须提取“判决如下：”之后的【所有】编号项（例如 一、 二、 等）。

--- 格式与内容铁则 (最重要) ---
9. 【完整性铁则 (新!)】：对于 `claims`, `facts_and_reasons`, `judgment_main` 这样的长文本字段，你【必须】提取 100% 逐字对应的完整原文。你【绝对禁止】以任何形式（例如 “...（省略）...” 或 "xxx（中间省略）xxx"）进行缩写、截断或总结。
10. 【缺失字段铁则】：如果范例中的某个字段（例如 `third_party`）在“法律文书原文”中【完全找不到对应信息】，你【必须】在 JSON 中保留这个键 (key)，并将其值 (value) 设为 `null`。
11. 【Null vs 空字符串铁则】：当一个字段的值为 `null` 时，它【必须】是 JSON 中的 `null` 关键字，【绝对禁止】使用空字符串 `""` 来代替。
12. 【禁止“聪明”铁则】：你【绝对禁止】提取范例 JSON 结构中【没有出现过】的任何信息（例如，禁止提取“统一社会信用代码”、“身份证号”等）。你的任务是【严格填充】，不是“主动帮忙”。
13. 【输出格式铁则】：你的回答【必须且只能】是一个工整的 JSON 对象。它必须以 `{` 开始，以 `}` 结束。在 `{...}` 之外，绝对不允许有任何其他文本、注释、Markdown标记或任何解释。

--- 示例1 (黄金范例) --- 
法律文书原文:
(2025) 京01民初789号 北京市海淀区人民法院 民 事 起 诉 状 (本诉状为虚构)
原告：北京A科技公司，住所地：北京市海淀区中关村大街1号。 法定代表人：王明，董事长。 委托诉讼代理人：李杨，北京基米律师事务所律师。 被告：上海B数据服务有限公司，住所地：北京市海淀区中关村大街9号。 被告：牛二，住北京市海淀区中关村大街3号。 第三人：无。
本院在审理原告北京A科技公司与被告上海B数据服务有限公司、牛二合同纠纷一案中，...
原告向本院提出诉讼请求：1. 请求判令二被告共同支付原告合同款100,000元；2. 请求判令二被告共同支付原告代理费5,000元；3. 本案诉讼费由二被告承担。 事实和理由：二被告于2024年5月1日与原告签订《软件开发合同》，约定开发周期为三个月，合同总金额10万元。原告依约交付软件后，二被告至今无故拒不支付合同款项。原告为本案诉讼支出代理费5000元。为维护原告合法权益，特诉至贵院，望判如所请。
本院受理后，依法适用简易程序。本案现已审理终结。
判决如下： 一、被告上海B数据服务有限公司、牛二应于本判决生效之日起十日内，共同支付原告北京A科技公司合同款100,000元及代理费5,000元，共计105,000元。 二、驳回原告北京A科技公司的其他诉讼请求。 案件受理费1000元，由二被告负担。 如不服本判决，可在判决书送达之日起十五日内，向本院递交上诉状。
审判员：张伟 二〇二五年十月一日
书记员：李华

JSON Output:
{
  "type": "民事诉讼",
  "plaintiff": {
    "name": "北京A科技公司",
    "address": "北京市海淀区中关村大街1号",
    "legal_rep": "王明",
    "authorized_agent": "李杨，北京基米律师事务所律师",
    "agent_price": "5000元"
  },
  "defendant": {
    "name": "上海B数据服务有限公司、牛二",
    "address": "北京市海淀区中关村大街9号、北京市海淀区中关村大街3号",
    "legal_rep": null,
    "authorized_agent": null
  },
  "third_party": null,
  "claims": "1. 请求判令二被告共同支付原告合同款100,000元；2. 请求判令二被告共同支付原告代理费5,000元；3. 本案诉讼费由二被告承担。",
  "facts_and_reasons": "二被告于2024年5月1日与原告签订《软件开发合同》，约定开发周期为三个月，合同总金额10万元。原告依约交付软件后，二被告至今无故拒不支付合同款项。原告为本案诉讼支出代理费5000元。为维护原告合法权益，特诉至贵院，望判如所请。",
  "court_name": "北京市海淀区人民法院",
  "appeal_court": "本院",
  "cause_of_action": "合同纠纷一案",
  "case_number": "(2025) 京01民初789号",
  "presiding_judge": "张伟",
  "execution_judge": null,
  "date_received": "二〇二五年十月一日",
  "appeal_deadline": "判决书送达之日起十五日内",
  "judgment_main": "一、被告上海B数据服务有限公司、牛二应于本判决生效之日起十日内，共同支付原告北京A科技公司合同款100,000元及代理费5,000元，共计105,000元。\n二、驳回原告北京A科技公司的其他诉讼请求。"
}
--- 真实任务 ---
现在，请【严格遵守上述所有规则，尤其是“法院陷阱”规则】，处理我提供的文件。
"""

# --- 5. (不变) 辅助函数：分块器 ---
def chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# --- 6. (新) 辅助函数：JSON 到“易读摘要”的转换器 ---
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

    print("--- 正在格式化易读摘要... ---")
    p("涉诉类型", data.get("type"))
    p("案号", data.get("case_number"))
    p("案由", data.get("cause_of_action"))
    p("受理法院", data.get("court_name"))
    p("上诉法院", data.get("appeal_court"))
    p("收到裁判时间", data.get("date_received"))
    p("上诉截止日", data.get("appeal_deadline"))

    # 处理原告
    plaintiff = data.get("plaintiff", {})
    if plaintiff:
        lines.append("\n--- 原告信息 ---")
        p("  原告名称", plaintiff.get("name"))
        p("  原告地址", plaintiff.get("address"))
        p("  法定代表人", plaintiff.get("legal_rep"))
        p("  委托代理人", plaintiff.get("authorized_agent"))
        p("  代理费", plaintiff.get("agent_price"))

    # 处理被告
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

    # 处理长文本
    if data.get("claims"):
        lines.append("\n--- 诉讼请求 ---")
        lines.append(data.get("claims"))
    if data.get("facts_and_reasons"):
        lines.append("\n--- 事实和理由 ---")
        lines.append(data.get("facts_and_reasons"))
    if data.get("judgment_main"):
        lines.append("\n--- 一审裁判主文 ---")
        lines.append(data.get("judgment_main"))

    return "\n".join(lines)


# --- 7. (新) GUI 日志重定向 ---
class TextRedirector:
    """将 print 语句重定向到 tkinter Text 控件"""

    def __init__(self, widget):
        self.widget = widget
        self.is_main_thread = threading.current_thread() is threading.main_thread()

    def write(self, str):
        if not self.is_main_thread:
            # 如果是来自后台线程的 print，安全地调度到主线程
            self.widget.after(0, self.thread_safe_write, str)
        else:
            self.thread_safe_write(str)

    def thread_safe_write(self, str):
        """(线程安全) 写入 GUI"""
        try:
            self.widget.config(state='normal')
            self.widget.insert(tk.END, str)
            self.widget.see(tk.END)
            self.widget.config(state='disabled')
        except tk.TclError:
            # 窗口可能已关闭
            pass

    def flush(self):
        pass


# --- 8. (新) Tkinter 应用主类 ---
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("法律文书提取工具 (V24-GUI 演示版)")
        self.root.geometry("1000x700")  # 窗口加大
        self.filepath = None

        # --- 1. 创建 GUI 控件 ---
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

        # --- (新) 可拖拽的左右分栏 ---
        self.paned_window = PanedWindow(root, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=5)
        self.paned_window.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # --- (新) 左侧：易读摘要 ---
        self.summary_frame = tk.Frame(self.paned_window, width=500)
        self.summary_label = tk.Label(self.summary_frame, text="摘要 (易于复制):")
        self.summary_label.pack(anchor="w")
        self.summary_widget = scrolledtext.ScrolledText(self.summary_frame, height=25, state="disabled", wrap=tk.WORD,
                                                        font=("Arial", 9))
        self.summary_widget.pack(fill=tk.BOTH, expand=True)
        self.paned_window.add(self.summary_frame, minsize=200)

        # --- (新) 右侧：原始日志 ---
        self.log_frame = tk.Frame(self.paned_window, width=500)
        self.log_label = tk.Label(self.log_frame, text="原始日志 (含 JSON):")
        self.log_label.pack(anchor="w")
        self.log_widget = scrolledtext.ScrolledText(self.log_frame, height=25, state="disabled", wrap=tk.WORD,
                                                    bg="black", fg="lightgreen", font=("Consolas", 9))
        self.log_widget.pack(fill=tk.BOTH, expand=True)
        self.paned_window.add(self.log_frame, minsize=200)

        # --- 2. (关键) 重定向 stdout/stderr ---
        self.redirector = TextRedirector(self.log_widget)
        sys.stdout = self.redirector
        sys.stderr = self.redirector

        print("--- 欢迎使用 V24 演示版 ---")
        print(f"--- Qwen (大脑) 模型: {QWEN_MODEL_NAME_TEXT} ---")
        print(f"--- Qwen (眼睛) 模型: {QWEN_MODEL_NAME_VISION} ---")
        print(f"--- VLM 批次大小: {VLM_PAGE_CHUNK_SIZE} 页 ---")
        print(f"--- VLM 图像质量: {IMAGE_COMPRESSION_QUALITY}% ---")
        print("请点击按钮选择一个 PDF 文件开始。\n")

    def select_file(self):
        """(不变) 由“选择 PDF”按钮调用"""
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
        """(不变) 在新线程中运行，防止 GUI 卡死"""
        if not self.filepath:
            print("【错误】：请先选择一个文件。\n")
            return

        # 1. 禁用按钮，清空两个日志
        self.run_btn.config(state="disabled", text="正在提取中，请稍候...")
        self.log_widget.config(state='normal')
        self.log_widget.delete(1.0, tk.END)
        self.log_widget.config(state='disabled')

        self.summary_widget.config(state='normal')
        self.summary_widget.delete(1.0, tk.END)
        self.summary_widget.config(state='disabled')

        print("--- 开始新任务 ---")

        # 2. (不变) 在“守护线程”中运行核心逻辑
        threading.Thread(target=self.run_extraction_logic, daemon=True).start()

    # (新) 线程安全的 GUI 更新函数
    def display_results(self, data, chunk_name=""):
        """(新) 线程安全地更新两个窗口"""
        if not data:
            print(f"--- {chunk_name} 未返回有效数据。---")
            return

        # 1. 更新“摘要”窗口
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

        # 2. 更新“日志”窗口 (打印原始 JSON)
        title = f" 最终提取结果 (格式化) {chunk_name} "
        print("\n" + f" {title.center(48, '=')} ")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print("\n" + "=" * 50)

    # (新) 线程安全的按钮重置
    def reset_button(self):
        """(新) (线程安全) 重置“运行”按钮的状态"""
        self.run_btn.config(state="normal", text="2. 开始提取 (文本型或扫描型)")

    # --- 9. (新) 后台核心逻辑 (V22) ---
    # (这些函数现在是 App 的一部分，以便它们可以调用 self.root.after)

    def call_qwen_text_api(self, raw_text):
        """(V22) 使用 OpenAI SDK 调用 Qwen 纯文本模型"""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"法律文书原文(从PDF提取的纯文本):\n{raw_text}"}
        ]
        print(f"--- (大脑) 正在调用 {QWEN_MODEL_NAME_TEXT} (纯文本 API) ---\n")
        try:
            completion = QWEN_TEXT_CLIENT.chat.completions.create(
                model=QWEN_MODEL_NAME_TEXT,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.0
            )
            model_output_string = completion.choices[0].message.content
            return json.loads(model_output_string)
        except Exception as e:
            print(f"【Qwen 文本 API 错误】: {e}\n")
            return None

    def call_qwen_vlm_api(self, file_path):
        """(V22 - 压缩、分块、重试、Bug修复)"""
        print(f"--- (眼睛) 正在使用 PyMuPDF+Pillow 将 {file_path} 转换为压缩 JPEG... ---\n")
        temp_files = []
        doc = None
        all_success = True
        try:
            doc = fitz.open(file_path)
            all_image_urls = []
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                pil_image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                tmp_file_path = f"{tempfile.gettempdir()}/_page{i + 1}_{Path(file_path).stem}.jpg"
                pil_image.save(tmp_file_path, "JPEG", quality=IMAGE_COMPRESSION_QUALITY)
                temp_files.append(tmp_file_path)
                all_image_urls.append(f"file://{Path(tmp_file_path).resolve()}")

            if not all_image_urls:
                print("【VLM 错误】: 无法从 PDF 提取任何图像页面。\n")
                return False

            print(f"--- (眼睛) 成功提取 {len(all_image_urls)} 页图像 (JPEG Quality={IMAGE_COMPRESSION_QUALITY}) ---\n")

            url_chunks = list(chunk_list(all_image_urls, VLM_PAGE_CHUNK_SIZE))
            print(f"--- (眼睛) 已将任务分为 {len(url_chunks)} 个批次 (每批 {VLM_PAGE_CHUNK_SIZE} 页) ---\n")

            for chunk_index, image_url_chunk in enumerate(url_chunks):
                chunk_name = f"批次 {chunk_index + 1}/{len(url_chunks)} (第 {chunk_index * VLM_PAGE_CHUNK_SIZE + 1} - {chunk_index * VLM_PAGE_CHUNK_SIZE + len(image_url_chunk)} 页)"
                print("\n" + "=" * 50)
                print(f"--- (眼睛) 正在处理 {chunk_name} ---\n")

                full_user_prompt_text = f"{SYSTEM_PROMPT}\n\n--- 真实任务：...请严格分析我提供的【这几页】图像..."
                content = [{"image": url} for url in image_url_chunk]
                content.append({"text": full_user_prompt_text})
                messages = [{"role": "user", "content": content}]

                response = None
                for attempt in range(MAX_RETRIES):
                    try:
                        print(f"--- (眼睛) 正在调用 VLM API (Attempt {attempt + 1}/{MAX_RETRIES})... ---\n")
                        response = dashscope.MultiModalConversation.call(
                            model=QWEN_MODEL_NAME_VISION,
                            messages=messages,
                            temperature=0.0,
                            timeout=API_TIMEOUT
                        )
                        if response.status_code == 200:
                            print(f"--- (眼睛) VLM API 调用成功 (Attempt {attempt + 1}) ---\n")
                            break
                        else:
                            print(
                                f"【Qwen VLM API 错误】 ({chunk_name}, Attempt {attempt + 1}): {response.code} - {response.message}\n")
                            if attempt < MAX_RETRIES - 1: time.sleep(5)

                    except (requests.exceptions.ConnectionError, RemoteDisconnected, Exception) as e:
                        print(f"【VLM API 网络/SDK 错误】 ({chunk_name}, Attempt {attempt + 1}): {e}\n")
                        if attempt < MAX_RETRIES - 1: time.sleep(5)

                if response is None or response.status_code != 200:
                    print(f"--- 批次 {chunk_name} 【失败】，已达到最大重试次数。---\n")
                    all_success = False
                    continue

                model_output_content = response.output.choices[0].message.content
                model_output_string = ""
                for item in model_output_content:
                    if 'text' in item: model_output_string += item['text']

                if not model_output_string:
                    print(f"【VLM 错误】 ({chunk_name}): VLM 返回的内容中没有找到文本。\n")
                    all_success = False
                    continue

                json_match = model_output_string[model_output_string.find('{'): model_output_string.rfind('}') + 1]
                if not json_match:
                    print(f"【VLM 错误】 ({chunk_name}): VLM 未返回有效的 JSON 结构。\n")
                    all_success = False
                    continue

                # (新) 线程安全地调用 GUI 更新
                extracted_data = json.loads(json_match)
                self.root.after(0, self.display_results, extracted_data, chunk_name)

            return all_success

        except Exception as e:
            print(f"【VLM 图像转换/处理异常】: {e}\n")
            return False

        finally:
            if doc:
                try:
                    doc.close()
                except:
                    pass
            print("--- (清理) 正在删除所有临时图像文件... ---")
            cleaned_count = 0
            for tmp_file in temp_files:
                try:
                    os.unlink(tmp_file)
                    cleaned_count += 1
                except Exception as e:
                    print(f"【警告】: 无法删除临时文件 {tmp_file}: {e}\n")
            print(f"--- (清理) 成功删除 {cleaned_count}/{len(temp_files)} 个文件 ---\n")

    def detect_pdf_type(self, filepath):
        """(V20 - PyMuPDF 版) 检测 PDF 是“文本型”还是“扫描型”"""
        print(f"--- (检测) 正在使用 PyMuPDF 打开 {filepath} ---\n")
        doc = None
        try:
            doc = fitz.open(filepath)
            text_length = 0
            for page in doc:
                text_length += len(page.get_text().strip())
                if text_length > 100:  break

            if text_length > 100:
                print(f"--- (检测) PDF 是【文本型】(找到 {text_length} 字符) ---\n")
                return "TEXT_PDF"
            else:
                print(f"--- (检测) PDF 是【扫描型】(仅找到 {text_length} 字符) ---\n")
                return "SCANNED_PDF"
        except Exception as e:
            print(f"【PyMuPDF 检测错误】: {e}\n")
            return "ERROR"
        finally:
            if doc:
                doc.close()

    # --- 10. (新) 后台“管道” (在线程中运行) ---
    def run_extraction_logic(self):
            """主执行函数 (“混合 VLM 实验室” PyMuPDF 版)"""
            try:
                # 1. 智能检测 PDF 类型
                pdf_type = self.detect_pdf_type(self.filepath)

                if pdf_type == "TEXT_PDF":
                    # (流程一) 文本型 PDF
                    print(f"--- (大脑) 正在使用 PyMuPDF 提取 {self.filepath} 的全部文本... ---\n")
                    doc = None
                    try:
                        doc = fitz.open(self.filepath)

                        # --------------------------------------------------
                        # (V24.1 修复)
                        # 错误：full_text_list = [page.get_text() for page in doc.pages]
                        # 修复：
                        full_text_list = [page.get_text() for page in doc.pages()]  # <-- 已添加 () 括号
                        # --------------------------------------------------

                        text = "\n".join(full_text_list)
                    except Exception as e:
                        print(f"【PyMuPDF 提取错误】: {e}\n")
                        return  # (在 finally 中重置按钮)
                    finally:
                        if doc: doc.close()

                    extracted_data = self.call_qwen_text_api(text)

                    # (新) 文本型只显示一次
                    self.root.after(0, self.display_results, extracted_data, "(文本型 PDF)")

                elif pdf_type == "SCANNED_PDF":
                    # (流程二) 扫描型 PDF
                    # (VLM 函数现在自己负责循环和打印)
                    vlm_success = self.call_qwen_vlm_api(self.filepath)

                    if vlm_success:
                        print("\n--- 所有 VLM 批处理任务均已尝试。 ---")
                    else:
                        print("\n--- VLM 任务处理中有错误发生。 ---")

                else:
                    print("--- PDF 处理失败，脚本终止。 ---")

                print("\n【任务结束】")

            except Exception as e:
                print(f"【未捕获的全局错误】: {e}\n")
            finally:
                # (不变) 无论成功还是失败，都重置按钮
                self.root.after(0, self.reset_button)

# --- 11. (新) 启动 Tkinter 应用 ---
if __name__ == "__main__":
    # (旧的 main() 已被移除)
    root = tk.Tk()
    app = App(root)
    root.mainloop()