#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mindmap — 一句话/一个知识点生成可拖拽的思维导图（Markdown → markmap HTML）

用法：
    python mindmap.py "勾股定理"
    python mindmap.py "讲讲二分查找" -o output.html
    echo "一段教材文字..." | python mindmap.py -o from_text.html

配置：同目录 .env（复用视觉桥的键，可把现有 .env 直接复制过来）
    VISION_PROVIDER  = glm | deepseek | openai | dashscope | moonshot | siliconflow | ollama | custom
    VISION_API_KEY   = 你的 API Key
    VISION_MODEL     = 模型名
    VISION_BASE_URL  = 接口地址（custom 必填）
    GLM_API_KEY      = 兼容别名（provider=glm 时）

输出：一个自包含 HTML（双击打开即可拖拽/缩放/折叠），用 markmap 渲染。
"""

import argparse
import base64
import html
import json
import os
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 服务商注册表（与视觉桥一致，均走 OpenAI 兼容 /chat/completions）
# ---------------------------------------------------------------------------
PROVIDERS = {
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4.7", "glm-4.7-thinking"],
        "needs_key": True,
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "needs_key": True,
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o"],
        "needs_key": True,
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus"],
        "needs_key": True,
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["kimi-k2-0711-preview"],
        "needs_key": True,
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["Qwen/Qwen3-Coder"],
        "needs_key": True,
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "models": ["qwen2.5-coder:14b"],
        "needs_key": False,
    },
    "custom": {
        "base_url": "",
        "models": [],
        "needs_key": True,
    },
}


def read_dotenv():
    """读取本脚本同目录 .env，返回 dict。"""
    env = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def resolve_config(env, args):
    """解析出 provider / key / model / base_url。"""
    provider = args.provider or env.get("VISION_PROVIDER", "glm").lower()
    if provider not in PROVIDERS:
        sys.exit(f"错误：不支持的 provider：{provider}。可选：{', '.join(PROVIDERS)}")

    p = PROVIDERS[provider]

    api_key = args.api_key or env.get("VISION_API_KEY") or env.get("GLM_API_KEY")
    model = args.model or env.get("VISION_MODEL") or (p["models"][0] if p["models"] else "gpt-4o")
    base_url = args.base_url or env.get("VISION_BASE_URL") or p["base_url"]
    if not base_url:
        sys.exit("错误：未配置 VISION_BASE_URL（custom 服务商必须填）。")

    if p["needs_key"] and not api_key:
        sys.exit("错误：未配置 API Key。请先配置 .env（VISION_API_KEY 或 GLM_API_KEY）。")

    return provider, api_key, model, base_url


def chat(provider, api_key, model, base_url, prompt, system="你是生成思维导图的专家。"):
    """调用 OpenAI 兼容 /chat/completions。"""
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

    try:
        resp = opener.open(req, timeout=110)
        data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", "ignore")[:300]
        if e.code in (401, 403):
            sys.exit(f"认证失败 ({e.code})：请检查 API Key。{body_err}")
        if e.code == 429:
            sys.exit("请求过于频繁 (429)：请稍后重试，或换模型/时段。")
        sys.exit(f"HTTP {e.code}: {body_err}")
    except Exception as e:
        sys.exit(f"网络/请求错误：{e}")


# ---------------------------------------------------------------------------
# Prompt 构造
# ---------------------------------------------------------------------------
def build_prompt(topic, extra=""):
    """让模型输出层级清晰的 Markdown 大纲。"""
    return f"""请把下面的主题整理成一张思维导图，要求：

1. 用 Markdown 标题表达层级：# 是中心主题，## 是一级分支，### 是二级分支；
2. 叶子节点用 `- ` 列表项表示，措辞要**简短精炼**（关键词，不要长句）；
3. 层次 3~5 层，分支数 3~8 个，逻辑清晰、覆盖全面；
4. 不要输出任何解释性文字，只输出 Markdown 本身。

主题：{topic}

{extra}"""


# ---------------------------------------------------------------------------
# HTML 模板（markmap 渲染，可拖拽/缩放/折叠）
# ---------------------------------------------------------------------------
def render_html(markdown_text, title="智绘工坊 · 思维导图"):
    """把 Markdown 包进一个自包含 HTML，用 markmap-autoloader 渲染。"""
    # 注意：脚本内容要转义 </script>
    escaped = markdown_text.replace("</script>", "<\\/script>")
    title_h = html.escape(title)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_h}</title>
<style>
  html, body {{ margin: 0; height: 100%; overflow: hidden; background: #fff; }}
  .markmap {{ width: 100vw; height: 100vh; }}
</style>
</head>
<body>
<div class="markmap">
<script type="text/template">
{escaped}
</script>
</div>
<script src="https://cdn.jsdelivr.net/npm/markmap-autoloader@0.18"></script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="一句话/知识点 → 可拖拽思维导图 HTML")
    ap.add_argument("topic", nargs="?", help="要生成导图的主题或一句话")
    ap.add_argument("-o", "--output", default="mindmap.html", help="输出 HTML 文件路径（默认 mindmap.html）")
    ap.add_argument("-t", "--title", default="", help="HTML 标题（默认取主题前 40 字）")
    ap.add_argument("-p", "--provider", help="覆盖 .env 的 VISION_PROVIDER")
    ap.add_argument("-k", "--api-key", help="覆盖 .env 的 API Key")
    ap.add_argument("-m", "--model", help="覆盖 .env 的 VISION_MODEL")
    ap.add_argument("-b", "--base-url", help="覆盖 .env 的 VISION_BASE_URL")
    args = ap.parse_args()

    # 主题来源：命令行参数 或 标准输入
    if args.topic:
        topic = args.topic
    else:
        topic = sys.stdin.read().strip()
    if not topic:
        ap.error("请提供主题：python mindmap.py \"勾股定理\" 或通过管道输入文本")

    env = read_dotenv()
    provider, api_key, model, base_url = resolve_config(env, args)
    print(f"[mindmap] provider={provider} model={model}")

    # 1. 调模型生成大纲
    print("[mindmap] 正在生成思维导图大纲 ...")
    prompt = build_prompt(topic)
    markdown = chat(provider, api_key, model, base_url, prompt)
    if not markdown:
        sys.exit("错误：模型返回为空。")

    # 2. 渲染成 HTML
    title = args.title or (topic[:40] if len(topic) > 40 else topic)
    out_html = render_html(markdown, title)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(out_html)

    print(f"[mindmap] 已生成：{os.path.abspath(args.output)}")
    print("[mindmap] 双击打开，即可拖拽 / 缩放 / 折叠节点。")


if __name__ == "__main__":
    main()
