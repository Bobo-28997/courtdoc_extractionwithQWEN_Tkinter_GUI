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
from flask import Flask, request, jsonify  # (新) 导入 Flask

# --- 1. (关键) 服务器端加载 Keys 和 Prompt ---
load_dotenv()
API_KEY = "key"

if not API_KEY:
    print("【致命错误】: 后端服务器未能加载 'DASHSCOPE_API_KEY'。")
    sys.exit(1)

# --- 2. 初始化【两个】Qwen 客户端 (在服务器上) ---
QWEN_TEXT_CLIENT = OpenAI(
    api_key=API_KEY,
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)
QWEN_MODEL_NAME_TEXT = "qwen-plus-2025-01-25"
dashscope.api_key = API_KEY
QWEN_MODEL_NAME_VISION = "qwen-vl-max"

# --- 3. (V22 优化) VLM 处理常量 ---
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


# --- 6. (不变) Qwen-max (纯文本) API 调用 ---
def call_qwen_text_api(raw_text):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"法律文书原文(从PDF提取的纯文本):\n{raw_text}"}
    ]
    print(f"--- (大脑) 正在调用 {QWEN_MODEL_NAME_TEXT} (纯文本 API) ---")
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
        print(f"【Qwen 文本 API 错误】: {e}")
        return None


# --- 7. (重大修改) Qwen-VL-Max (VLM) API 调用 ---
# (此函数不再“打印”，而是“返回一个列表”)
def call_qwen_vlm_api(file_path):
    """
    (V22 逻辑 - V25 修改)
    1. 将 PDF 转为压缩 JPEG
    2. 分块
    3. 循环调用 VLM API
    4. (新) 将所有结果收集到一个列表中并返回
    """
    print(f"--- (眼睛) 正在使用 PyMuPDF+Pillow 将 {file_path} 转换为压缩 JPEG... ---")
    temp_files = []
    doc = None
    all_results = []  # (新) 用于收集所有 JSON 结果

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
            print("【VLM 错误】: 无法从 PDF 提取任何图像页面。")
            return None  # (返回 None 表示失败)

        print(f"--- (眼睛) 成功提取 {len(all_image_urls)} 页图像 (JPEG Quality={IMAGE_COMPRESSION_QUALITY}) ---")

        url_chunks = list(chunk_list(all_image_urls, VLM_PAGE_CHUNK_SIZE))
        print(f"--- (眼睛) 已将任务分为 {len(url_chunks)} 个批次 (每批 {VLM_PAGE_CHUNK_SIZE} 页) ---")

        for chunk_index, image_url_chunk in enumerate(url_chunks):
            chunk_name = f"批次 {chunk_index + 1}/{len(url_chunks)}"
            print(f"\n--- (眼睛) 正在处理 {chunk_name} ---")

            full_user_prompt_text = f"{SYSTEM_PROMPT}\n\n--- 真实任务：...请严格分析我提供的【这几页】图像..."
            content = [{"image": url} for url in image_url_chunk]
            content.append({"text": full_user_prompt_text})
            messages = [{"role": "user", "content": content}]

            response = None
            for attempt in range(MAX_RETRIES):
                try:
                    print(f"--- (眼睛) 正在调用 VLM API (Attempt {attempt + 1}/{MAX_RETRIES})... ---")
                    response = dashscope.MultiModalConversation.call(
                        model=QWEN_MODEL_NAME_VISION,
                        messages=messages,
                        temperature=0.0,
                        timeout=API_TIMEOUT
                    )
                    if response.status_code == 200:
                        break  # 成功
                    else:
                        print(
                            f"【VLM API 错误】 ({chunk_name}, Attempt {attempt + 1}): {response.code} - {response.message}")
                        if attempt < MAX_RETRIES - 1: time.sleep(5)
                except (requests.exceptions.ConnectionError, RemoteDisconnected, Exception) as e:
                    print(f"【VLM API 网络/SDK 错误】 ({chunk_name}, Attempt {attempt + 1}): {e}")
                    if attempt < MAX_RETRIES - 1: time.sleep(5)

            if response is None or response.status_code != 200:
                print(f"--- 批次 {chunk_name} 【失败】，已达到最大重试次数。---")
                all_results.append({"error": f"批次 {chunk_name} 处理失败。"})
                continue

            model_output_content = response.output.choices[0].message.content
            model_output_string = ""
            for item in model_output_content:
                if 'text' in item: model_output_string += item['text']

            json_match = model_output_string[model_output_string.find('{'): model_output_string.rfind('}') + 1]
            if not json_match:
                print(f"【VLM 错误】 ({chunk_name}): VLM 未返回有效的 JSON 结构。")
                all_results.append({"error": f"批次 {chunk_name} VLM 未返回 JSON。"})
                continue

            # (新) 收集结果，而不是打印
            all_results.append(json.loads(json_match))

        return all_results  # (新) 返回所有分块的结果列表

    except Exception as e:
        print(f"【VLM 图像转换/处理异常】: {e}")
        return None  # (返回 None 表示严重失败)
    finally:
        if doc: doc.close()
        print("--- (清理) 正在删除所有临时图像文件... ---")
        for tmp_file in temp_files:
            try:
                os.unlink(tmp_file)
            except:
                pass


