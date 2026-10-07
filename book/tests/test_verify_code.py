# tests/book/test_verify_code.py
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "book" / "build"))
from verify_code import extract_python_listings, check_syntax, check_sample_provenance  # noqa: E402

def test_extract_and_syntax(tmp_path):
    md = ("intro\n"
          "::: listing a.py | L1\n"
          "x = 1\n"
          ":::\n"
          "::: listing b.txt | not python\n"
          "noise\n"
          ":::\n")
    f = tmp_path / "c.md"; f.write_text(md)
    listings = extract_python_listings(f)
    assert [l.label for l in listings] == ["a.py"]      # only .py extracted
    ok, err = check_syntax(listings[0].code)
    assert ok and err is None
    bad_ok, bad_err = check_syntax("def (:")
    assert not bad_ok and "Syntax" in bad_err


def test_new_feature_flag_listing_matches_the_tested_sample() -> None:
    root = Path(__file__).resolve().parents[2]
    label = "samples/lumen/tests/test_feature_flags.py"
    for edition in ("manuscript", "manuscript-es"):
        chapter = root / "book" / edition / "96-appendix-f-feature-flags.md"
        listing = next(item for item in extract_python_listings(chapter) if item.label == label)
        assert check_sample_provenance(listing, root)
        tampered = type(listing)(listing.label, listing.code + "\nassert False", listing.source, listing.line)
        assert not check_sample_provenance(tampered, root)
