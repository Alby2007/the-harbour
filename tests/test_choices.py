"""parse_choices — pure parser for Devin's enumerated questions."""

from devinmobile.choices import parse_choices


def test_numbered_list():
    text = (
        "Which approach should I take?\n\n"
        "1. rewrite the parser\n"
        "2. patch the lexer\n"
        "3. punt to a follow-up"
    )
    assert parse_choices(text) == [
        "rewrite the parser", "patch the lexer", "punt to a follow-up",
    ]


def test_numbered_paren_style():
    text = "Pick one?\n1) alpha\n2) beta"
    assert parse_choices(text) == ["alpha", "beta"]


def test_lettered_list():
    text = "Which target?\nA) staging\nB) prod"
    assert parse_choices(text) == ["staging", "prod"]


def test_bullet_list():
    text = "Which file should I touch?\n- foo.py\n- bar.py\n- baz.py"
    assert parse_choices(text) == ["foo.py", "bar.py", "baz.py"]


def test_no_question_mark():
    text = "Here's what I did:\n1. fixed the bug\n2. ran the tests"
    assert parse_choices(text) is None


def test_non_sequential_numbering():
    # a gapped list is prose, not a menu — even with a ? somewhere
    text = "Does this order look right?\n1. first\n3. third"
    assert parse_choices(text) is None


def test_not_starting_at_one():
    text = "Continue from here?\n2. second\n3. third"
    assert parse_choices(text) is None


def test_too_many_options():
    items = "\n".join(f"{i}. option {i}" for i in range(1, 7))
    assert parse_choices(f"Which one?\n{items}") is None


def test_single_item_is_not_a_menu():
    text = "Is this ok?\n1. the only option"
    assert parse_choices(text) is None


def test_oversized_option():
    text = f"Which?\n1. {'x' * 200}\n2. short"
    assert parse_choices(text) is None


def test_last_block_wins():
    text = (
        "Here's the context:\n"
        "- found a leak\n"
        "- found a race\n\n"
        "How should I fix it?\n"
        "1. mutex\n"
        "2. channel"
    )
    assert parse_choices(text) == ["mutex", "channel"]


def test_question_after_list_still_parses():
    text = "1. option a\n2. option b\n\nSound right?"
    assert parse_choices(text) == ["option a", "option b"]


def test_confirmation_question_yes_no():
    text = "The migration is ready — should I proceed?"
    assert parse_choices(text) == ["Yes", "No"]


def test_confirmation_variants():
    for q in (
        "Want me to continue?",
        "Shall I go ahead?",
        "OK to merge this?",
        "Would you like me to open the PR?",
    ):
        assert parse_choices(q) == ["Yes", "No"], q


def test_open_question_no_buttons():
    assert parse_choices("What does this function do?") is None


def test_confirmation_not_at_end():
    # the ? isn't the closing beat — it's mid-message
    text = "Should I proceed? Also, see the note above."
    assert parse_choices(text) is None


def test_list_beats_fallback():
    # a qualifying list wins over the yes/no fallback even when the
    # closing line is confirmation-shaped
    text = "Want me to proceed?\n1. do it\n2. skip it"
    assert parse_choices(text) == ["do it", "skip it"]


def test_empty_and_whitespace():
    assert parse_choices("") is None
    assert parse_choices("   \n\n") is None
