"""Minimal non-streaming demo for DeepSeek, Kimi/Moonshot, or GLM."""

from __future__ import annotations

import argparse
import asyncio
import os

from agent.core.messages import Message, TextBlock
from agent.model.openai_compatible import OpenAICompatibleProvider
from agent.model.presets import create_provider_config
from agent.model.types import LLMRequest


ENVIRONMENT_KEY_NAMES = {
    "deepseek": "DEEPSEEK_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "glm": "ZHIPUAI_API_KEY",
}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=ENVIRONMENT_KEY_NAMES, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", help="Optional endpoint override")
    args = parser.parse_args()

    api_key = os.environ.get(ENVIRONMENT_KEY_NAMES[args.provider])
    if not api_key:
        parser.error(f"Set {ENVIRONMENT_KEY_NAMES[args.provider]} before running this demo")

    config = create_provider_config(
        args.provider,
        api_key=api_key,
        model=args.model,
        base_url=args.base_url,
    )
    provider = OpenAICompatibleProvider(config)
    history = [
        Message(role="user", content=[TextBlock(text="你好，请简单介绍你自己")])
    ]
    response = await provider.chat(LLMRequest(messages=history))
    print(response)


if __name__ == "__main__":
    asyncio.run(main())
