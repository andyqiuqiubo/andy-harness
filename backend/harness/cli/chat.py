"""CLI 验证脚本: harness chat --provider <name> "你好"

用法:
    uv run python -m harness.cli.chat --provider deepseek --api-key <key> "你好"
    uv run python -m harness.cli.chat --provider qwen --api-key <key> "你好"
    uv run python -m harness.cli.chat --provider doubao --api-key <key> "你好"
    uv run python -m harness.cli.chat --provider custom --api-key <key> \\
        --base-url https://api.example.com/v1 --model my-model "你好"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# 确保项目根目录在 sys.path 中
backend_dir = str(Path(__file__).parent.parent.parent)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from harness.infra.crypto import APIKeyEncryptor, mask_api_key  # noqa: E402
from harness.modules.model_manager.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
    ProviderError,
)
from harness.modules.model_manager.provider_registry import (  # noqa: E402
    ProviderRegistry,
)

# 内置 provider 映射
BUILTIN_PROVIDERS: dict[str, type[OpenAICompatibleProvider]] = {}


def _register_builtins() -> None:
    """延迟导入并注册内置 provider。"""
    from plugins.provider_deepseek.main import DeepSeekProvider
    from plugins.provider_doubao.main import DoubaoProvider
    from plugins.provider_qwen.main import QwenProvider

    BUILTIN_PROVIDERS["deepseek"] = DeepSeekProvider
    BUILTIN_PROVIDERS["qwen"] = QwenProvider
    BUILTIN_PROVIDERS["doubao"] = DoubaoProvider


async def run_chat(
    provider_name: str,
    api_key: str,
    message: str,
    model: str | None = None,
    base_url: str | None = None,
    extra_params: str | None = None,
) -> None:
    """执行一次流式对话。"""
    registry = ProviderRegistry()

    # 解析额外参数
    params: dict[str, Any] | None = None
    if extra_params:
        try:
            params = json.loads(extra_params)
        except json.JSONDecodeError:
            print(f"错误: extra_params 不是合法 JSON: {extra_params}", file=sys.stderr)
            return

    if provider_name in BUILTIN_PROVIDERS:
        provider_class = BUILTIN_PROVIDERS[provider_name]
        config: dict[str, Any] = {
            "name": provider_name,
            "api_key": api_key,
            "base_url": base_url or provider_class.base_url,
            "models": provider_class.default_models,
        }
        if params:
            config["extra_params"] = params
        registry.register_provider(provider_name, provider_class, config)
    else:
        # 自定义 provider
        from harness.modules.model_manager.openai_compatible import (
            OpenAICompatibleProvider as BaseProvider,
        )

        class CustomProvider(BaseProvider):
            base_url = base_url or ""
            default_models = [model or "gpt-4o"]
            provider_name = provider_name

        config = {
            "name": provider_name,
            "api_key": api_key,
            "base_url": base_url,
            "models": [model] if model else [],
        }
        if params:
            config["extra_params"] = params
        registry.register_provider(provider_name, CustomProvider, config)

    # 获取 provider 实例
    try:
        provider = registry.get_provider(provider_name)
    except Exception as e:
        print(f"错误: 无法获取 provider: {e}", file=sys.stderr)
        return

    # 使用第一个模型（如果未指定）
    if not model:
        models = await provider.list_models()
        if models:
            model = models[0]
        else:
            model = "gpt-4o"

    print(f"Provider: {provider_name}")
    print(f"Model: {model}")
    print(f"API Key: {mask_api_key(api_key)}")
    print(f"Base URL: {provider.base_url}")
    print("---")

    # 流式输出
    try:
        async for chunk in provider.chat(
            messages=[{"role": "user", "content": message}],
            model=model,
            stream=True,
        ):
            if "delta" in chunk:
                print(chunk["delta"], end="", flush=True)
            if "reasoning_content" in chunk:
                print(f"\n[思维链] {chunk['reasoning_content']}", end="", flush=True)
        print("\n---\n对话完成。")
    except ProviderError as e:
        print(f"\n错误: {e}", file=sys.stderr)


def main() -> None:
    """CLI 入口。"""
    _register_builtins()

    parser = argparse.ArgumentParser(
        description="andy-harness CLI — 流式对话验证"
    )
    parser.add_argument(
        "message",
        help="对话内容",
    )
    parser.add_argument(
        "--provider",
        required=True,
        help="Provider 名称（deepseek / qwen / doubao / 自定义名称）",
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="API Key",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="模型名称（不指定则使用 provider 默认模型）",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="API Base URL（自定义 provider 时必填）",
    )
    parser.add_argument(
        "--extra-params",
        default=None,
        help="额外参数（JSON 字符串）",
    )
    parser.add_argument(
        "--encrypt-test",
        action="store_true",
        help="测试 API Key 加密（不发送请求）",
    )

    args = parser.parse_args()

    if args.encrypt_test:
        # 测试加密
        encryptor = APIKeyEncryptor()
        encrypted = encryptor.encrypt(args.api_key)
        print(f"原始 Key: {mask_api_key(args.api_key)}")
        print(f"加密后: {encrypted}")
        print(f"解密后: {mask_api_key(encryptor.decrypt(encrypted))}")
        print("加密/解密测试通过。")
        return

    asyncio.run(
        run_chat(
            provider_name=args.provider,
            api_key=args.api_key,
            message=args.message,
            model=args.model,
            base_url=args.base_url,
            extra_params=args.extra_params,
        )
    )


if __name__ == "__main__":
    main()
