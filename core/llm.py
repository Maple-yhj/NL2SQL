from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI


@runtime_checkable
class LLMProtocol(Protocol):
    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
    ) -> str:
        ...


class LangChainLLM:
    """Small compatibility adapter around a LangChain chat model."""

    def __init__(self, model) -> None:
        self.model = model

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
    ) -> str:
        messages = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=prompt))
        response = await self.model.ainvoke(messages)
        content = getattr(response, "content", response)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            ).strip()
        return str(content).strip()


def create_llm() -> LLMProtocol:
    """Create the configured LangChain chat model behind the project protocol."""

    load_dotenv()
    provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    if not provider:
        provider = "deepseek" if os.getenv("DEEPSEEK_API_KEY") else "google"

    model_name = os.getenv("DEFAULT_MODEL_NAME", "").strip()
    if provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("Missing DEEPSEEK_API_KEY.")
        model = ChatOpenAI(
            model=model_name or "deepseek-chat",
            api_key=api_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            temperature=0,
        )
        return LangChainLLM(model)

    if provider == "google":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("Missing GEMINI_API_KEY.")
        model = ChatGoogleGenerativeAI(
            model=model_name or "gemini-2.5-flash",
            api_key=api_key,
            temperature=0,
        )
        return LangChainLLM(model)

    raise ValueError(f"Unsupported LLM_PROVIDER: {provider}")
