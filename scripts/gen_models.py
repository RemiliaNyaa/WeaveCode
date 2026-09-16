"""从上游模型目录抽取模型表，生成运行时查表用的 data/models.json。

上游目录每家厂商有二十来个字段，本脚本只保留运行时真正要用的五个
（id / name / context / output / reasoning），并按白名单挑出 Anthropic 兼容的厂商。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

UPSTREAM_URL = "https://models.opencode.ai/api.json"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "src" / "weavecode" / "data" / "models.json"

# 厂商白名单（手工维护）：上游目录只记厂商自己的 OpenAI 兼容地址，
# Anthropic 兼容端点不在目录里，无法按字段自动推断，只能人工点名
PROVIDER_WHITELIST = (
    "anthropic",
    "deepseek",
    "moonshotai",
    "moonshotai-cn",
    "zhipuai",
    "zai",
    "minimax",
    "minimax-cn",
    "alibaba",
    "alibaba-cn",
    "volcengine",
    "tencent-tokenhub",
    "siliconflow",
    "siliconflow-cn",
    "bailing",
    "stepfun",
    "stepfun-ai",
)


# 拉取上游模型目录并解析成 dict
def fetch(source: str) -> dict[str, Any]:
    response = httpx.get(source, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    data: Any = response.json()
    if not isinstance(data, dict):
        raise SystemExit(f"unexpected upstream payload from {source}")
    return data


# 单个模型是否收进表：要支持工具调用、有上下文窗口，且不是免费档或路由模型
def _keep(model_id: str, model: Any) -> bool:
    if not isinstance(model, dict):
        return False
    if model_id.endswith(":free") or model.get("router"):
        return False
    if not model.get("tool_call"):
        return False
    context = model.get("context")
    return isinstance(context, int) and context > 0


# 按白名单抽取模型表，输出 {version, source, providers} 结构
def select(raw: dict[str, Any], version: str) -> dict[str, Any]:
    upstream = raw.get("providers")
    if not isinstance(upstream, dict):
        raise SystemExit("upstream payload has no providers table")

    providers: dict[str, Any] = {}
    kept = 0
    for key in PROVIDER_WHITELIST:
        entry = upstream.get(key)
        if not isinstance(entry, dict):
            print(f"skip provider {key}: not found upstream", file=sys.stderr)
            continue
        models: dict[str, Any] = {}
        raw_models = entry.get("models")
        candidates = raw_models if isinstance(raw_models, dict) else {}
        for model_id, model in candidates.items():
            if not _keep(model_id, model):
                continue
            assert isinstance(model, dict)
            models[str(model_id)] = {
                "name": str(model.get("name") or model_id),
                "context": int(model.get("context") or 0),
                "output": int(model.get("output") or 0),
                "reasoning": bool(model.get("reasoning")),
            }
        if models:
            providers[str(key)] = {"name": str(entry.get("name") or key), "models": models}
            kept += len(models)

    print(f"providers={len(providers)} models={kept}")
    return {"version": version, "source": UPSTREAM_URL, "providers": providers}


def main() -> int:
    parser = argparse.ArgumentParser(prog="gen_models", description="生成 data/models.json")
    parser.add_argument("--source", default=UPSTREAM_URL, help="上游模型目录地址")
    parser.add_argument("--input", default=None, help="改为读取本地目录文件，跳过网络请求")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT), help="输出文件路径")
    parser.add_argument("--version", default=None, help="写进表头的版本标识（默认当天日期）")
    args = parser.parse_args()

    if args.input:
        raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SystemExit(f"unexpected payload in {args.input}")
    else:
        raw = fetch(args.source)

    version = args.version or datetime.now(UTC).strftime("%Y-%m-%d")
    table = select(raw, version)
    if not table["providers"]:
        raise SystemExit("no provider survived the filters")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
