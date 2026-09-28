"""Regression: numeric ranges must not become PLN concepts ("2 is similar to 20")."""
from wmtm.pln import extract_pln_atoms


def names(text):
    return [a.name for a in extract_pln_atoms(text)]


def test_numeric_tilde_range_yields_nothing():
    assert names("took 2 ~ 20 min") == []


def test_numbers_rejected_in_all_patterns():
    assert names("2 -> 20") == []
    assert names("3 is a 4") == []
    assert names("5 has 6") == []
    assert names("7 is similar to 8") == []


def test_stopwords_rejected():
    assert names("it is a problem") == []
    assert names("this has issues") == []


def test_real_concepts_still_extracted():
    assert "Implication(rain,flood)" in names("rain causes flood")
    assert "Inheritance(cat,mammal)" in names("cat is a mammal")
    assert "Evaluation(has,shard3,receipts)" in names("shard3 has receipts")
    assert "Similarity(wmtm,ecan)" in names("wmtm is similar to ecan")
    assert names("wmtm ~ ecan") == []
