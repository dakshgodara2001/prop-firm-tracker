from proptracker.matching import FirmMatcher


def _matcher(conn):
    return FirmMatcher.from_db(conn)


def test_exact_match_after_suffix_strip(conn):
    m = _matcher(conn).match("NK SECURITIES RESEARCH PRIVATE LIMITED")
    assert m and m.firm_name == "NK Securities Research"
    assert m.method == "exact" and m.confidence == 1.0


def test_exact_match_short_alias_hrti(conn):
    m = _matcher(conn).match("HRTI PRIVATE LIMITED")
    assert m and m.firm_name == "Hudson River Trading / HRT India"
    assert m.method == "exact"


def test_prefix_match_prefers_longest_alias(conn):
    m = _matcher(conn).match("GRAVITON RESEARCH CAPITAL MARKETS TRADING PRIVATE LIMITED")
    assert m and m.firm_name == "Graviton Research Capital"
    assert m.method == "prefix"
    assert m.alias == "GRAVITON RESEARCH CAPITAL"  # not the shorter "GRAVITON"


def test_prefix_match_multiword(conn):
    m = _matcher(conn).match("JUMP TRADING INDIA PRIVATE LIMITED")
    assert m and m.firm_name == "Jump Trading"
    assert m.method == "prefix" and m.alias == "JUMP TRADING"


def test_short_single_word_alias_is_exact_only(conn):
    # "JUMP" (4 chars) must not fuzzy-match unrelated companies.
    assert _matcher(conn).match("JUMP NETWORKS LIMITED") is None
    # ...but still matches when it IS the whole normalized name.
    m = _matcher(conn).match("JUMP")
    assert m and m.method == "exact"


def test_contains_match_whole_word_only(conn):
    m = _matcher(conn).match("SHRI QUADEYE TRADERS PRIVATE LIMITED")
    assert m and m.firm_name == "QE Securities / Quadeye"
    assert m.method == "contains"
    # Token boundaries: IRAGE must not fire inside MIRAGE.
    assert _matcher(conn).match("MIRAGE CERAMICS LIMITED") is None


def test_ampersand_and_dots(conn):
    m = _matcher(conn).match("QUBE RESEARCH & TECHNOLOGIES PTE LTD")
    assert m and m.firm_name == "Qube Research & Technologies"
    m = _matcher(conn).match("N.K. SECURITIES")
    assert m and m.firm_name == "NK Securities Research"


def test_exact_only_aliases_never_fuzzy_match(conn):
    matcher = _matcher(conn)
    # Live false positives caught in production data: generic English words.
    assert matcher.match("MILLENNIUM STOCK BROKING PVT LTD") is None
    assert matcher.match("MAVERICK SHARE BROKERS PRIVATE LIMITED") is None
    # Exact equality still works...
    m = matcher.match("MILLENNIUM")
    assert m and m.firm_name == "Millennium" and m.method == "exact"
    # ...and the specific multi-word aliases stay fuzzy.
    m = matcher.match("MILLENNIUM MANAGEMENT INDIA PRIVATE LIMITED")
    assert m and m.firm_name == "Millennium" and m.method == "prefix"
    m = matcher.match("MAVERICK DERIVATIVES TRADING LLP")
    assert m and m.firm_name == "Maverick Derivatives" and m.method == "prefix"


def test_no_match_for_regular_participants(conn):
    matcher = _matcher(conn)
    for name in (
        "HDFC MUTUAL FUND",
        "RELIANCE STRATEGIC INVESTMENTS",
        "GRAVITAS COMMODITIES",  # close to GRAVITON but different word
        "",
    ):
        assert matcher.match(name) is None
