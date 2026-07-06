import unittest

from graph.message_type import classify_message_type


class MessageTypeClassifierTests(unittest.TestCase):
    def test_summary_metric_question_uses_text_even_when_rows_exist(self):
        message_type = classify_message_type(
            question="\u534e\u4e1c\u5730\u533a\u548c\u534e\u5357\u5730\u533a\u7684GMV\u662f\u591a\u5c11",
            contextualized_question="",
            sql="SELECT region, SUM(amount) AS gmv FROM orders GROUP BY region",
            rows=[
                {"region": "east", "gmv": 100},
                {"region": "south", "gmv": 80},
            ],
            error="",
        )

        self.assertEqual(message_type, "text")

    def test_detail_record_question_uses_table_when_rows_exist(self):
        message_type = classify_message_type(
            question="\u534e\u4e1c\u5730\u533a\u6628\u5929\u6240\u6709\u7684\u8ba2\u5355\u8bb0\u5f55",
            contextualized_question="",
            sql="SELECT order_id, customer_id, amount, created_at FROM orders WHERE region = 'east'",
            rows=[{"order_id": "O-1", "amount": 100}],
            error="",
        )

        self.assertEqual(message_type, "table")

    def test_detail_sql_shape_uses_table_when_question_is_ambiguous(self):
        message_type = classify_message_type(
            question="show yesterday orders",
            contextualized_question="",
            sql="SELECT order_id, customer_id, amount, created_at FROM orders ORDER BY created_at DESC",
            rows=[{"order_id": "O-1", "amount": 100}],
            error="",
        )

        self.assertEqual(message_type, "table")

    def test_aggregate_sql_shape_uses_text_when_question_is_ambiguous(self):
        message_type = classify_message_type(
            question="show regional gmv",
            contextualized_question="",
            sql="SELECT region, SUM(amount) AS gmv FROM orders GROUP BY region",
            rows=[{"region": "east", "gmv": 100}],
            error="",
        )

        self.assertEqual(message_type, "text")

    def test_product_volume_topn_detail_uses_table_despite_summary_words(self):
        message_type = classify_message_type(
            question="查看体积最大的商品，返回商品 id、品类、长宽高和重量，取前 25 个",
            contextualized_question="",
            sql=(
                "SELECT product_id, product_category_name, product_length_cm, "
                "product_width_cm, product_height_cm, product_weight_g, "
                "product_length_cm * product_width_cm * product_height_cm AS volume "
                "FROM olist_products_dataset ORDER BY volume DESC LIMIT 25"
            ),
            rows=[
                {
                    "product_id": "P-1",
                    "product_category_name": "moveis",
                    "product_length_cm": 10,
                    "product_width_cm": 20,
                    "product_height_cm": 30,
                    "product_weight_g": 500,
                    "volume": 6000,
                }
            ],
            error="",
        )

        self.assertEqual(message_type, "table")

    def test_generic_list_word_does_not_override_aggregate_metric_question(self):
        message_type = classify_message_type(
            question="\u5217\u51fa\u534e\u4e1c\u548c\u534e\u5357\u7684GMV",
            contextualized_question="",
            sql="SELECT region, SUM(amount) AS gmv FROM orders GROUP BY region",
            rows=[{"region": "east", "gmv": 100}],
            error="",
        )

        self.assertEqual(message_type, "text")

    def test_error_has_priority(self):
        message_type = classify_message_type(
            question="\u534e\u4e1c\u5730\u533a\u6628\u5929\u6240\u6709\u7684\u8ba2\u5355\u8bb0\u5f55",
            contextualized_question="",
            sql="",
            rows=[{"order_id": "O-1"}],
            error="SQL validation failed.",
        )

        self.assertEqual(message_type, "error")


if __name__ == "__main__":
    unittest.main()
