"""P1-A regressions: declared table ids require supplementary context, and
declared_file_count no longer takes max() across statements.

Residual Cause A in the P1 50-paper retest report
(~/workspace/jsd-50-paper-test-p1-report.md, section 4 item 4 and
section 2(a)-(b)): after the DOI/year guard, declared counts were still
median 55 (max 282) and 46/50 papers reported false "missing" tables.
The remaining over-extraction has two mechanisms, both reproduced here:

1. A word-final S/A/E letter glued to a following number range is read as
   a table namespace: "the range were 10-100 ug/ml" -> E10..E100 (#27,
   99 declared), "data 1-60" -> A1..A60 and "was 113" -> S113 (#1, 282
   declared), "were 1-94" -> E1..E94 (#43, 96 declared vs 3 real files).
2. Bare lists of lettered ids with no supplementary signal anywhere
   nearby (sample ids, reference numbers, figure numbers) are extracted
   as table declarations.

The fix requires a table mention to carry supplementary context: an
explicit supplementary/table/appendix/extended-data prefix or trailing
table word, an attached-letter range (S1-S3), a supplementary signal
within a short window, or a supplementary-link/section context supplied
by the caller. When in doubt it under-extracts rather than minting ids.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from supp_finder import (  # noqa: E402
    Candidate,
    covered_table_ids,
    declared_file_count,
    detect_declared_file_count,
    detect_declared_supplement_count,
    detect_declared_table_range,
    is_result_obviously_incomplete,
    supplementary_table_ids,
)


# ── (a) #27: word-final letters must not become table namespaces ──────────

def test_p1a_word_final_e_does_not_make_extended_data_range():
    # Verbatim mechanism behind #27's E10-E100 (99 declared ids):
    # the trailing "e" of "were" + "10-100" was read as Extended Data.
    text = (
        "The AgNPs solution was prepared in methanol at different "
        "concentrations the range were 10\u2013100 \u00b5g/ml, and 200\u00b5l "
        "from all dilution of silver nanoparticle solutions were added."
    )
    assert supplementary_table_ids(text) == set()


def test_p1a_plos_page_keeps_only_real_supplementary_tables():
    # #27 (PMC12208411): real declarations are the S1-S3 Table captions;
    # the concentration range, the reference e-numbers and the figure
    # numbers must not add ids.
    text = (
        "The antimicrobial activity exhibited highest activity at 30 \u03bcg/ml "
        "against three bacterial strains. (S1 Table). Silver nitrate exhibited "
        "moderate activity, (Fig 7) with zones from 17.5 mm to 18.0 mm. "
        "(S1 Table). The AgNPs solution was prepared at different "
        "concentrations the range were 10\u2013100 \u00b5g/ml, and 200\u00b5l "
        "from all dilution were added. (S2 Table). The hypoglycemic effects "
        "demonstrated inhibition of \u03b1-amylase activity. (S3 Table). "
        "Supporting information S1 Fig. Graphical abstract. "
        "S1 Table. Antibacterial activity raw data file. "
        "S2 Table. DPPH Radical scavenging capacity. "
        "S3 Table. Inhibition of in-vitro \u03b1-amylase enzyme assay raw data file. "
        "12. Shehzad A, et al. PeerJ. 2018;6:e6086. doi: 10.7717/peerj.6086 "
        "40. Zhang L, et al. PLoS One. 2014;9(8):e104058. "
        "doi: 10.1371/journal.pone.0104058"
    )
    assert supplementary_table_ids(text) == {"S1", "S2", "S3"}
    assert detect_declared_table_range(text) == {"S1", "S2", "S3"}


def test_p1a_word_final_a_and_s_ranges_are_not_tables():
    # #1 mechanisms: "data 1-60" -> A1..A60, "was 113" -> S113.
    assert supplementary_table_ids("The data 1-60 were analyzed.") == set()
    assert supplementary_table_ids("The study was 113 patients.") == set()
    assert supplementary_table_ids("The values were 5, 10 and 15.") == set()
    assert supplementary_table_ids("The scores were 1\u201394 for all samples.") == set()


def test_p1a_bare_lettered_lists_without_context_are_not_tables():
    # Sample ids / reference numbers with no supplementary signal.
    assert supplementary_table_ids("Samples S1, S2 and S3 were analyzed.") == set()
    assert supplementary_table_ids("S1, S2, S3") == set()
    assert supplementary_table_ids("see E10, E11 and E12 for details") == set()


def test_p1a_bare_list_with_supplementary_signal_nearby_is_kept():
    # The same list IS a declaration when a supplementary signal sits
    # next to it.
    assert supplementary_table_ids(
        "S1, S2 and S3 are provided in the supplementary material.") == {
        "S1", "S2", "S3"}
    assert supplementary_table_ids(
        "The supplementary files include S1, S2 and S3.") == {
        "S1", "S2", "S3"}


# ── (b) #43: 96 declared ids collapse to the real two ─────────────────────

def test_p1a_bmc_page_96_declarations_collapse_to_two():
    # #43 (10.1186/s12916-025-04162-3): 96 declared, true set 3 MOESM files.
    text = (
        "Additional file 1: Table S1. Baseline characteristics. "
        "Additional file 2: Table S2. Sensitivity analyses. "
        "The quality scores were 1\u201394 for all samples. "
        "BMC Med 23, 94 (2025). doi: 10.1186/s12916-025-04162-3"
    )
    assert supplementary_table_ids(text) == {"S1", "S2"}


def test_p1a_no_false_missing_warning_for_plos_page():
    # End to end: once the fake E10-E100 range is gone, a page whose
    # S1-S3 files are all present is complete instead of "missing" 91 tables.
    html = (
        '<section id="supplementary"><h2>Supporting information</h2>'
        "<p>(S1 Table) (S2 Table) (S3 Table) The range were "
        "10\u2013100 \u00b5g/ml.</p>"
        '<a href="/s1.xlsx">Table S1</a>'
        '<a href="/s2.xlsx">Table S2</a>'
        '<a href="/s3.xlsx">Table S3</a></section>'
    )
    cands = [
        Candidate(url="https://example.com/s1.xlsx", filename="s1.xlsx",
                  link_text="Table S1"),
        Candidate(url="https://example.com/s2.xlsx", filename="s2.xlsx",
                  link_text="Table S2"),
        Candidate(url="https://example.com/s3.xlsx", filename="s3.xlsx",
                  link_text="Table S3"),
    ]
    incomplete, reason = is_result_obviously_incomplete(cands, html)
    assert not incomplete, reason


# ── (c) Legitimate inputs must still parse ────────────────────────────────

def test_p1a_legitimate_table_mentions_still_work():
    assert supplementary_table_ids("Tables S1-S3") == {"S1", "S2", "S3"}
    assert supplementary_table_ids("S1-S3") == {"S1", "S2", "S3"}
    assert supplementary_table_ids("Table S1.1 and Table S1.2") == {"S1.1", "S1.2"}
    assert supplementary_table_ids("Appendix A1-A3") == {"A1", "A2", "A3"}
    assert supplementary_table_ids("Extended Data Tables 1-3") == {"E1", "E2", "E3"}
    assert supplementary_table_ids("Extended Data Table 1") == {"E1"}
    assert supplementary_table_ids("Supplementary Table S15") == {"S15"}
    assert supplementary_table_ids("S15 Data") == {"S15"}
    assert supplementary_table_ids("Tables S1, S2, and S3") == {"S1", "S2", "S3"}
    assert supplementary_table_ids("Supplementary Tables 2-5") == {"S2", "S3", "S4", "S5"}
    assert supplementary_table_ids("Supplementary Table S1 and S2") == {"S1", "S2"}
    assert supplementary_table_ids("Results presented in S4 Table.") == {"S4"}
    # Figure ranges keep the pre-existing contract (attached-letter range).
    assert supplementary_table_ids(
        "Supplementary Materials and Methods, Figures S1-S3, Tables S1-S4") == {
        "S1", "S2", "S3", "S4"}


def test_p1a_chinese_table_mentions_still_work():
    assert supplementary_table_ids("\u89c1\u9644\u88681\u81f3\u9644\u88686") == {
        "S1", "S2", "S3", "S4", "S5", "S6"}
    assert supplementary_table_ids("\u8be6\u89c1\u9644\u8868\u56db") == {"S4"}


def test_p1a_lone_lettered_token_still_needs_context():
    # Unchanged: a lone "S1" with no table word anywhere is not evidence.
    assert supplementary_table_ids("S1") == set()
    assert supplementary_table_ids("Table S1, 2020-2021 data") == {"S1"}
    assert supplementary_table_ids("see Table E36269 for details") == set()
    assert supplementary_table_ids("Table S0") == set()


def test_p1a_link_and_filename_context_still_counts():
    # A candidate's own link text / filename IS supplementary context, so
    # a bare list there still resolves (H6 coverage must not regress).
    assert supplementary_table_ids("S1, S2", in_supplementary_context=True) == {
        "S1", "S2"}
    assert covered_table_ids([
        Candidate(url="https://example.com/s1_s2.xlsx",
                  filename="s1_s2.xlsx", link_text="S1, S2"),
    ]) == {"S1", "S2"}


# ── (d) declared_file_count no longer takes max() ─────────────────────────

def test_p1a_declared_file_count_takes_first_statement_not_max():
    # Two file-count statements disagreeing: the first (the article's own
    # declaration) wins; the larger restatement must not inflate it.
    assert declared_file_count(
        "3 Supplementary Files. Supplementary Material (10 files).") == 3
    assert declared_file_count(
        "Supplementary Material (10 files). 3 Supplementary Files.") == 10
    # The H3 single-statement case is unchanged.
    assert declared_file_count("Additional file (7)") == 7
    assert declared_file_count("nothing here") == 0


def test_p1a_h5_file_count_reading_also_drops_max():
    # The H5 file-count reading follows the same rule, so the two
    # file-count entry points cannot disagree on one page.
    text = "3 Supplementary Files. Supplementary Material (10 files)."
    assert detect_declared_file_count(text) == 3
    assert detect_declared_supplement_count(text) == 3


def test_p1a_file_count_and_table_range_are_counted_independently():
    # A file-count statement and a table range on the same page are
    # independent readings: neither inflates the other.
    text = "3 Supplementary Files. Tables S1-S10 are provided."
    assert declared_file_count(text) == 3
    assert detect_declared_file_count(text) == 3
    assert detect_declared_table_range(text) == {
        f"S{i}" for i in range(1, 11)}
    assert detect_declared_supplement_count(text) == 3
