import os, time
import openai
from typing import Optional

def _log_raw(provider: str, model: str, raw: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[LLM_RAW] ts={ts} provider={provider} model={model} chars={len(raw or '')} raw={raw!r}")


def _usage_tokens(response) -> tuple:
    """Per-call (tokens_in, tokens_out) from a provider response, or (-1, -1) if unavailable.
    Handles both the Chat Completions shape (prompt_tokens/completion_tokens) and the
    Responses API shape (input_tokens/output_tokens). Never raises."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return (-1, -1)

    def pick(*names) -> int:
        for n in names:
            v = getattr(usage, n, None)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return -1
        return -1

    return (pick("prompt_tokens", "input_tokens"), pick("completion_tokens", "output_tokens"))


def _log_usage(provider: str, model: str, response) -> None:
    """Emit per-call token usage beside [LLM_RAW]. Cost is a primary stage-2 outcome and the
    raw-text line carries no usage; the gateway proxy logs bytes, not tokens."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    tokens_in, tokens_out = _usage_tokens(response)
    print(f"[LLM_USAGE] ts={ts} provider={provider} model={model} "
          f"tokens_in={tokens_in} tokens_out={tokens_out}")


class AbstractAIProvider:
    def __init__(self, name: str):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        raise NotImplementedError

    @property
    def is_available(self) -> bool:
        raise NotImplementedError

class AIProvider(AbstractAIProvider):
    """Lazy AI provider with on-demand initialization."""

    def __init__(self, name: str, var_name: str, model_name: str, base_url: str):
        super().__init__(name)
        self._var_name = var_name
        self._model_name = model_name
        self._base_url = base_url
        self._client = None  # lazy initialization

    def _ensure_client(self):
        """Initialize client on first use."""
        if self._client is None:
            self._client = self._create_client()

    def _create_client(self) -> Optional[openai.OpenAI]:
        """Create OpenAI client from environment."""
        proxy_url = os.environ.get("GATEWAY_URL")
        if proxy_url:
            prefix = self._name.lower()
            base_url = f"{proxy_url.rstrip('/')}/{prefix}/"
            print(f"[lib_llm_ext.AIProvider._create_client] Connecting via proxy: {base_url}")
            return openai.OpenAI(
                    api_key="proxy",
                    base_url=base_url,
                    )
        if self._var_name in os.environ:
            if self._var_name == "OLLAMA_API_KEY":
                llm_server_local_url = os.environ.get("LLM_SERVER_LOCAL_URL")
                if llm_server_local_url:
                    self._base_url = llm_server_local_url.rstrip("/") + "/v1"
                elif not self._base_url.endswith("/v1"):
                    self._base_url = self._base_url.rstrip("/") + "/v1"

            return openai.OpenAI(api_key=os.environ.get(self._var_name), base_url=self._base_url)

        return None

    @property
    def is_available(self) -> bool:
        """Check if provider is configured (without initializing)."""
        return bool(os.environ.get("GATEWAY_URL")) or bool(os.environ.get(self._var_name))

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        """Send chat request, initializing client if needed."""
        self._ensure_client()

        if self._client is None:
            raise RuntimeError(f"{self.name} not configured (set {self._var_name})")

        content = content.replace(":-:-:-:", " ")
        try:
            response = self._client.chat.completions.create(
                model=self._model_name,
                messages=[{"role": "user", "content": content}],
                max_tokens=max_tokens,
                **kwargs
            )

            raw = response.choices[0].message.content or ""
            _log_raw(self._name, self._model_name, raw)
            _log_usage(self._name, self._model_name, response)
            return self._clean_text(raw)
        except Exception as e:
            print(f"[lib_llm_ext.AIProvider.chat] Exception while communicating with LLM: {e}")
            return ""

    def _clean_text(self, text: str) -> str:
        """Unescape special characters."""
        return text.replace("_quote_", '"').replace("_apostrophe_", "'")

class OpenRouterProvider(AIProvider):
    """OpenRouter provider with reasoning mode enabled (reasoning tokens excluded from the response)."""

    def _create_client(self) -> Optional[openai.OpenAI]:
        """Create OpenRouter client from environment."""
        proxy_url = os.environ.get("GATEWAY_URL")
        if proxy_url:
            base_url = f"{proxy_url.rstrip('/')}/openrouter/"
            print(f"[lib_llm_ext.OpenRouterProvider._create_client] Connecting via proxy: {base_url}")
            return openai.OpenAI(
                    api_key="proxy",
                    base_url=base_url,
                    )
        if self._var_name in os.environ:
            return openai.OpenAI(api_key=os.environ.get(self._var_name), base_url=self._base_url)

        return None

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        return super().chat(content, max_tokens, reasoning, extra_body={
            "reasoning": {
                "enabled": True,
                "max_tokens": 6000,
                "exclude": True,
            }
        }, **kwargs)

class AsiOneProvider(AIProvider):
    """Lazy AI provider with on-demand initialization."""

    def __init__(self, name: str, var_name: str, model_name: str, base_url: str):
        super().__init__(name, var_name, model_name, base_url)

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        """Send chat request, initializing client if needed."""
        self._ensure_client()

        if self._client is None:
            raise RuntimeError(f"{self.name} not configured (set {self._var_name})")

        sysmsg, usermsg = content.split(":-:-:-:")
        try:
            response = self._client.chat.completions.create(
                model=self._model_name,
                messages=[{"role": "system", "content": sysmsg},
                          {"role": "user", "content": usermsg}],
                max_tokens=max_tokens,
                extra_body={
                    "enable_thinking": True,
                    "thinking_budget": 6000 
                },
                **kwargs
            )

            raw = response.choices[0].message.content
            _log_raw(self._name, self._model_name, raw)
            _log_usage(self._name, self._model_name, response)
            resp = self._clean_text(raw)
            resp = resp.replace("</arg_value>", " ").replace("</tool_call>", " ").replace("<arg_value>", " ").replace("<tool_call>", " ")
            return resp
        except Exception as e:
            print(f"[lib_llm_ext.ASIOneProvider.chat] Exception while communicating with LLM: {e}")
            return ""


class OpenAIProvider(AIProvider):
    """OpenAI provider using the Responses API (reasoning models)."""

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        """Send chat request via the Responses API, initializing client if needed."""
        self._ensure_client()

        if self._client is None:
            raise RuntimeError(f"{self.name} not configured (set {self._var_name})")

        if ":-:-:-:" in content:
            sysmsg, usermsg = content.split(":-:-:-:", 1)
        else:
            sysmsg, usermsg = "", content
        usermsg = usermsg.strip()
        if not usermsg:
            usermsg = "EMPTY / NO NEW USER INPUT."
        try:
            response = self._client.responses.create(
                model=self._model_name,
                instructions=sysmsg,
                input=usermsg,
                max_output_tokens=max_tokens,
                reasoning={"effort": reasoning},
                **kwargs
            )

            raw = response.output_text
            _log_raw(self._name, self._model_name, raw)
            _log_usage(self._name, self._model_name, response)
            return self._clean_text(raw)
        except Exception as e:
            print(f"[lib_llm_ext.OpenAIProvider.chat] Exception while communicating with LLM: {e}")
            return ""


class TestProvider(AbstractAIProvider):
    """Test provider for mocking LLM output"""

    def __init__(self):
        super().__init__("Test")
        self._mock = None
        self._controller_ip = os.environ.get("TEST_SERVER_IP")

    def _llm_mock(self):
        if not self._mock:
            from Autotests.mock.llm import LlmMockAgent, LLM_MOCK_PORT
            self._mock = LlmMockAgent((self._controller_ip, LLM_MOCK_PORT))
        return self._mock

    @property
    def is_available(self) -> bool:
        return self._controller_ip is not None

    def chat(self, content: str, max_tokens: int = 6000, reasoning: str = "medium", **kwargs) -> str:
        return self._llm_mock().chat(content)

# Provider registry - lazy, no initialization yet
_provider_registry = {}


def _register_provider(name: str, var_name: str, model_name: str, base_url: str):
    """Register a provider configuration (no instantiation yet)."""
    _register_provider_instance(AIProvider(name, var_name, model_name, base_url))

def _register_provider_instance(provider: AbstractAIProvider):
    """Register a pre-initialized provider configuration (no instantiation yet)."""
    _provider_registry[provider.name] = provider

def _get_provider(name: str) -> Optional[AIProvider]:
    """Get or create provider instance on demand."""
    return _provider_registry.get(name)


# Register all providers (cheap - just stores config)
_register_provider(name="ASICloud", var_name="ASI_API_KEY", model_name="minimax/minimax-m3", base_url="https://inference.asicloud.cudos.org/v1")
_register_provider(name="Anthropic", var_name="ANTHROPIC_API_KEY", model_name="claude-opus-4-8", base_url="https://api.anthropic.com/v1/")
_register_provider(name="Ollama-local", var_name="OLLAMA_API_KEY", model_name="qwen3.5:9b", base_url="http://localhost:11434/v1")
_register_provider_instance(AsiOneProvider(name="ASIOne", var_name="ASIONE_API_KEY", model_name="asi1-ultra", base_url="https://api.asi1.ai/v1"))
_register_provider_instance(OpenRouterProvider(name="OpenRouter", var_name="OPENROUTER_API_KEY", model_name="z-ai/glm-5.2", base_url="https://openrouter.ai/api/v1"))
_register_provider_instance(OpenRouterProvider(name="MiniMaxM3", var_name="OPENROUTER_API_KEY", model_name="minimax/minimax-m3", base_url="https://openrouter.ai/api/v1"))
_register_provider_instance(TestProvider())
_register_provider_instance(OpenAIProvider(name="OpenAI", var_name="OPENAI_API_KEY", model_name="gpt-5.5", base_url="https://api.openai.com/v1"))


def callProvider(provider_name: str, content: str, max_tokens: int = 6000, reasoning: str = "medium") -> str:
    """Generic dispatcher for MeTTa."""
    provider = _get_provider(provider_name)
    if not provider or not provider.is_available:
        raise RuntimeError(f"Provider '{provider_name}' not available")
    return provider.chat(content=content, max_tokens=max_tokens, reasoning=reasoning)



_embedding_model = None

def initLocalEmbedding():
    model_name="intfloat/e5-large-v2"
    global _embedding_model
    os.environ["HF_HUB_OFFLINE"] = "1"
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer(model_name)
    return _embedding_model

def useLocalEmbedding(atom):
    global _embedding_model
    if _embedding_model is None:
        raise RuntimeError("Call initLocalEmbedding() first.")
    return _embedding_model.encode(
        atom,
        normalize_embeddings=True
    ).tolist()


def _selftest_usage_logging() -> None:
    """DB-free self-test of the [LLM_USAGE] token extraction and log line (run in-container:
    `python lib_llm_ext.py`)."""
    import contextlib
    import io

    class _U:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _R:
        def __init__(self, usage):
            self.usage = usage

    # Chat Completions shape / Responses API shape / missing usage / bad values.
    assert _usage_tokens(_R(_U(prompt_tokens=11, completion_tokens=7))) == (11, 7)
    assert _usage_tokens(_R(_U(input_tokens=5, output_tokens=9))) == (5, 9)
    assert _usage_tokens(_R(None)) == (-1, -1)
    assert _usage_tokens(object()) == (-1, -1)
    assert _usage_tokens(_R(_U(prompt_tokens=None, completion_tokens="x"))) == (-1, -1)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _log_usage("Anthropic", "claude-opus-4-8", _R(_U(prompt_tokens=3, completion_tokens=4)))
    line = buf.getvalue()
    for token in ("[LLM_USAGE]", "provider=Anthropic", "model=claude-opus-4-8",
                  "tokens_in=3", "tokens_out=4"):
        assert token in line, f"missing {token!r} in {line!r}"
    print("lib_llm_ext usage-logging self-test: OK")


if __name__ == "__main__":
    _selftest_usage_logging()


