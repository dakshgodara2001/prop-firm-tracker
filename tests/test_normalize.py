from proptracker.normalize import normalize_name


def test_strips_legal_suffixes():
    assert normalize_name("NK SECURITIES RESEARCH PRIVATE LIMITED") == "NK SECURITIES RESEARCH"
    assert normalize_name("Graviton Research Capital LLP") == "GRAVITON RESEARCH CAPITAL"
    assert normalize_name("HRTI PRIVATE LIMITED") == "HRTI"
    assert normalize_name("JANE STREET SINGAPORE PTE. LTD.") == "JANE STREET SINGAPORE"


def test_punctuation_becomes_word_boundary():
    assert normalize_name("N.K. Securities") == "N K SECURITIES"
    assert normalize_name("QUBE RESEARCH & TECHNOLOGIES PTE LTD") == "QUBE RESEARCH TECHNOLOGIES"
    assert normalize_name("DOLAT CAPITAL MARKET PVT. LTD.") == "DOLAT CAPITAL MARKET"


def test_keeps_business_words_and_digits():
    assert normalize_name("JSI2 INVESTMENTS PVT LTD") == "JSI2 INVESTMENTS"
    assert normalize_name("MAXIZO TRADING PRIVATE LIMITED") == "MAXIZO TRADING"


def test_whitespace_collapse_and_empty():
    assert normalize_name("  Tower   Research  Capital ") == "TOWER RESEARCH CAPITAL"
    assert normalize_name("") == ""
    assert normalize_name(None) == ""
    assert normalize_name("PVT LTD") == ""


def test_trailing_and_co_chain():
    assert normalize_name("JUMP TRADING AND CO") == "JUMP TRADING"