# --- 8. (不变) PyMuPDF 检测器 ---
def detect_pdf_type(filepath):
    """(V20 - PyMuPDF 版) 检测 PDF 是“文本型”还是“扫描型”"""
    print(f"--- (检测) 正在使用 PyMuPDF 打开 {filepath} ---")
    doc = None
    try:
        doc = fitz.open(filepath)
        text_length = 0
        for page in doc.pages():  # (V24.1 修复) 已添加 ()
            text_length += len(page.get_text().strip())
            if text_length > 100:  break

        if text_length > 100:
            print(f"--- (检测) PDF 是【文本型】(找到 {text_length} 字符) ---")
            return "TEXT_PDF"
        else:
            print(f"--- (检测) PDF 是【扫描型】(仅找到 {text_length} 字符) ---")
            return "SCANNED_PDF"
    except Exception as e:
        print(f"【PyMuPDF 检测错误】: {e}")
        return "ERROR"
    finally:
        if doc:
            doc.close()


# --- 9. (新) Flask 服务器核心 ---
app = Flask(__name__)


# (V25.2 - 文件锁修复)
# 我们将替换整个 @app.route("/extract") 函数
@app.route("/extract", methods=["POST"])
def handle_extraction():
    """这是客户端将调用的唯一 API 端点 (已修复 Windows 文件锁)"""
    print("\n" + "=" * 50)
    print(f"--- 收到新的 /extract 请求 ---")

    if 'file' not in request.files:
        print("【请求错误】: 'file' 字段未在请求中找到。")
        return jsonify({"error": "请求中未包含 'file'。"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "未选择文件。"}), 400

    # --- (V25.2 修复开始) ---
    # 我们不再使用 'with' 语句
    # 1. 手动创建一个【不自动删除】的临时文件
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp_path = tmp.name

    try:
        # 2. 保存客户端上传的内容
        file.save(tmp_path)

        # 3. (关键!) 立即关闭文件句柄，【释放文件锁】
        tmp.close()

        print(f"--- 文件已临时保存到: {tmp_path} (文件锁已释放) ---")

        # 4. (现在可以安全调用了) 检测 PDF 类型
        #    (fitz.open(tmp_path) 现在可以成功打开文件了)
        pdf_type = detect_pdf_type(tmp_path)

        if pdf_type == "TEXT_PDF":
            # (流程一) 文本型 PDF
            print(f"--- (大脑) 正在使用 PyMuPDF 提取 {tmp_path} 的全部文本... ---")
            doc = fitz.open(tmp_path)
            text = "\n".join([page.get_text() for page in doc.pages()])
            doc.close()  # 再次确保关闭

            extracted_data = call_qwen_text_api(text)

            if extracted_data:
                return jsonify([extracted_data])
            else:
                raise Exception("Qwen-Text 未能返回有效数据。")

        elif pdf_type == "SCANNED_PDF":
            # (流程二) 扫描型 PDF
            # (call_qwen_vlm_api 也会打开和关闭 tmp_path)
            extracted_data_list = call_qwen_vlm_api(tmp_path)

            if extracted_data_list:
                return jsonify(extracted_data_list)
            else:
                raise Exception("Qwen-VLM 未能返回有效数据。")

        else:  # "ERROR"
            raise Exception("无法检测或读取 PDF。")

    except Exception as e:
        # 5. (不变) 捕获所有错误
        print(f"【服务器处理错误】: {e}")
        return jsonify({"error": str(e)}), 500

    finally:
        # 6. (关键!) 无论成功还是失败，【手动删除】临时文件
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
                print(f"--- 临时文件 {tmp_path} 已成功删除 ---")
            except Exception as e:
                print(f"【警告】: 无法删除临时文件 {tmp_path}: {e}")
    # --- (V25.2 修复结束) ---


# --- 10. (新) 启动服务器 ---
if __name__ == "__main__":
    print("--- 法律文书提取【后端服务器 V25】 ---")
    print(f"--- 正在加载 Qwen (Text: {QWEN_MODEL_NAME_TEXT}, VLM: {QWEN_MODEL_NAME_VISION}) ---")
    print(f"--- API Key: {API_KEY[:5]}...{API_KEY[-4:]} (已加载) ---")
    print("\n【警告】: 这是一个开发服务器。请勿在生产环境中使用。")
    # 监听 0.0.0.0 (所有内网 IP) 的 5000 端口
    app.run(host="0.0.0.0", port=5000, debug=True)