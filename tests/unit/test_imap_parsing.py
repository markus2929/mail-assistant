from mailassistant.mail.imap_bridge import (extract_message_id, parse_fetch_items, parse_list_line,
                                            quote_mailbox)


def test_list_lines():
    i = parse_list_line(rb'(\HasNoChildren \Sent) "/" "Sent"')
    assert i.name == "Sent" and "\\sent" in i.flags and i.delimiter == "/"
    assert parse_list_line(rb'(\Marked) "/" "Folders/[INBOX]Bestellingen"').name == "Folders/[INBOX]Bestellingen"
    assert parse_list_line(rb'(\Marked) "/" INBOX').name == "INBOX"
    assert parse_list_line(rb'(\Noselect) NIL Folders').delimiter is None
    assert parse_list_line(b"garbage") is None


def test_fetch_items_uid_before_and_after_literal():
    before = [(b"1 (UID 345 BODY[] {5}", b"hello"), b")",
              (b"2 (UID 346 BODY[] {3}", b"abc"), b")"]
    assert parse_fetch_items(before) == [(345, b"hello"), (346, b"abc")]
    after = [(b"1 (BODY[] {5}", b"hello"), b" UID 345)"]
    assert parse_fetch_items(after) == [(345, b"hello")]
    assert parse_fetch_items([b"1 (UID 5 FLAGS ())"]) == []


def test_message_id_extraction_including_folded_and_missing():
    assert extract_message_id(b"Message-ID: <Ab@Example.NL>\r\n\r\n") == "<ab@example.nl>"
    assert extract_message_id(b"Message-ID:\r\n <folded@x.y>\r\n\r\n") == "<folded@x.y>"
    assert extract_message_id(b"\r\n") is None


def test_mailbox_quoting():
    assert quote_mailbox('Folders/A "b"') == '"Folders/A \\"b\\""'


class _StubImap:
    def __init__(self, reply):
        self.reply = reply

    def uid(self, *args):
        return self.reply


def _source(reply):
    from mailassistant.mail.imap_bridge import ImapBridgeSource
    src = ImapBridgeSource("h", 1, "u", "p", "00")
    src._m, src._selected = _StubImap(reply), "INBOX"
    return src


def test_no_such_message_means_empty_result_not_error():
    src = _source(("NO", [b"no such message"]))
    assert src.search_uids("INBOX", 15178) == []
    assert src.fetch_message_ids("INBOX", [1, 2]) == {}
    assert list(src.fetch_raw("INBOX", [1])) == []


def test_other_no_responses_still_raise():
    from mailassistant.mail.source import MailSourceError
    try:
        _source(("NO", [b"internal error"])).search_uids("INBOX", 1)
    except MailSourceError:
        return
    raise AssertionError("expected MailSourceError")


def test_search_parsing_filters_the_always_returned_last_uid():
    assert _source(("OK", [b"5 6 7"])).search_uids("INBOX", 6) == [6, 7]
    assert _source(("OK", [b"5"])).search_uids("INBOX", 9) == []   # "9:*" returns the last message
    assert _source(("OK", [None])).search_uids("INBOX", 1) == []
