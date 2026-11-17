from mailassistant.followups.quoting import own_text


def test_gmail_dutch_attribution_wrapped_over_two_lines():
    body = ("Ik stuur je vrijdag de offerte.\n\nGroet, Alex\n\nOp di 3 nov 2026 om 10:12 schreef Piet Jansen <\n"
            "piet@example.nl>:\n\n> Kun je mij een offerte sturen?\n")
    assert own_text(body) == "Ik stuur je vrijdag de offerte.\n\nGroet, Alex"


def test_proton_english_attribution_with_wrote_on_next_line():
    body = ("Thanks, received.\n\nOn Tuesday, November 3rd, 2026 at 10:12, Frank <frank@example.com>\n"
            "wrote:\n\nPlease send me the invoice by Friday.\n")
    assert own_text(body) == "Thanks, received."


def test_outlook_header_block_with_rule_line():
    body = ("Hierbij de tekening.\n\n________________________________\nVan: Soil Lab <info@soillab.example>\n"
            "Verzonden: dinsdag 3 november 2026 10:12\nAan: Alex\nOnderwerp: tekening\n\nWil je de tekening sturen?")
    assert own_text(body) == "Hierbij de tekening."
    en = "Done.\n\nFrom: A <a@x>\nSent: Tuesday, 3 November 2026 10:12\nTo: B\n\nPlease do it."
    assert own_text(en) == "Done."


def test_forward_and_original_message_separators():
    assert own_text("Zie hieronder.\n\n---------- Forwarded message ---------\nVan: x\nGraag betalen.") == "Zie hieronder."
    assert own_text("Ok\n--------------- Original Message ---------------\nask") == "Ok"


def test_ordinary_sentences_starting_with_op_or_van_are_kept():
    body = ("Op termijn willen wij dat alle contracten dezelfde vervaldatum hebben.\n"
            "Van: de penningmeester kregen we bericht.\nOp naar nieuwe avonturen!")
    assert own_text(body) == body


def test_inline_gt_quotes_are_dropped_but_answers_kept():
    assert own_text("> Kun je dat sturen?\nJa, doe ik morgen.\n> En de factuur?\nVolgt.") == \
        "Ja, doe ik morgen.\nVolgt."
