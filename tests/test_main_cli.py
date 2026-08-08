from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import json
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from unittest import mock

from data_agent.runtime import (
    AgentEvent,
    AgentEventType,
    AgentRequest,
    AgentResponse,
    PrincipalContext,
)
from data_agent.runtime.events import RunCompletedPayload


ROOT = Path(__file__).resolve().parents[1]


class _Runtime:
    def __init__(self, *, explode: bool = False) -> None:
        self.explode = explode
        self.calls: list[tuple[AgentRequest, PrincipalContext]] = []

    async def run(
        self,
        request: AgentRequest,
        principal: PrincipalContext,
    ) -> AsyncIterator[AgentEvent]:
        self.calls.append((request, principal))
        if self.explode:
            raise RuntimeError("database password must not escape")
        response = AgentResponse(
            ok=True,
            question=request.question,
            conversation_id=request.conversation_id,
            tenant_id=principal.tenant_id,
            sql="SELECT 1",
            answer=f"{request.mode.value} complete",
        )
        yield AgentEvent(
            type=AgentEventType.RUN_COMPLETED,
            run_id="run-cli",
            sequence=0,
            data=RunCompletedPayload(),
            response=response,
        )


class _Composition:
    def __init__(self, runtime: _Runtime) -> None:
        self.runtime = runtime
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _NativeComposition:
    def __init__(self, runtime: _Runtime) -> None:
        self.analysis_runtime = runtime
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class DataAgentCliTests(unittest.TestCase):
    def _cli(self):
        self.assertIsNotNone(importlib.util.find_spec("data_agent.cli"))
        return importlib.import_module("data_agent.cli")

    def test_cli_help_exposes_product_commands(self):
        cli = self._cli()
        output = io.StringIO()
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(output):
            cli.main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = output.getvalue()
        self.assertIn("ask", help_text)
        for retired in ("validate-config", "compile-packs", "rebuild-index"):
            self.assertNotIn(retired, help_text)

    def test_ask_forwards_all_modes_and_always_closes_runtime(self):
        cli = self._cli()
        for mode in ("plan", "preview", "execute"):
            with self.subTest(mode=mode):
                runtime = _Runtime()
                composition = _Composition(runtime)
                factory = mock.AsyncMock(return_value=composition)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = cli.main(
                        [
                            "ask",
                            " show gmv ",
                            "--mode",
                            mode,
                            "--tenant-id",
                            "tenant-cli",
                            "--user-id",
                            "user-cli",
                            "--conversation-id",
                            "conv-cli",
                            "--source-id",
                            "orders",
                            "--source-version",
                            "2",
                            "--binding-id",
                            "orders-binding",
                            "--binding-version",
                            "3",
                            "--include-trace",
                        ],
                        runtime_factory=factory,
                    )

                self.assertEqual(code, 0)
                response = AgentResponse.model_validate(json.loads(output.getvalue()))
                self.assertEqual(response.answer, f"{mode} complete")
                request, principal = runtime.calls[-1]
                self.assertEqual(request.mode.value, mode)
                self.assertEqual(request.enterprise_id, "user-dataset")
                self.assertEqual(request.domain_id, "dataset")
                self.assertEqual(request.source_id, "orders")
                self.assertEqual(request.source_version, 2)
                self.assertEqual(request.binding_id, "orders-binding")
                self.assertEqual(request.binding_version, 3)
                self.assertTrue(request.include_trace)
                self.assertEqual(principal.tenant_id, "tenant-cli")
                self.assertEqual(principal.user_id, "user-cli")
                factory.assert_awaited_once_with()
                self.assertEqual(composition.close_calls, 1)

    def test_ask_closes_runtime_when_stream_raises_and_hides_exception(self):
        cli = self._cli()
        composition = _Composition(_Runtime(explode=True))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main(
                ["ask", "show gmv"],
                runtime_factory=mock.AsyncMock(return_value=composition),
            )

        self.assertEqual(code, 1)
        self.assertEqual(composition.close_calls, 1)
        self.assertIn("Data Agent command failed safely.", stderr.getvalue())
        self.assertNotIn("password", stderr.getvalue())

    def test_ask_accepts_native_composition_without_legacy_runtime_attribute(self):
        runtime = _Runtime()
        composition = _NativeComposition(runtime)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            code = self._cli().main(
                ["ask", "show gmv", "--mode", "plan"],
                runtime_factory=mock.AsyncMock(return_value=composition),
            )

        self.assertEqual(code, 0)
        self.assertEqual(len(runtime.calls), 1)
        self.assertEqual(composition.close_calls, 1)

    def test_main_module_is_a_thin_cli_forwarder(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")

        self.assertNotIn("graph.pipeline", source)
        self.assertNotIn("run_nl2sql", source)
        self.assertIn("data_agent.cli", source)

    def test_default_cli_factory_selects_native_analysis_runtime(self):
        source = (ROOT / "src" / "data_agent" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("analysis_runtime", source)
        self.assertNotIn("build_upload_runtime", source)


if __name__ == "__main__":
    unittest.main()
