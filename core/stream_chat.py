from collections.abc import Callable
from dataclasses import dataclass

from core.google_client import get_client, get_model_name


@dataclass(slots=True)
class StreamResult:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class GeminiLLM:
    """Small async wrapper around Gemini streaming generation."""

    def __init__(self, client=None, model: str | None = None):
        self.client = client or get_client()
        self.model = model or get_model_name()

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        result = await self.stream(
            prompt=prompt,
            system=system,
            max_output_tokens=max_output_tokens,
            on_token=on_token,
        )
        return result.text

    async def stream(
        self,
        prompt: str,
        system: str = "",
        max_output_tokens: int = 2048,
        on_token: Callable[[str], None] | None = None,
    ) -> StreamResult:
        try:
            from google.genai import types
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "google-genai is not installed. Install it with: pip install google-genai"
            ) from exc

        config = types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=max_output_tokens,
        )

        parts: list[str] = []
        input_tokens = None
        output_tokens = None
        stream = await self.client.aio.models.generate_content_stream(
            model=self.model,
            contents=prompt,
            config=config,
        )

        async for chunk in stream:
            text = getattr(chunk, "text", None)
            if text:
                parts.append(text)
                if on_token:
                    on_token(text)

            usage = getattr(chunk, "usage_metadata", None)
            if usage:
                input_tokens = getattr(usage, "prompt_token_count", input_tokens)
                output_tokens = getattr(usage, "candidates_token_count", output_tokens)

        return StreamResult(
            text="".join(parts).strip(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


async def stream_gemini(client, prompt: str, system: str = "") -> StreamResult:
    llm = GeminiLLM(client=client)
    return await llm.stream(
        prompt=prompt,
        system=system,
        on_token=lambda text: print(text, end="", flush=True),
    )
