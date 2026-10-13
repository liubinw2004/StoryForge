"""``app/services/json_parser.py`` 单元测试。

分两部分：
- :class:`TestExistingSemantics`：**既有三级容错语义回归**（13 条），修改前后必须完全一致；
- :class:`TestSingleQuoteFallback`：本次修复 F1 新增的「单引号 JSON 兜底」能力。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.json_parser import extract_json_object, parse_json_lenient


class TestExistingSemantics(unittest.TestCase):
    """既有语义回归（13 条）：修改 json_parser 后必须全部保持通过。"""

    def test_01_plain_object(self):
        self.assertEqual(parse_json_lenient('{"a": 1, "b": "x"}'), {"a": 1, "b": "x"})

    def test_02_top_level_array(self):
        self.assertEqual(parse_json_lenient('[{"a": 1}, {"b": 2}]'), [{"a": 1}, {"b": 2}])

    def test_03_fenced_json_block(self):
        text = '```json\n{"title": "咖啡馆", "shots": []}\n```'
        self.assertEqual(parse_json_lenient(text), {"title": "咖啡馆", "shots": []})

    def test_04_fenced_block_without_language(self):
        text = '```\n{"a": 1}\n```'
        self.assertEqual(parse_json_lenient(text), {"a": 1})

    def test_05_embedded_in_chinese_prose(self):
        text = '好的，这是脚本：{"title": "约定"} 以上。'
        self.assertEqual(parse_json_lenient(text), {"title": "约定"})

    def test_06_trailing_comma_in_object(self):
        self.assertEqual(parse_json_lenient('{"a": 1, "b": 2,}'), {"a": 1, "b": 2})

    def test_07_trailing_comma_in_array(self):
        self.assertEqual(parse_json_lenient('{"a": [1, 2, 3,]}'), {"a": [1, 2, 3]})

    def test_08_trailing_comma_in_nested_object(self):
        text = '{"shots": [{"id": "shot_001", "duration": 4,},],}'
        self.assertEqual(parse_json_lenient(text), {"shots": [{"id": "shot_001", "duration": 4}]})

    def test_09_fullwidth_double_quotes(self):
        text = '{\u201ctitle\u201d: \u201c咖啡馆\u201d}'
        self.assertEqual(parse_json_lenient(text), {"title": "咖啡馆"})

    def test_10_leading_and_trailing_whitespace(self):
        self.assertEqual(parse_json_lenient('\n\n   {"a": 1}   \n'), {"a": 1})

    def test_11_top_level_int_scalar(self):
        self.assertEqual(parse_json_lenient("42"), 42)

    def test_12_empty_string_raises(self):
        for text in ("", "   ", "\n\t"):
            with self.subTest(text=repr(text)):
                with self.assertRaises(ValueError):
                    parse_json_lenient(text)

    def test_13_truncated_and_garbage_raise(self):
        for text in ('{"a": 1', '{"shots": [{"a": 1}', "这不是 JSON", "it's just English"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_json_lenient(text)


class TestSingleQuoteFallback(unittest.TestCase):
    """F1：单引号包裹的 JSON 必须能被解析（策略：ast.literal_eval 兜底）。"""

    def test_single_quotes_with_apostrophe_in_value(self):
        """核心用例：值内含撇号 ``"it's ok"`` 不能被破坏。"""
        text = "{'name': '小明', 'shots': [{'dialogue': \"it's ok\", 'duration': 4}]}"
        parsed = parse_json_lenient(text)
        self.assertEqual(parsed["name"], "小明")
        self.assertEqual(parsed["shots"][0]["dialogue"], "it's ok")
        self.assertEqual(parsed["shots"][0]["duration"], 4)

    def test_single_quotes_with_bare_true_false_null(self):
        text = "{'ok': true, 'missing': null, 'done': false}"
        parsed = parse_json_lenient(text)
        self.assertIs(parsed["ok"], True)
        self.assertIsNone(parsed["missing"])
        self.assertIs(parsed["done"], False)

    def test_bare_literal_inside_string_is_not_corrupted(self):
        """归一裸字面量时必须只处理字符串外部，不能改到引号内的文本。"""
        text = "{'text': 'it is true and false', 'flag': true}"
        parsed = parse_json_lenient(text)
        self.assertEqual(parsed["text"], "it is true and false")
        self.assertIs(parsed["flag"], True)

    def test_single_quoted_array_of_objects(self):
        text = "[{'a': 1}, {'b': 'x'}]"
        self.assertEqual(parse_json_lenient(text), [{"a": 1}, {"b": "x"}])

    def test_single_quotes_inside_code_fence(self):
        text = "```json\n{'title': '约定', 'shots': [{'shot_id': 'shot_001'}]}\n```"
        parsed = parse_json_lenient(text)
        self.assertEqual(parsed["title"], "约定")
        self.assertEqual(parsed["shots"][0]["shot_id"], "shot_001")

    def test_single_quotes_embedded_in_prose(self):
        text = "这是脚本：{'title': '约定'} 请查收。"
        self.assertEqual(parse_json_lenient(text), {"title": "约定"})

    def test_single_quotes_with_trailing_comma(self):
        self.assertEqual(parse_json_lenient("{'a': 1, 'b': 2,}"), {"a": 1, "b": 2})

    def test_single_quotes_with_brace_inside_string(self):
        """单引号字符串里含花括号，括号匹配也必须能正确配对。"""
        text = "{'note': '第一幕 {开场}', 'n': 1}"
        self.assertEqual(parse_json_lenient(text), {"note": "第一幕 {开场}", "n": 1})

    def test_invalid_single_quoted_still_raises(self):
        for text in ("{'a': }", "{'a': 1", "{'a' 1}"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_json_lenient(text)

    def test_valid_single_quoted_object_parses(self):
        self.assertEqual(parse_json_lenient("{'a': 'b'}"), {"a": "b"})

    def test_tuple_and_nested_values_become_json_compatible(self):
        parsed = parse_json_lenient("{'pair': (1, 2), 'nested': {'k': 'v'}}")
        self.assertEqual(parsed, {"pair": [1, 2], "nested": {"k": "v"}})

    def test_scalar_text_does_not_gain_new_success(self):
        """容器之外的裸文本不应因兜底而「从失败变成功」，保持既有语义。"""
        for text in ("None", "True", "hello"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_json_lenient(text)

    def test_extract_json_object_handles_single_quotes(self):
        snippet = extract_json_object("前缀 {'a': 1} 后缀")
        self.assertIsNotNone(snippet)
        self.assertEqual(parse_json_lenient(snippet), {"a": 1})


class TestExtractAndQuoteRegression(unittest.TestCase):
    """第二轮 QA 补回：``extract_json_object`` 既有行为 + ``'`` 识别后的回归防护。"""

    def test_extract_json_object_prefers_code_block(self):
        """既有语义：有代码块时应优先返回块内内容（原被覆盖文件中的断言）。"""
        self.assertEqual(extract_json_object('```json\n{"x": 1}\n```'), '{"x": 1}')

    def test_extract_json_object_returns_none_on_bare_text(self):
        self.assertIsNone(extract_json_object("no json here"))

    def test_double_quoted_json_with_apostrophe_value_still_parses(self):
        """回归：为支持单引号而让 ``'`` 参与配对，**不得**破坏合法双引号 JSON。

        值内含撇号（``it's``）时，``'`` 必须被当作普通字符而非字符串定界符。
        """
        self.assertEqual(
            parse_json_lenient('{"a": "it\'s ok", "b": "don\'t panic"}'),
            {"a": "it's ok", "b": "don't panic"},
        )

    def test_handwritten_prose_with_apostrophes_raises(self):
        """手写英文撇号文本不应被误判为可解析的 JSON。"""
        for text in ("it's a test", "don't panic", "Here's the plan, it's simple"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_json_lenient(text)


if __name__ == "__main__":
    unittest.main()
