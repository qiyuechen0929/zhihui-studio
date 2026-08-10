#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mindmap web server — 在浏览器里输入一句话，生成可拖拽思维导图。

启动：  python server.py
访问：  http://localhost:8000/

配置：同目录 .env（复用视觉桥的键，可把现有 .env 直接复制过来）
    VISION_PROVIDER  = glm | deepseek | openai | dashscope | moonshot | siliconflow | ollama | custom
    VISION_API_KEY   = 你的 API Key
    VISION_MODEL     = 模型名
    VISION_BASE_URL  = 接口地址（custom 必填）
    GLM_API_KEY      = 兼容别名（provider=glm 时）
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 服务商注册表（与视觉桥一致，均走 OpenAI 兼容 /chat/completions）
# ---------------------------------------------------------------------------
PROVIDERS = {
    "glm": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-flash", "glm-4.7-flash", "glm-4-flashx", "glm-4.7", "glm-5", "glm-5.1", "glm-5.2", "glm-4.7-thinking"],
        "needs_key": True,
        "free": ["glm-4-flash", "glm-4.7-flash"],
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash", "deepseek-v4-pro"],
        "needs_key": True,
        "free": [],
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"],
        "needs_key": True,
        "free": [],
    },
    "dashscope": {
        "name": "阿里云百炼 (Qwen)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max", "qwen3-coder-plus", "qwen3-coder-flash", "qwen-flash"],
        "needs_key": True,
        "free": ["qwen-flash"],
    },
    "moonshot": {
        "name": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["kimi-k2.6", "kimi-k2.5", "kimi-k2.5-lite", "kimi-k2-0711-preview"],
        "needs_key": True,
        "free": [],
    },
    "siliconflow": {
        "name": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["Pro/zai-org/GLM-5.1", "Pro/zai-org/GLM-4.7", "siliconflow/DeepSeek-V3.2", "siliconflow/DeepSeek-R1", "Qwen/Qwen3-Coder", "THUDM/GLM-4-9B-Chat"],
        "needs_key": True,
        "free": ["Qwen/Qwen3-Coder", "THUDM/GLM-4-9B-Chat"],
    },
    "ollama": {
        "name": "Ollama (本地免费)",
        "base_url": "http://localhost:11434/v1",
        "models": ["qwen2.5-coder:14b", "qwen3:8b", "deepseek-coder:6.7b", "llama3.1:8b"],
        "needs_key": False,
        "free": ["*"],
    },
    "custom": {
        "name": "自定义 (OpenAI 兼容)",
        "base_url": "",
        "models": [],
        "needs_key": True,
        "free": [],
    },
}

# 配置文件路径（打包为 exe 后定位到 exe 所在目录，保证可写、与源码方式一致）
if getattr(sys, "frozen", False):  # PyInstaller 打包
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")


def _setup_console():
    """Windows 控制台默认 GBK，无法编码部分字符（如 ⚠️ emoji），print 会直接抛
    UnicodeEncodeError 导致启动崩溃。这里把 stdout/stderr 的错误处理改为 replace，
    只影响个别无法显示的字符，不影响中文正常显示。

    打包为 GUI 程序时 stdout/stderr 可能为 None，需兼容跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure:
                reconfigure(errors="replace")
        except Exception:
            pass


def log(*parts):
    """安全打印（GUI 打包后 stdout 可能为 None，直接 print 会抛异常）。"""
    try:
        if sys.stdout is None:
            return
        print(*parts)
    except Exception:
        pass


def read_config():
    """读取用户配置 config.json；若不存在则返回空 dict。"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def write_config(cfg):
    """保存用户配置到 config.json。"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def read_history(limit=100):
    """读取生成历史（最新在前）。"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                items = json.load(f)
                if isinstance(items, list):
                    return items[:limit]
        except Exception:
            pass
    return []


