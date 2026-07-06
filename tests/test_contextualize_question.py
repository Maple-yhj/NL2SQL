import unittest

from graph.tools.contextualize_question import build_contextualization_prompt, contextualize_question


class FakeLLM:
    def __init__(self, response="show GMV by region last month"):
        self.response = response
        self.calls = []

    async def complete(self, prompt, system="", max_output_tokens=2048):
        self.calls.append({"prompt": prompt, "system": system, "max_output_tokens": max_output_tokens})
        return self.response


class ContextualizeQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_original_question_when_no_context_exists(self):
        llm = FakeLLM()

        result = await contextualize_question(
            question="show gmv",
            conversation_history=[],
            user_memories=[],
            llm=llm,
        )

        self.assertEqual(result, "show gmv")
        self.assertEqual(llm.calls, [])

    async def test_rewrites_follow_up_with_conversation_context(self):
        llm = FakeLLM("show GMV by region last month")

        result = await contextualize_question(
            question="那按地区呢",
            conversation_history=[
                {"role": "user", "content": "show GMV last month", "metadata": {}},
                {"role": "assistant", "content": "GMV is 100.", "metadata": {"sql": "SELECT 100 AS gmv"}},
            ],
            user_memories=[{"memory_key": "timezone", "memory_value": "Asia/Shanghai", "metadata": {}}],
            llm=llm,
        )

        self.assertEqual(result, "show GMV by region last month")
        self.assertIn("那按地区呢", llm.calls[0]["prompt"])
        self.assertIn("show GMV last month", llm.calls[0]["prompt"])
        self.assertIn("Asia/Shanghai", llm.calls[0]["prompt"])

    async def test_preserves_previous_month_grain_when_follow_up_adds_customer_state(self):
        llm = FakeLLM("2018年GMV按客户州拆分")

        result = await contextualize_question(
            question="那按客户州拆一下",
            conversation_history=[
                {"role": "user", "content": "2018 年 GMV 按月份趋势", "metadata": {}},
                {
                    "role": "assistant",
                    "content": "已按月统计。",
                    "metadata": {
                        "sql": (
                            "SELECT date_trunc('month', shipping_limit_date) AS month, "
                            "SUM(price + freight_value) AS gmv "
                            "FROM olist_order_items_dataset GROUP BY month"
                        )
                    },
                },
            ],
            user_memories=[],
            llm=llm,
        )

        self.assertIn("月份", result)
        self.assertIn("客户州", result)

    def test_prompt_formats_history_and_user_memories(self):
        prompt = build_contextualization_prompt(
            question="那按地区呢",
            conversation_history=[{"role": "user", "content": "show GMV", "metadata": {}}],
            user_memories=[{"memory_key": "preferred_region", "memory_value": "华东", "metadata": {}}],
        )

        self.assertIn("[RECENT CONVERSATION]", prompt)
        self.assertIn("show GMV", prompt)
        self.assertIn("preferred_region: 华东", prompt)


if __name__ == "__main__":
    unittest.main()
