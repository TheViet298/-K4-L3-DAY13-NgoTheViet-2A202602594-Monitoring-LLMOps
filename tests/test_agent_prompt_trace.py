from __future__ import annotations

from contextlib import contextmanager

from app import agent as agent_module


class ManagedPrompt:
    version = 3

    def compile(self, **variables: str) -> str:
        return (
            f"Feature={variables['feature']}\n"
            f"Docs={variables['docs']}\n"
            f"Question={variables['message']}"
        )


class RecordingLangfuseClient:
    def __init__(self) -> None:
        self.prompt = ManagedPrompt()
        self.span_updates: list[dict] = []
        self.observations: list[dict] = []

    def get_prompt(self, name: str, **kwargs):
        return self.prompt

    def update_current_span(self, **kwargs) -> None:
        self.span_updates.append(kwargs)

    def start_as_current_observation(self, **kwargs):
        self.observations.append(kwargs)

        @contextmanager
        def _context():
            yield type("Obs", (), {"output": None})()

        return _context()

    def update_current_generation(self, **kwargs) -> None:
        self.observations.append({"generation": kwargs})


def test_agent_records_prompt_version_with_v4_observation_api(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_PROMPT_NAME", "day13-chat")
    monkeypatch.setenv("LANGFUSE_PROMPT_LABEL", "production")
    client = RecordingLangfuseClient()
    monkeypatch.setattr(agent_module, "get_langfuse_client", lambda: client)
    monkeypatch.setattr(agent_module, "tracing_enabled", lambda: True)

    propagated: list[dict] = []

    @contextmanager
    def record_attributes(**kwargs):
        propagated.append(kwargs)
        yield

    monkeypatch.setattr(agent_module, "propagate_attributes", record_attributes)

    agent = agent_module.LabAgent()
    agent_module.LabAgent.run.__wrapped__(
        agent,
        user_id="student-01",
        feature="qa",
        session_id="session-01",
        message="Explain traces",
        correlation_id="req-12345678",
    )

    span_update = client.span_updates[-1]
    assert span_update["metadata"] == {
        "doc_count": 1,
        "prompt_name": "day13-chat",
        "prompt_label": "production",
        "prompt_version": "3",
        "prompt_source": "langfuse",
        "prompt_fetch_error": "",
    }
    assert span_update["version"] == "3"
    assert propagated[0]["metadata"]["correlation_id"] == "req-12345678"
    assert propagated[-1]["prompt"] is client.prompt


def test_agent_creates_child_observations_for_retrieval_and_generation(monkeypatch) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.started: list[dict] = []
            self.updated_generation: list[dict] = []

        def start_as_current_observation(self, **kwargs):
            self.started.append(kwargs)

            @contextmanager
            def _context():
                yield type("Obs", (), {"output": None})()

            return _context()

        def update_current_generation(self, **kwargs) -> None:
            self.updated_generation.append(kwargs)

    client = RecordingClient()
    monkeypatch.setattr(agent_module, "get_langfuse_client", lambda: client)
    monkeypatch.setattr(agent_module, "tracing_enabled", lambda: True)

    monkeypatch.setattr(agent_module, "retrieve", lambda message: ["doc-1"])

    def fake_resolve_prompt(client_arg, **kwargs):
        return type(
            "Prompt",
            (),
            {
                "name": "day13-chat",
                "label": "production",
                "version": "3",
                "source": "langfuse",
                "managed_prompt": object(),
                "text": "prompt text",
                "fetch_error": "",
            },
        )()

    monkeypatch.setattr(agent_module, "resolve_prompt", fake_resolve_prompt)

    def fake_generate(self, prompt: str):
        return type(
            "Resp",
            (),
            {
                "text": "answer",
                "usage": type("Usage", (), {"input_tokens": 11, "output_tokens": 22})(),
                "ttft_ms": 15,
            },
        )()

    monkeypatch.setattr(agent_module.LabAgent, "_heuristic_quality", lambda *args, **kwargs: 0.9)
    monkeypatch.setattr(agent_module.LabAgent, "_estimate_cost", lambda *args, **kwargs: 0.005)

    agent = agent_module.LabAgent()
    agent.llm = type("LLM", (), {"generate": fake_generate})()
    agent_module.LabAgent.run.__wrapped__(
        agent,
        user_id="student-01",
        feature="qa",
        session_id="session-01",
        message="Explain traces",
        correlation_id="req-12345678",
    )

    starts = [item for item in client.started if item.get("as_type") in {"retriever", "generation"}]
    assert any(item["as_type"] == "retriever" for item in starts)
    assert any(item["as_type"] == "generation" for item in starts)
    assert all("input" not in item and "output" not in item for item in starts)
    assert any(item.get("model") == "claude-sonnet-4-5" for item in client.updated_generation)
    generation_update = next(
        item for item in client.updated_generation if item.get("name") == "llm-generation"
    )
    assert "input" not in generation_update
    assert "output" not in generation_update
