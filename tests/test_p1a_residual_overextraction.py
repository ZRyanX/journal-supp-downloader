"""P1FIX round-3 residual Cause A: general over-extraction still makes the
completeness warning untrustworthy.

Third-round 50-paper report (~/workspace/jsd-50-paper-test-p1fix-report.md,
section 2(c) and section 4 item 2): 19 papers report completeness warnings,
318 false missing ids in total. Paper #1 (PMC12209443) contains no occurrence
of "Supplementary Table" at all, yet the extractor still outputs 46 ids
(E115-E120, E210-E214, E419-E430, S1-S23). Three mint paths, all reproduced
here with verbatim fragments from the #1 PMC full text
(https://pmc.ncbi.nlm.nih.gov/articles/PMC12209443/):

(a) Reference e-page ranges self-anchor: "e115-e120" (Lancet Infect. Dis.
    23, e115-e120), "e210-e214", "e419-e430" are read as Extended Data
    table ranges, because _group_is_anchored treats any attached-letter
    range as an id mention with no supplementary signal required.
(b) Ethics approval numbers expand as ranges: "R-FP-S-2-0023-0000" is
    parsed as S-2 followed by -0023 and expands to S2-S23 (22 fake ids).
(c) "Supplementary Fig./Movie/Data" prefixes mint table ids:
    _table_token_namespace maps any prefix containing "supplement" to the
    S namespace without requiring the word "table", so "Supplementary
    Data 1/2" mint S1/S2; and a lettered range after a figure word
    ("Figures S1-S3", "Fig. S1-S3") self-anchors as a table range.

The fix tightens all three paths while keeping the legitimate inputs:
bare "S1-S3" stays extractable (an existing test forces that contract --
see test_p1a2_bare_s_range_contract_is_kept, which locks the known cost
that "Samples S1-S3" still extracts), as do "Extended Data 1-3",
"Appendix A1-A3", "Supplementary Table S15", "S15 Data" and the Chinese
forms. Tests that encode the old behaviour are updated in place, not
deleted: tests/test_p1a_supp_context.py no longer lists "Figures S1-S3"
as a legitimate input (intentional semantic change, same convention as
the P1-B update of test_d2).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from supp_finder import (  # noqa: E402
    Candidate,
    detect_declared_table_range,
    is_result_obviously_incomplete,
    supplementary_table_ids,
)


# ── (a) Reference e-page ranges must not self-anchor ─────────────────────

def test_p1a2_epage_range_lancet_is_not_a_table_range():
    # Verbatim #1 reference 11: Lancet Infect. Dis. 23, e115-e120.
    assert supplementary_table_ids(
        "Cole, J. et al. Monkeypox encephalitis with transverse myelitis "
        "in a female patient. Lancet Infect. Dis. 23, e115\u2013e120 (2022)."
    ) == set()


def test_p1a2_epage_ranges_other_journals_are_not_table_ranges():
    # Verbatim #1 references 8 and 30.
    assert supplementary_table_ids(
        "Ogoina, D. et al. Clinical course and outcome of human Monkeypox "
        "in Nigeria. Clin. Infect. Dis. 71, e210\u2013e214 (2020)."
    ) == set()
    assert supplementary_table_ids(
        "Sergeev, A. A. et al. The possibility of using the ICR mouse as an "
        "animal model to assess antimonkeypox drug efficacy. Transbound. "
        "Emerg. Dis. 63, e419\u2013e430 (2016)."
    ) == set()


def test_p1a2_large_e_range_with_supp_signal_nearby_is_still_blocked():
    # A nearby supplementary word must not bless a three-digit E range:
    # e-page numbers are three-digit by nature, genuine Extended Data
    # tables are numbered small.
    assert supplementary_table_ids(
        "The supplementary material lists e115-e120 as references."
    ) == set()
    assert supplementary_table_ids(
        "See the supplementary files: e210-e214."
    ) == set()


def test_p1a2_small_e_range_with_supp_signal_is_kept():
    # Small E ranges with a supplementary signal are still real.
    assert supplementary_table_ids(
        "The extended data include E1-E3.") == {"E1", "E2", "E3"}
    assert supplementary_table_ids(
        "The supplementary files include E1-E3.") == {"E1", "E2", "E3"}


def test_p1a2_explicit_table_word_overrides_large_e_range():
    # An explicit table word anchors even a large E range.
    assert supplementary_table_ids("Extended Data Tables 115-120") == {
        f"E{i}" for i in range(115, 121)}


def test_p1a2_bare_s_range_contract_is_kept():
    # The existing contract forced by test_h4_bare_s_range_is_expanded and
    # test_p1a_legitimate_table_mentions_still_work: a bare S range is
    # still extracted, so "Samples S1-S3" still extracts too. That is the
    # known cost paid to keep legitimate S ranges, locked in here so any
    # future tightening of (a) notices it.
    assert supplementary_table_ids("S1-S3") == {"S1", "S2", "S3"}
    assert supplementary_table_ids("Tables S1-S3") == {"S1", "S2", "S3"}
    assert supplementary_table_ids("Samples S1-S3 were analyzed.") == {
        "S1", "S2", "S3"}


# ── (b) Multi-segment codes must not expand as ranges ────────────────────

def test_p1a2_ethics_approval_number_is_not_a_table_range():
    # Verbatim #1 methods sentence: the ethics number alone minted S2-S23.
    assert supplementary_table_ids(
        "In accordance with Articles 13 and 14 of the Federal Act on "
        "Research on Embryonic Stem Cells, the work with human H1 ESC "
        "line was approved by the Cantonal Ethics Committee of Bern, "
        "Switzerland under the authorization numbers R-FP-S-2-0023-0000 "
        "and KEK BE Ref373/19."
    ) == set()


def test_p1a2_multisegment_code_variants_are_not_table_ranges():
    # Shorter code (no trailing segment) and the bare code itself.
    assert supplementary_table_ids("Approval R-FP-S-2-0023.") == set()
    assert supplementary_table_ids("S-2-0023-0000") == set()
    assert supplementary_table_ids("Approval No. AB-S-1-0007-0001.") == set()


def test_p1a2_year_suffix_after_a_range_is_kept():
    # A plain year glued after a range is not a code segment: neither the
    # range end nor the trailing segment is zero-padded.
    assert supplementary_table_ids("Tables S1-S3-2024") == {"S1", "S2", "S3"}
    assert supplementary_table_ids("S1-S3 (2024)") == {"S1", "S2", "S3"}


# ── (c) Fig./Movie/Data prefixes must not mint table ids ──────────────────

def test_p1a2_supplementary_data_number_is_not_a_table():
    # Verbatim #1 prose: "(Fig. 5c; Supplementary Data 1)" and
    # "Supplementary Information (Supplementary Data 2)".
    assert supplementary_table_ids("Supplementary Data 1") == set()
    assert supplementary_table_ids(
        "Supplementary Data 1 and Supplementary Data 2 contain the raw "
        "values.") == set()
    assert supplementary_table_ids("(Fig. 5c; Supplementary Data 1)") == set()
    assert supplementary_table_ids(
        "Reproducibility data are provided as Supplementary Information "
        "(Supplementary Data 2).") == set()


def test_p1a2_supplementary_fig_and_movie_numbers_are_not_tables():
    # Verbatim #1 prose forms; bare numbers after Fig./Movie never were
    # lettered tokens, locked in as regression guards.
    assert supplementary_table_ids(
        "Supplementary Fig. 1a and Supplementary Fig. 4a") == set()
    assert supplementary_table_ids("Supplementary Fig. 5c\u2013e") == set()
    assert supplementary_table_ids("Supplementary Movies 2-4") == set()
    assert supplementary_table_ids(
        "Supplementary Movies 4\u20136") == set()
    assert supplementary_table_ids(
        "(Fig. 3d, Supplementary Fig. 4a and b, Supplementary Movies 2-4)."
    ) == set()


def test_p1a2_figure_prefixed_lettered_ranges_are_not_tables():
    # INTENTIONAL SEMANTIC CHANGE (P1FIX round-3, Cause A(c)): a lettered
    # range after a figure/movie word is a figure reference, not a table
    # declaration. This replaces the P1-A contract under which "Figures
    # S1-S3" self-anchored as a table range (see the updated expectation
    # in tests/test_p1a_supp_context.py).
    assert supplementary_table_ids("Figures S1-S3") == set()
    assert supplementary_table_ids("Fig. S1-S3") == set()
    assert supplementary_table_ids("Figure S1-S3") == set()
    assert supplementary_table_ids("Supplementary Fig. S1-S3") == set()
    assert supplementary_table_ids("Figs. S1-S3") == set()
    # Not even a nearby supplementary signal re-anchors a figure range.
    assert supplementary_table_ids(
        "Figures S1, S2 and S3 are shown in the supplement.") == set()


def test_p1a2_mixed_tables_and_figures_phrase_still_counts():
    # "Tables and Figures" / "Figures and Tables" names tables too, so
    # the range stays a table declaration (pre-existing contract, see
    # test_explicit_supp_figures_and_tables).
    assert supplementary_table_ids(
        "Supplementary Tables and Figures S1 to S4 provide cohort info."
    ) == {"S1", "S2", "S3", "S4"}
    assert supplementary_table_ids(
        "See Supplementary Figures and Tables S1-S5 for details."
    ) == {"S1", "S2", "S3", "S4", "S5"}


def test_p1a2_lettered_supplementary_data_ranges_are_kept():
    # An explicit S/A/E letter still wins over the Data prefix.
    assert supplementary_table_ids("Supplementary Data S1-S4") == {
        "S1", "S2", "S3", "S4"}
    assert supplementary_table_ids("Data S1-S3") == {"S1", "S2", "S3"}
    assert supplementary_table_ids("S15 Data") == {"S15"}


def test_p1a2_legitimate_prefixes_still_work():
    assert supplementary_table_ids("Extended Data 1-3") == {"E1", "E2", "E3"}
    assert supplementary_table_ids("Extended Data Tables 1-3") == {
        "E1", "E2", "E3"}
    assert supplementary_table_ids("Appendix A1-A3") == {"A1", "A2", "A3"}
    assert supplementary_table_ids("see Appendix 1-4") == {
        "A1", "A2", "A3", "A4"}
    assert supplementary_table_ids("Supplementary Table S15") == {"S15"}
    assert supplementary_table_ids("Supplementary Tables 2-5") == {
        "S2", "S3", "S4", "S5"}
    assert supplementary_table_ids("Table S1.1 and Table S1.2") == {
        "S1.1", "S1.2"}


def test_p1a2_bare_main_text_tables_still_rejected():
    # Guard for the (c) namespace change: a bare "Tables N" prefix is a
    # main-text table, not a supplementary declaration (H5/P1) -- the
    # "table" word alone must not assign the S namespace.
    assert supplementary_table_ids(
        "As seen in Tables 1 to 3, the results indicate...") == set()
    assert supplementary_table_ids(
        "Tables 1, 2, and 3 describe the primary cohort.") == set()


# ── #1 PMC full-text composite (PMC12209443) ─────────────────────────────

# Verbatim fragments from the #1 PMC full text, in document order: the
# three e-page references, the ethics approval sentence, Supplementary
# Fig./Movies/Data prose mentions. The article contains no occurrence of
# "Supplementary Table". Before the fix this composite yields the 46 fake
# ids of the round-3 report (E115-E120, E210-E214, E419-E430, S1-S23).
_PMC12209443_FRAGMENTS = (
    "The generation of hNOs from human pluripotent stem cells was "
    "performed according to established methods (Supplementary Fig. 1a). "
    "In forebrain neurons fluorescence signal was detected additionally "
    "lying within neurites protruding from MPXV foci (Fig. 2b and c, "
    "Supplementary Movies 1 and 2). "
    "Somata and neurites of single cells scattered amidst the dense "
    "neuron population (Fig. 3d, Supplementary Fig. 4a and b, "
    "Supplementary Movies 2-4). "
    "Confirmed to represent a common phenomenon in MPXV-harboring neurons "
    "(Fig. 4c, Supplementary Fig. 5c, Supplementary Movies 4\u20136). "
    "Only a minority of DEGs and were among the least upregulated "
    "transcripts (Fig. 5c; Supplementary Data 1). "
    "Reproducibility data, including independent replicate details, are "
    "provided as Supplementary Information (Supplementary Data 2). "
    "In accordance with Articles 13 and 14 of the Federal Act on "
    "Research on Embryonic Stem Cells, the work with human H1 ESC line "
    "was approved by the Cantonal Ethics Committee of Bern, Switzerland "
    "under the authorization numbers R-FP-S-2-0023-0000 and KEK BE "
    "Ref373/19. "
    "Cole, J. et al. Monkeypox encephalitis with transverse myelitis in "
    "a female patient. Lancet Infect. Dis. 23, e115\u2013e120 (2022). "
    "Ogoina, D. et al. Clinical course and outcome of human Monkeypox in "
    "Nigeria. Clin. Infect. Dis. 71, e210\u2013e214 (2020). "
    "Sergeev, A. A. et al. The possibility of using the ICR mouse as an "
    "animal model to assess antimonkeypox drug efficacy. Transbound. "
    "Emerg. Dis. 63, e419\u2013e430 (2016)."
)


def test_p1a2_pmc12209443_fragments_mint_no_fake_ids():
    assert supplementary_table_ids(_PMC12209443_FRAGMENTS) == set()
    assert detect_declared_table_range(_PMC12209443_FRAGMENTS) == set()


def test_p1a2_pmc12209443_real_table_declaration_survives():
    # The same composite plus one genuine declaration keeps exactly it.
    text = _PMC12209443_FRAGMENTS + " (Supplementary Table S1)."
    assert supplementary_table_ids(text) == {"S1"}


def test_p1a2_no_false_missing_warning_for_pmc12209443():
    # End to end: with the fake declarations gone, a page whose only
    # supplementary file is the Supplementary Data 1 workbook is complete
    # instead of "missing" 45 tables.
    html = "<html><body><p>" + _PMC12209443_FRAGMENTS + "</p></body></html>"
    cands = [
        Candidate(url="https://example.com/data.xlsx", filename="data.xlsx",
                  link_text="Supplementary Data 1"),
    ]
    incomplete, reason = is_result_obviously_incomplete(cands, html)
    assert not incomplete, reason
