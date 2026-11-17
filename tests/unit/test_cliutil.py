import argparse

import pytest

from mailassistant.cliutil import parse_ids


def test_ids_in_every_reasonable_spelling():
    assert parse_ids(["89", "90"]) == [89, 90]
    assert parse_ids(["89,90,95"]) == [89, 90, 95]
    assert parse_ids(["89,", "90,", "95"]) == [89, 90, 95]          # shell splits "89, 90, 95" like this
    assert parse_ids(["#89", "90;", "89"]) == [89, 90]              # '#' prefix, duplicates dropped


def test_garbage_is_refused_not_guessed():
    for bad in (["89", "abc"], ["8-9"], [","], []):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_ids(bad)