def append_history(topic, model, provider, node_count, elapsed):
    """追加一条生成历史，最多保留 200 条。"""
    items = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                items = json.load(f)
                if not isinstance(items, list):
                    items = []
        except Exception:
            items = []
    items.insert(0, {
        "id": int(time.time() * 1000),
        "topic": topic,
        "model": model,
        "provider": provider,
        "node_count": node_count,
        "elapsed": round(elapsed, 1),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    items = items[:200]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    return items


def delete_history(history_id):
    """按 id 删除一条历史记录，返回是否成功。"""
    if not os.path.exists(HISTORY_FILE):
        return False
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            items = json.load(f)
        if not isinstance(items, list):
            return False
        new_items = [it for it in items if it.get("id") != history_id]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(new_items, f, ensure_ascii=False, indent=2)
        return len(new_items) != len(items)
    except Exception:
        return False


def count_nodes(markdown):
    """粗略统计导图节点数（标题 + 列表项）。"""
    import re
    n = 0
    for line in markdown.split("\n"):
        line = line.strip()
        if not line or line.startswith("---") or line.startswith("markmap:"):
            continue
        if re.match(r"^#{1,4} ", line) or line.startswith("- "):
            n += 1
    return n


def read_dotenv():
    """读取 BASE_DIR 目录下 .env，返回 dict。"""
    env = {}
    path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def resolve_config(env):
    """解析出 provider / key / model / base_url。

    优先级：config.json（用户在个人中心配置的）> .env > 默认值。
    """
    cfg = read_config()
    provider = (cfg.get("provider") or env.get("VISION_PROVIDER", "glm")).lower()
    if provider not in PROVIDERS:
        raise RuntimeError(f"不支持的 provider：{provider}。可选：{', '.join(PROVIDERS)}")

    p = PROVIDERS[provider]
    api_key = cfg.get("api_key") or env.get("VISION_API_KEY") or env.get("GLM_API_KEY")
    model = cfg.get("model") or env.get("VISION_MODEL") or (p["models"][0] if p["models"] else "gpt-4o")
    base_url = cfg.get("base_url") or env.get("VISION_BASE_URL") or p["base_url"]
    if not base_url:
        raise RuntimeError("未配置 VISION_BASE_URL（custom 服务商必须填）。")
    if p["needs_key"] and not api_key:
        raise RuntimeError("未配置 API Key。请在右上角个人中心里填写。")
    return provider, api_key, model, base_url


def resolve_assistant_config(env):
    """解析 AI 助手的独立配置。

    如果用户在个人中心给 AI 助手配了独立模型，则用它；否则回落到主配置。
    这样 AI 助手可以用更快的模型，不影响导图生成。
    """
    cfg = read_config()
    a = cfg.get("assistant") or {}

    # 有独立配置：provider/model/key/base_url 任一被设置即视为启用
    if any(k in a and a[k] for k in ("provider", "model", "api_key", "base_url")):
        provider = (a.get("provider") or "glm").lower()
        if provider not in PROVIDERS:
            raise RuntimeError(f"不支持的 AI 助手 provider：{provider}")
        p = PROVIDERS[provider]
        api_key = a.get("api_key") or ""
        model = a.get("model") or (p["models"][0] if p["models"] else "gpt-4o")
        base_url = a.get("base_url") or p["base_url"]
        if p["needs_key"] and not api_key:
            raise RuntimeError("AI 助手未配置 API Key。请到个人中心「AI 助手配置」里填写。")
        return provider, api_key, model, base_url

    # 没有独立配置 -> 回落主配置
    return resolve_config(env)


def _api_error_message(e):
    """把上游模型接口的 HTTPError 翻译成用户能看懂的中文提示。

    DeepSeek/智谱等返回的 401/403 错误体里带英文（如 "Authentication Fails,
    Your api key is invalid"），直接把英文抛给用户很难懂。这里归类成几类常见
    情况并给出去个人中心重填/检查余额的引导。
    """
    code = getattr(e, "code", 0)
    raw = ""
    try:
        raw = e.read().decode("utf-8", "ignore")[:300]
    except Exception:
        pass
    detail = raw
    # 尝试提取上游 message 字段（OpenAI 兼容错误格式）
    try:
        parsed = json.loads(raw)
        detail = parsed.get("error", {}).get("message", "") or parsed.get("error", raw)
    except Exception:
        pass

    if code in (401, 403):
        return ("API Key 无效或已失效（服务商返回 401/403 认证失败），"
                f"请到右上角「个人中心」重新填写正确的 API Key。服务商提示：{detail}")
    if code == 429:
        return f"请求太频繁或额度用尽（429），请稍后再试，或到个人中心换个模型/Key。服务商提示：{detail}"
    return f"模型接口错误 {code}: {detail}"


def _send_model_error(handler, e):
    """发送模型上游错误的 JSON 响应。"""
    handler.send_json(502, {"error": _api_error_message(e)})


def chat(api_key, model, base_url, prompt, system="你是生成思维导图的专家。"):
    """调用 OpenAI 兼容 /chat/completions，返回文本。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
    }
    body = json.dumps(payload).encode("utf-8")
    uri = base_url.rstrip("/") + "/chat/completions"

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 禁用代理直连
    req = urllib.request.Request(uri, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    resp = opener.open(req, timeout=110)
    data = json.loads(resp.read().decode("utf-8"))
    message = data["choices"][0]["message"]
    content = (message.get("content") or "").strip()
    # 推理模型（如 deepseek-v4-flash / glm-4.7-thinking）有时把答案放在
    # reasoning_content，content 为空。此时回退取推理内容，避免返回空文本。
    if not content:
        content = (message.get("reasoning_content") or "").strip()
    return content


def chat_vision(api_key, model, base_url, prompt, image_b64, mime="image/png"):
    """调用支持视觉的模型，传入图片(base64) + 文字问题，返回文本。"""
    content = [{"type": "text", "text": prompt}]
    if image_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{image_b64}"},
        })
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.3,
    }
    body = json.dumps(payload).encode("utf-8")
    uri = base_url.rstrip("/") + "/chat/completions"

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 禁用代理直连
    req = urllib.request.Request(uri, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    resp = opener.open(req, timeout=110)
    data = json.loads(resp.read().decode("utf-8"))
    message = data["choices"][0]["message"]
    content = (message.get("content") or "").strip()
    if not content:
        content = (message.get("reasoning_content") or "").strip()
    return content


def build_prompt(topic, mode="fast"):
    """按模式生成思维导图。mode: fast | standard | detailed"""
    if mode == "fast":
        structure = """- 一级分支：3~5 个；
- 层级深度：2~3 层；
- 总节点数：12~20 个；
- 每个分支展开到二级或三级即可；"""
    elif mode == "detailed":
        structure = """- 一级分支：6~9 个；
- 层级深度：3~5 层；
- 总节点数：40~70 个；
- 每个一级分支都尽量展开到三级或更深；"""
    else:  # standard
        structure = """- 一级分支：4~6 个；
- 层级深度：2~4 层；
- 总节点数：20~40 个；
- 每个一级分支展开到三级或更深；"""

    return f"""请把下面的主题整理成一张思维导图，要求：

1. 用 Markdown 标题表达层级：# 中心主题，## 一级分支，### 二级分支，#### 三级分支；
2. 叶子节点用 `- ` 列表项表示，措辞精炼（2~6 字），但含义要具体；
3. **结构要求（严格按此执行）**：
{structure}
4. 内容要有干货：概念、分类、原理、特点、步骤、例子、应用等，按主题合理展开；
5. 不要输出任何解释性文字，只输出 Markdown 本身，不要带代码块围栏。

主题：{topic}"""


def build_improve_prompt(markdown, feedback):
    """根据用户反馈，优化现有的思维导图 Markdown。"""
    return f"""我有一张思维导图，用 Markdown 标题表达层级（# 中心主题，## 一级分支，### 二级分支，#### 三级分支），叶子节点用 `- ` 列表项表示。

用户对这张图提出了改进意见，请你**根据意见优化这张思维导图**，要求：
1. 保持中心主题不变，保留原有好的结构；
2. 针对用户的意见做针对性改进（调整层级、补充内容、删减冗余、修正措辞等）；
3. 仍要满足：一级分支 4~8 个，层级 3~5 层，总节点尽量丰富；
4. 不要输出任何解释性文字，只输出优化后的 Markdown 本身，不要带代码块围栏。

用户意见：{feedback}

当前思维导图：
{markdown}"""


def _strip_fence(markdown):
    """清理可能由模型输出的 ```markdown ... ``` 围栏。"""
    markdown = markdown.strip()
    if markdown.startswith("```"):
        lines = markdown.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        markdown = "\n".join(lines).strip()
    return markdown


def _extract_topic(markdown):
    """从导图 Markdown 里提取中心主题（第一个 # 标题）。"""
    for line in markdown.split("\n"):
        line = line.strip()
        if line.startswith("# ") and not line.startswith("##"):
            return line[2:].strip()
    # 取第一行非空内容
    for line in markdown.split("\n"):
        if line.strip():
            return line.strip()[:30]
    return "思维导图"


def _inject_markmap_frontmatter(markdown):
    """在 Markdown 顶部注入 markmap YAML 渲染参数。

    markmap-autoloader 支持 frontmatter 里的 `markmap` 字段来覆盖渲染选项。
    maxWidth 默认约 140，中文长文本会被截断成单字再被裁剪，这里调大到 300，
    让"整数/浮点/字符"这类词能完整显示。
    """
    yaml_block = "---\nmarkmap:\n  maxWidth: 220\n  initialExpandLevel: 3\n  spacingVertical: 10\n  spacingHorizontal: 100\n  colorFreezeLevel: 3\n---\n\n"
    # 如果已经带 frontmatter（以 --- 开头），就替换/插入 markmap 字段前的占位
    if markdown.startswith("---"):
        return markdown  # 模型一般不会输出 frontmatter，安全起见直接返回
    return yaml_block + markdown


# ---------------------------------------------------------------------------
# HTTP Handler：静态文件 + /api/generate
# ---------------------------------------------------------------------------
# 静态资源（index.html、lib/）目录：打包后内置在 exe 的临时解压目录 _MEIPASS 中；
# 源码运行时就在项目目录。
if getattr(sys, "frozen", False):
    STATIC_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    STATIC_DIR = BASE_DIR


def _repair_chart_option(option):
    """补全模型返回的 ECharts option 中缺失的必要结构。

    模型常漏掉 bar/line/scatter 必需的 xAxis/yAxis，或数据格式不标准，
    直接 setOption 会抛 "Cannot read properties of undefined (reading 'get')"。
    这里做最小修补：按 series 类型补齐坐标轴，并规范化数据。
    """
    if not isinstance(option, dict):
        return option
    series = option.get("series")
    if not isinstance(series, list) or not series:
        return option

    # 确定主系列类型
    s = next((x for x in series if isinstance(x, dict) and x.get("type")), series[0] if isinstance(series[0], dict) else {})
    stype = s.get("type", "bar") if isinstance(s, dict) else "bar"

    def _norm_val(item):
        """把 {name, value} 或 value 统一成 ECharts 认识的格式。"""
        if isinstance(item, dict):
            return item
        return item

    if stype in ("bar", "line"):
        # 需要 xAxis(y) + yAxis(值)。从数据里提取类别名。
        cats = []
        for it in series:
            if not isinstance(it, dict):
                continue
            for d in (it.get("data") or []):
                if isinstance(d, dict) and d.get("name") and d["name"] not in cats:
                    cats.append(d["name"])
        if not option.get("xAxis"):
            if cats:
                option["xAxis"] = {"type": "category", "data": cats}
            else:
                n = len((s.get("data") or [])) if isinstance(s, dict) else 0
                option["xAxis"] = {"type": "category", "data": [f"第{i+1}项" for i in range(max(n, 1))]}
        if not option.get("yAxis"):
            option["yAxis"] = {"type": "value"}
        # 若数据是 {name, value} 且 xAxis 已是分类，转为 [值] 数组（配合 axis.data）
        for it in series:
            if isinstance(it, dict) and isinstance(it.get("data"), list):
                d0 = it["data"]
                if d0 and all(isinstance(x, dict) and "value" in x for x in d0) and option.get("xAxis", {}).get("data"):
                    it["data"] = [x["value"] for x in d0]

        # 组合图（柱状+折线）优化：两个系列数值量级差异大时（如销量 1200 vs 增长率 10），
        # 折线会贴底看不清。给折线系列配第二条 y 轴，并关联到该系列。
        if len(series) > 1:
            types_map = {}
            for it in series:
                if isinstance(it, dict):
                    types_map.setdefault(it.get("type"), []).append(it)
            has_bar = "bar" in types_map
            has_line = "line" in types_map
            if has_bar and has_line:
                # 已有第二个 yAxis（数组）就不重复加
                y2 = None
                if isinstance(option.get("yAxis"), list) and len(option["yAxis"]) >= 2:
                    y2 = option["yAxis"][1]
                else:
                    # 把单对象 yAxis 转成数组，补第二条
                    y1 = option.get("yAxis") or {"type": "value"}
                    y2 = {"type": "value", "splitLine": {"show": False}}
                    option["yAxis"] = [y1, y2]
                # 折线系列关联第二条 y 轴
                for it in types_map.get("line", []):
                    if isinstance(it, dict):
                        it["yAxisIndex"] = 1
    elif stype == "scatter":
        if not option.get("xAxis"):
            option["xAxis"] = {"type": "value"}
        if not option.get("yAxis"):
            option["yAxis"] = {"type": "value"}
    # pie 无需坐标轴，但确保 data 是 {name,value} 数组
    return option


class Handler(SimpleHTTPRequestHandler):
    server_version = "MindmapServer/1.0"
    # 静态文件根目录 = 打包资源目录
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def do_GET(self):
        path = self.path.rstrip("/") or "/"
        if path == "/api/config":
            self.send_json(200, read_config())
            return
        if path == "/api/history":
            self.send_json(200, read_history())
            return
        if path == "/api/providers":
            # 返回服务商列表（不含 key），供前端渲染配置界面
            info = {
                k: {
                    "name": v["name"],
                    "models": v["models"],
                    "needs_key": v["needs_key"],
                    "free": v.get("free", []),
                }
                for k, v in PROVIDERS.items()
            }
            self.send_json(200, info)
            return
        super().do_GET()

    def do_POST(self):
        path = self.path.rstrip("/") or "/"
        if path == "/api/generate":
            self.handle_generate()
            return
        if path == "/api/improve":
            self.handle_improve()
            return
        if path == "/api/config":
            self.handle_save_config()
            return
        if path == "/api/learn":
            self.handle_learn()
            return
        if path == "/api/chart":
            self.handle_chart()
            return
        self.send_json(404, {"error": "Not Found"})

    def do_DELETE(self):
        path = self.path.rstrip("/") or "/"
        if path.startswith("/api/history/"):
            try:
                hid = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_json(400, {"error": "无效的历史记录 id"})
                return
            ok = delete_history(hid)
            self.send_json(200, {"ok": ok})
            return
        self.send_json(404, {"error": "Not Found"})

    def handle_save_config(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            cfg = read_config()
            # 只允许保存白名单字段（主配置）
            for key in ("provider", "api_key", "model", "base_url"):
                if key in data:
                    cfg[key] = str(data[key]).strip()
            # AI 助手独立配置（嵌套在 assistant 下）
            if "assistant" in data and isinstance(data["assistant"], dict):
                a = cfg.get("assistant") or {}
                for key in ("provider", "api_key", "model", "base_url"):
                    if key in data["assistant"]:
                        a[key] = str(data["assistant"][key]).strip()
                cfg["assistant"] = a
            write_config(cfg)
            self.send_json(200, {"ok": True, "config": cfg})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def handle_generate(self):
        t0 = time.time()
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            topic = (data.get("topic") or "").strip()
            if not topic:
                self.send_json(400, {"error": "请输入一句话或知识点。"})
                return
            mode = data.get("mode") or "fast"
            if mode not in ("fast", "standard", "detailed"):
                mode = "fast"

            try:
                provider, api_key, model, base_url = resolve_config(env)
            except RuntimeError as e:
                # 未配置 Key 等配置错误 -> 401，前端据此引导去个人中心
                self.send_json(401, {"error": str(e), "need_config": True})
                return
            markdown = chat(api_key, model, base_url, build_prompt(topic, mode))
            markdown = _strip_fence(markdown)
            # 注入 markmap YAML frontmatter：调大 maxWidth 防止中文长文本被截断成单字，
            # 同时让首屏 fit 到合适大小（不遮住）。
            markdown = _inject_markmap_frontmatter(markdown)
            elapsed = time.time() - t0
            node_count = count_nodes(markdown)
            # 保存历史
            try:
                append_history(topic, model, provider, node_count, elapsed)
            except Exception:
                pass
            self.send_json(200, {
                "markdown": markdown,
                "model": model,
                "provider": provider,
                "node_count": node_count,
                "elapsed": round(elapsed, 1),
            })
        except urllib.error.HTTPError as e:
            _send_model_error(self, e)
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def handle_improve(self):
        """AI 助手：根据用户反馈优化思维导图。（一次调用，同时返回建议+新导图）"""
        t0 = time.time()
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            markdown = (data.get("markdown") or "").strip()
            feedback = (data.get("feedback") or "").strip()
            if not markdown:
                self.send_json(400, {"error": "缺少思维导图内容。"})
                return
            if not feedback:
                self.send_json(400, {"error": "请先输入你想怎么改进这张图。"})
                return

            try:
                provider, api_key, model, base_url = resolve_assistant_config(env)
            except RuntimeError as e:
                self.send_json(401, {"error": str(e), "need_config": True})
                return

            # 单次调用：让模型一次返回"建议" + 分隔符 + "新导图"，避免两次串行调用（省一半时间）
            one_call_prompt = f"""我有一张思维导图，用 Markdown 标题表达层级（# 中心主题，## 一级分支），叶子节点用 `- ` 列表项表示。

用户想改进：{feedback}

请分两部分输出，中间用一行【新导图】分隔：

第一部分（【建议】标题下）：给 2~4 条具体、简洁的改进建议，分点列出。
第二部分（【新导图】标题下）：直接输出根据建议优化后的完整 Markdown 导图，保持中心主题不变，不要带代码块围栏。

当前思维导图：
{markdown}"""
            raw_result = chat(api_key, model, base_url, one_call_prompt)
            raw_result = _strip_fence(raw_result)

            # 解析建议和新导图
            advice = ""
            improved = ""
            if "【新导图】" in raw_result:
                parts = raw_result.split("【新导图】", 1)
                advice = parts[0].replace("【建议】", "").strip()
                improved = parts[1].strip()
            else:
                # 模型没按格式输出，整个当建议，重新走一次"只生成新导图"
                advice = raw_result
                improved = chat(api_key, model, base_url, build_improve_prompt(markdown, feedback))

            if not improved:
                improved = markdown  # 兜底：保持原图

            improved = _inject_markmap_frontmatter(improved)

            elapsed = time.time() - t0
            node_count = count_nodes(improved)
            # 保存为新的历史记录
            try:
                append_history(_extract_topic(improved) + "（优化）", model, provider, node_count, elapsed)
            except Exception:
                pass

            self.send_json(200, {
                "advice": advice or "已根据你的反馈重新生成导图。",
                "markdown": improved,
                "model": model,
                "provider": provider,
                "node_count": node_count,
                "elapsed": round(elapsed, 1),
            })
        except urllib.error.HTTPError as e:
            _send_model_error(self, e)
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def handle_learn(self):
        """学习助手：解答题目/知识点，支持上传图片（视觉识别题目）。"""
        t0 = time.time()
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            question = (data.get("question") or "").strip()
            image_b64 = (data.get("image") or "").strip()   # base64 图片，可选
            mime = (data.get("mime") or "image/png").strip()

            if not question and not image_b64:
                self.send_json(400, {"error": "请输入问题，或上传一张题目图片。"})
                return

            # 图片识别：优先用主配置的 key，模型切到视觉模型（GLM-4V 系列）
            if image_b64:
                try:
                    provider, api_key, model, base_url = resolve_config(env)
                except RuntimeError as e:
                    self.send_json(401, {"error": str(e), "need_config": True})
                    return
                # 切到视觉模型：智谱 GLM-4V-Flash（免费/低价）
                vision_model = "glm-4v-flash"
                vision_prompt = ("请识别这张图片中的题目内容，并给出详细解答。"
                                 "如果图片是题目/作业，请：1) 提取题目原文 2) 分析解题思路 3) 给出步骤和答案。"
                                 "如果图片是知识点/笔记，请用通俗易懂的方式讲解。"
                                 + (f"\n用户补充说明：{question}" if question else ""))
                answer = chat_vision(api_key, vision_model, base_url, vision_prompt, image_b64, mime)
            else:
                # 纯文字解答：用 AI 助手的模型（若配了）或主模型
                try:
                    provider, api_key, model, base_url = resolve_assistant_config(env)
                except RuntimeError as e:
                    self.send_json(401, {"error": str(e), "need_config": True})
                    return
                learn_prompt = (f"你是一个耐心的学习辅导助手。请解答用户的问题，要求："
                                f"1) 先给出简洁的答案 2) 再分步讲解思路 3) 举一个例子帮助理解。\n\n"
                                f"【格式要求】如果涉及数学公式，必须用规范的 LaTeX：行内公式用 \\(...\\) 包裹，"
                                f"独立的公式块用 \\[...\\] 包裹（开头和结尾都要写全，不要只写一半）。"
                                f"不要输出任何解释格式的话。\n\n用户问题：{question}")
                answer = chat(api_key, model, base_url, learn_prompt,
                              system="你是耐心的学习辅导老师，擅长把复杂问题讲简单。")

            elapsed = time.time() - t0
            self.send_json(200, {
                "answer": answer,
                "model": model,
                "used_image": bool(image_b64),
                "elapsed": round(elapsed, 1),
            })
        except urllib.error.HTTPError as e:
            _send_model_error(self, e)
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def handle_chart(self):
        """其它图表：根据描述生成 ECharts 图表配置 或 mermaid 流程图。支持多轮对话修改。"""
        t0 = time.time()
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            description = (data.get("description") or "").strip()
            chart_type = (data.get("chart_type") or "auto").strip()
            existing = data.get("existing")   # 已有的图表配置（dict 或 None），用于修改

            if not description:
                self.send_json(400, {"error": "请描述你想生成的图表。"})
                return

            try:
                provider, api_key, model, base_url = resolve_config(env)
            except RuntimeError as e:
                self.send_json(401, {"error": str(e), "need_config": True})
                return

            # 判断是否在修改已有图表
            is_modify = bool(existing)
            existing_str = ""
            if is_modify:
                if existing.get("chart_type") == "flowchart":
                    existing_str = "已有 mermaid 流程图代码：\n" + (existing.get("mermaid") or "")
                elif existing.get("option"):
                    import json as _json
                    existing_str = "已有 ECharts option JSON：\n" + _json.dumps(existing.get("option"), ensure_ascii=False, indent=2)

            # 流程图画 mermaid
            is_flow = (chart_type == "flowchart" or
                       (chart_type == "auto" and not is_modify and any(k in description for k in ("流程", "流程图", "步骤", "架构"))))
            if is_flow:
                if is_modify:
                    prompt = (f"用户有一份已有的 mermaid 流程图，ta 提出修改意见：{description}\n\n"
                              f"请根据意见修改流程图，保持逻辑清晰，节点用中文，用 --> 或 -.-> 连接。"
                              f"只输出修改后的完整 mermaid 代码（从 flowchart 开头），不要任何解释。\n\n"
                              f"{existing_str}")
                else:
                    prompt = (f"请根据下面的描述，生成一个 mermaid 流程图（flowchart TD）。"
                              f"要求：节点用中文，逻辑清晰，用 --> 或 -.-> 连接。"
                              f"只输出 mermaid 代码（从 flowchart 开头），不要加任何解释或代码块围栏。\n\n描述：{description}")
                raw = chat(api_key, model, base_url, prompt,
                           system="你是专业的流程图设计专家，输出规范的 mermaid 语法。")
                raw = _strip_fence(raw)
                self.send_json(200, {
                    "chart_type": "flowchart",
                    "mermaid": raw,
                    "model": model,
                    "elapsed": round(time.time() - t0, 1),
                })
                return

            # ECharts 图表（饼图/折线/柱状/散点等），支持多轮修改
            # 用户在前端选的图表类型（pie/line/bar/scatter）作为强制约束传给模型，
            # 而不是让模型从描述里猜（否则用户选柱状图也会被生成成饼图）
            CHART_TYPE_NAMES = {
                "pie": "饼图",
                "line": "折线图",
                "bar": "柱状图",
                "scatter": "散点图",
                "auto": "",
            }
            chart_type_hint = CHART_TYPE_NAMES.get(chart_type, "")
            type_rule = ""
            if chart_type_hint:
                type_rule = (f"\n**必须使用 {chart_type_hint}**：series 的 type 必须是 "
                             f"\"{chart_type}\"（用户在前端已明确选择，不得改为其它图表类型）。")
            elif chart_type == "auto":
                type_rule = ("图表类型按描述推断：饼图用 pie、折线用 line、柱状用 bar、"
                             "散点用 scatter 等。")

            if is_modify:
                # 修改已有图表。类型策略：
                # - 用户描述里明确提到"柱状/折线/饼/散点"等类型词，或前端选了与原类型不同的
                #   类型按钮 -> 允许/要求切换类型（如"换成柱状图""再生成一张折线图"）
                # - 否则保持原类型，只改用户要求的部分（改标题/颜色/数据等）
                # 注意：existing.option 可能是 None（比如当前是流程图时），
                # 不能直接用 .get("option", {}).get(...)，None.get() 会崩
                _opt = (existing or {}).get("option") or {}
                s0 = _opt.get("series", [{}]) if isinstance(_opt, dict) else [{}]
                orig_type = ""
                if isinstance(s0, list) and s0 and isinstance(s0[0], dict) and s0[0].get("type"):
                    orig_type = s0[0]["type"]

                # 描述里出现的图表类型词
                type_words = {
                    "pie": ["饼", "pie"],
                    "line": ["折线", "曲线", "line"],
                    "bar": ["柱状", "条形", "柱形", "bar"],
                    "scatter": ["散点", "scatter"],
                }
                want_type = None
                for t, words in type_words.items():
                    if any(w in description for w in words):
                        want_type = t
                        break
                # 前端选了类型按钮，且与原类型不同 -> 视为要切类型
                if chart_type in ("pie", "line", "bar", "scatter") and chart_type != orig_type:
                    want_type = chart_type
                # 前端选了具体类型且描述也指向同一类型 -> 明确要求
                if chart_type in ("pie", "line", "bar", "scatter") and not want_type and chart_type == orig_type:
                    want_type = chart_type

                if want_type and want_type != orig_type:
                    # 用户明确要求换类型
                    type_hint = CHART_TYPE_NAMES.get(want_type, want_type)
                    type_rule2 = (f"\n**用户明确要求把图表改成 {type_hint}**：series 的 type 必须是 "
                                  f"\"{want_type}\"（原类型是 \"{orig_type or '未知'}\"），请按新类型重新组织数据。")
                elif orig_type:
                    # 未要求换类型 -> 保持原类型，防止模型跑偏
                    type_rule2 = f"\n**必须保持 series 类型为 \"{orig_type}\"**，不要改成其它图表类型。"
                else:
                    type_rule2 = ""
                prompt = (f"用户有一份已有的 ECharts 配置，ta 提出修改意见：{description}\n\n"
                          f"要求：\n"
                          f"1. {type_rule2 or '按用户意见调整。'}\n"
                          f"2. 只按用户意见做针对性修改（改标题/数据/样式/类型/增加系列等），不要重造整张图；\n"
                          f"3. 保留原配置里与修改无关的合理部分（title/tooltip/legend/axis/series 数据等）；\n"
                          f"4. 只输出修改后的完整、合法的 ECharts option JSON 对象，不要任何解释或代码块围栏。\n\n"
                          f"{existing_str}")
            else:
                prompt = (f"请根据下面的描述，生成一个 ECharts 5 的图表配置。"
                          f"要求：只输出一个合法的 JSON 对象（ECharts option），不要输出任何解释、不要代码块围栏。"
                          f"option 里应包含：title（中文标题）、tooltip、legend、合适的 series（数据要合理、贴合描述）。"
                          f"{type_rule}\n\n描述：{description}")
            raw = chat(api_key, model, base_url, prompt,
                       system="你是专业的数据可视化工程师，只输出合法的 ECharts option JSON。")
            raw = _strip_fence(raw)
            # 尝试解析 JSON；失败则返回原始文本让前端提示
            import json as _json
            def _try_parse(s):
                try:
                    return _json.loads(s)
                except Exception:
                    return None
            option = _try_parse(raw)
            if option is None:
                # 去掉可能的 ```json 围栏
                cleaned = raw.replace("```json", "").replace("```", "").strip()
                option = _try_parse(cleaned)
            if option is None:
                # 提取第一个 {...} JSON 对象（模型可能夹带解释文字）
                import re as _re
                m = _re.search(r'\{.*\}', cleaned, _re.S)
                if m:
                    option = _try_parse(m.group(0))
            if option is None:
                self.send_json(200, {
                    "chart_type": "echarts",
                    "option_raw": raw,
                    "option": None,
                    "model": model,
                    "elapsed": round(time.time() - t0, 1),
                    "parse_error": True,
                })
                return

            # 兜底校验：用户明确要求了某种图表类型（描述里提到类型词 优先，其次选类型按钮），
            # 但模型返回的 series type 不符时，强制纠正（LLM 输出不稳定，prompt + 代码双保险）
            # 注意：组合图场景（"在柱状图基础上加一条折线"）不能把所有 series 强改成同一类型，
            # 否则新增的折线会被改成柱状。检测到组合意图时只校验第一个系列。
            is_combo = any(w in description for w in ("基础上加", "加一条", "增加一", "再加上", "再加", "组合", "同时显示", "叠加"))
            enforce_type = ""
            for t, words in (("bar", ["柱状", "条形", "柱形", "bar"]),
                             ("pie", ["饼", "pie"]),
                             ("line", ["折线", "曲线", "line"]),
                             ("scatter", ["散点", "scatter"])):
                if any(w in description for w in words):
                    enforce_type = t
                    break
            if not enforce_type and chart_type in ("pie", "line", "bar", "scatter"):
                enforce_type = chart_type
            if enforce_type:
                series = option.get("series")
                if isinstance(series, list) and series:
                    targets = series[:1] if is_combo else series
                    for s in targets:
                        if isinstance(s, dict) and s.get("type") and s["type"] != enforce_type:
                            s["type"] = enforce_type
                    # 组合图：确保新增系列里名字含"率/趋势/百分比"的折线保持 line 类型
                    if is_combo:
                        # 记录原图已有的系列类型（用于区分"新增系列"）
                        orig_types = []
                        _eo = (existing or {}).get("option")
                        if isinstance(_eo, dict):
                            for _es in (_eo.get("series") or []):
                                if isinstance(_es, dict) and _es.get("type"):
                                    orig_types.append(_es.get("type"))
                        want_line_add = "折线" in description
                        want_base_bar = any(w in description for w in ("柱状", "条形", "柱形", "柱状图"))
                        if chart_type == "bar" and is_combo:
                            want_base_bar = True

                        for idx, s in enumerate(series):
                            if not isinstance(s, dict):
                                continue
                            nm = (s.get("name") or "")
                            if idx < len(orig_types):
                                # 原有系列：保持原来的类型（bar 就 bar，line 就 line）
                                if s.get("type") != orig_types[idx] and orig_types[idx] in ("bar", "line", "pie", "scatter"):
                                    s["type"] = orig_types[idx]
                            elif want_line_add and any(k in nm for k in ("率", "趋势", "增长", "百分比")):
                                # 新增且名字含"率/增长" -> 折线
                                if s.get("type") == "bar":
                                    s["type"] = "line"
                            elif want_base_bar and want_line_add and s.get("type") == "line" and idx == 0:
                                # 组合图第一个系列应是柱状底色（仅当模型把它生成了 line）
                                s["type"] = "bar"
                        # 兜底：全系列都是 line 且用户要柱状+折线 -> 第一个拉回 bar
                        if want_base_bar and want_line_add and len(series) > 1 and \
                           all(isinstance(x, dict) and x.get("type") == "line" for x in series):
                            series[0]["type"] = "bar"

            # 结构补全：模型返回的 option 常缺 xAxis/yAxis（bar/line/scatter 必需），
            # 缺轴会导致 ECharts 抛 "Cannot read properties of undefined (reading 'get')"。
            option = _repair_chart_option(option)

            self.send_json(200, {
                "chart_type": "echarts",
                "option": option,
                "model": model,
                "elapsed": round(time.time() - t0, 1),
            })
        except urllib.error.HTTPError as e:
            _send_model_error(self, e)
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # 精简日志，方便看生成请求（GUI 模式下 stderr 可能为 None）
        try:
            if sys.stderr:
                sys.stderr.write("[mindmap] %s\n" % (fmt % args))
        except Exception:
            pass


def _find_free_port(preferred):
    """尝试用 preferred 端口，被占用则自动换一个可用端口，返回 (port, used_free)。"""
    for port in (preferred,) + tuple(p for p in range(preferred + 1, preferred + 20)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    return preferred


def _open_browser_later(port, delay=1.2):
    """延迟打开浏览器，等服务就绪。"""
    def _do():
        time.sleep(delay)
        try:
            webbrowser.open(f"http://localhost:{port}/")
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


# 全局环境配置（read_dotenv 结果），在 start_server() / main() 中初始化
env = {}


def start_server(port=8000, host="127.0.0.1", open_browser=False):
    """启动 HTTP 服务，返回 (server, port)。供桌面 app（pywebview）和命令行共用。"""
    _setup_console()
    global env
    env = read_dotenv()

    # 端口被占用时自动换一个可用端口（避免多开/残留进程导致启动失败）
    port = _find_free_port(port)
    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if open_browser:
        _open_browser_later(port)
    return server, port


def main():
    ap = argparse.ArgumentParser(description="mindmap web server")
    ap.add_argument("-p", "--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args()

    server, port = start_server(args.port, args.host, open_browser=not args.no_browser)
    log("=" * 50)
    log("  智绘工坊 已启动！")
    log(f"  请在浏览器打开:  http://localhost:{port}/")
    log("  按 Ctrl+C 停止服务")
    log("=" * 50)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[mindmap] 已停止。")


if __name__ == "__main__":
    main()
