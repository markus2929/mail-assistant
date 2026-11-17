from mailassistant.mail.htmltext import html_to_text


def test_hidden_variants_removed_and_counted():
    html = ('<p>Zichtbaar</p><div hidden>geheim1</div><span style="font-size:0">geheim2</span>'
            '<div style="opacity: 0.0">geheim3</div><div style="VISIBILITY:hidden">geheim4</div>'
            '<div><div style="display:none"><p>geheim5</p></div></div>')
    text, hidden = html_to_text(html)
    assert text == "Zichtbaar"
    assert hidden == len("geheim1") * 5


def test_small_font_is_not_hidden():
    text, hidden = html_to_text('<p style="font-size:0.9em">klein maar zichtbaar</p>')
    assert "klein maar zichtbaar" in text and hidden == 0


def test_meeting_links_kept_tracking_links_dropped():
    html = ('<a href="https://teams.microsoft.com/l/meetup-join/abc">Join</a> '
            '<a href="https://track.example/x?u=1234567">Lees meer</a>')
    text, _ = html_to_text(html)
    assert "https://teams.microsoft.com/l/meetup-join/abc" in text
    assert "track.example" not in text and "Lees meer" in text


def test_structure_and_entities():
    text, _ = html_to_text("<ul><li>een</li><li>twee &amp; drie</li></ul><p>a<br>b</p>")
    assert text.splitlines()[:2] == ["- een", "- twee & drie"]
    assert "a\nb" in text


def test_malformed_html_and_unclosed_head():
    text, _ = html_to_text("<html><head><title>t<body><p>inhoud <b>vet</p>")
    assert "inhoud" in text and "vet" in text
    assert html_to_text("<<<>>> &&& <div")[0] is not None


def test_invisible_and_bidi_characters_removed():
    text, _ = html_to_text("<p>ab\u200bc\u202ed\ufeffe</p>")
    assert text == "abcde"
