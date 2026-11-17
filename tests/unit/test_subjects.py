from mailassistant.mail.subjects import normalise_subject


def test_prefixes_english_and_dutch():
    assert normalise_subject("Re: Re: Afspraak") == ("afspraak", True)
    assert normalise_subject("Antw: Offerte  hekwerk") == ("offerte hekwerk", True)
    assert normalise_subject("FW: x") == ("x", True)
    assert normalise_subject("Doorgestuurd: Factuur") == ("factuur", True)
    assert normalise_subject("AW: Re[2]: Test") == ("test", True)


def test_non_prefix_words_untouched():
    assert normalise_subject("Reminder: betaling") == ("reminder: betaling", False)
    assert normalise_subject(None) == ("", False)
    assert normalise_subject("Re:") == ("", True)
