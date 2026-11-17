from mailassistant.llm.jsonutil import extract_json


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == '{"a": 1}'
    assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json('Sure! Here it is: {"a": {"b": 2}} Hope that helps') == '{"a": {"b": 2}}'
    assert extract_json("no json here") == "no json here"
