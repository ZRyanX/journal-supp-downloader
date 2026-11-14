#!/usr/bin/env python3
"""
Microsoft MarkItDown Converter Integration
==========================================
Provides optional, high-fidelity conversion of downloaded scientific
supplementary files (.docx, .xlsx, .pptx, .pdf, .zip, .csv) into
structured Markdown sidecars for inspection by users and LLMs.

Safety Guarantee:
- MarkItDown is an OPTIONAL post-processing step.
- Original scientific files (.xlsx, .docx, .zip) are ALWAYS preserved 100% intact.
- Lazy-imports markitdown; degrades gracefully if markitdown is not installed.
"""

import io
import os
import sys
import zipfile
from typing import List, Optional, Tuple


CONVERTIBLE_EXTENSIONS = {
    ".docx", ".xlsx", ".xls", ".pptx",
    ".pdf", ".csv", ".tsv", ".json", ".xml", ".zip",
    ".html", ".htm",
}


IGNORED_FILENAMES = {
    "download_manifest.json",
    "manifest.json",
    "cookies.json",
    "package.json",
    "supplementary_candidates.json",
    "supplementary_candidates.partial.json",
    "supplements_markdown_summary.md",
}

# Maximum single file size allowed for full markdown conversion (25 MB)
MAX_CONVERTIBLE_FILE_SIZE = 25 * 1024 * 1024


def is_markitdown_available() -> bool:
    """Check if microsoft markitdown package is available in the current environment."""
    try:
        import markitdown  # noqa: F401
        return True
    except ImportError:
        return False


def is_convertible_file(path_or_name: str) -> bool:
    """Check whether a file extension is supported for Markdown conversion."""
    if not path_or_name:
        return False
    base = os.path.basename(str(path_or_name)).lower()
    if base in IGNORED_FILENAMES or base.endswith(("_manifest.json", ".log")):
        return False
    ext = os.path.splitext(base)[1]
    return ext in CONVERTIBLE_EXTENSIONS


def _cell_str(val) -> str:
    if val is None:
        return ""
    text = str(val).replace("\r\n", " ").replace("\n", " ").replace("|", "\\|").strip()
    return text


def _is_numeric_or_data_cell(val) -> bool:
    if val is None:
        return False
    if isinstance(val, (int, float)):
        return True
    s = str(val).strip()
    if not s:
        return False
    try:
        float(s.replace(",", ""))
        return True
    except ValueError:
        return s.lower() in {"bdl", "<dl", "nd", "n.d.", "-", "—", "na", "n/a"}


def _iter_excel_sheets(
    input_path: str,
    file_bytes: Optional[bytes] = None,
) -> Optional[List[Tuple[str, List[List[object]], List[Tuple[int, int, int, int]]]]]:
    """Load sheets from an ``.xlsx`` or ``.xls`` workbook (from disk or bytes)
    and return ``[(sheet_name, grid, merged_ranges)]`` with 0-based ``(rlo, rhi, clo, chi)``.
    """
    lower = input_path.lower()
    if lower.endswith(".xlsx"):
        try:
            import openpyxl
        except ImportError:
            return None
        try:
            source = io.BytesIO(file_bytes) if file_bytes is not None else input_path
            wb = openpyxl.load_workbook(source, read_only=False, data_only=True)
        except Exception:
            return None
        sheets = []
        try:
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                max_row = ws.max_row or 0
                max_col = ws.max_column or 0
                if max_row == 0 or max_col == 0:
                    continue
                grid: List[List[object]] = [
                    list(row_vals[:max_col]) + [None] * max(0, max_col - len(row_vals))
                    for row_vals in ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col, values_only=True)
                ]
                merged = [
                    (mr.min_row - 1, mr.max_row, mr.min_col - 1, mr.max_col)
                    for mr in ws.merged_cells.ranges
                    if mr.min_row <= 15
                ]
                sheets.append((sheet_name, grid, merged))
        finally:
            try:
                wb.close()
            except Exception:
                pass
        return sheets

    if lower.endswith(".xls"):
        try:
            import xlrd
        except ImportError:
            return None
        try:
            if file_bytes is not None:
                try:
                    book = xlrd.open_workbook(file_contents=file_bytes, formatting_info=True)
                except Exception:
                    book = xlrd.open_workbook(file_contents=file_bytes, formatting_info=False)
            else:
                try:
                    book = xlrd.open_workbook(input_path, formatting_info=True)
                except Exception:
                    book = xlrd.open_workbook(input_path, formatting_info=False)
        except Exception:
            return None

        sheets = []
        for sheet in book.sheets():
            max_row = sheet.nrows
            max_col = sheet.ncols
            if max_row == 0 or max_col == 0:
                continue
            grid = []
            for r in range(max_row):
                row_vals = []
                for c in range(max_col):
                    cell = sheet.cell(r, c)
                    ctype = cell.ctype
                    val = cell.value
                    if ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR):
                        row_vals.append(None)
                    elif ctype == xlrd.XL_CELL_DATE:
                        try:
                            row_vals.append(xlrd.xldate.xldate_as_datetime(val, book.datemode).isoformat())
                        except Exception:
                            row_vals.append(val)
                    elif ctype == xlrd.XL_CELL_BOOLEAN:
                        row_vals.append(bool(val))
                    elif ctype == xlrd.XL_CELL_NUMBER:
                        try:
                            fval = float(val)
                            row_vals.append(int(fval) if fval.is_integer() else fval)
                        except Exception:
                            row_vals.append(val)
                    else:
                        row_vals.append(val)
                grid.append(row_vals)
            merged = [
                (rlo, rhi, clo, chi)
                for (rlo, rhi, clo, chi) in getattr(sheet, "merged_cells", ())
                if rlo < 15
            ]
            sheets.append((sheet.name, grid, merged))
        return sheets

    return None


def _convert_excel_multirow_headers_to_markdown(
    input_path: str,
    file_bytes: Optional[bytes] = None,
) -> Optional[str]:
    """Flatten multi-row / merged headers in geoscience Excel workbooks (.xlsx / .xls)
    when MarkItDown's default header=0 produces ``Unnamed: N`` placeholder columns.
    Reads the workbook in memory without modifying the original file.
    """
    sheets = _iter_excel_sheets(input_path, file_bytes=file_bytes)
    if not sheets:
        return None

    sections: List[str] = []
    for sheet_name, grid, merged_ranges in sheets:
        max_row = len(grid)
        max_col = len(grid[0]) if grid else 0
        if max_row == 0 or max_col == 0:
            continue

        # Expand merged cells across the top header candidate zone (first 15 rows)
        for rlo, rhi, clo, chi in merged_ranges:
            if rlo >= max_row or clo >= max_col:
                continue
            top_val = grid[rlo][clo]
            for rr in range(rlo, min(rhi, max_row)):
                for cc in range(clo, min(chi, max_col)):
                    if grid[rr][cc] is None:
                        grid[rr][cc] = top_val

        # Trim trailing completely empty columns
        used_cols = 0
        for row in grid:
            for idx in range(len(row) - 1, -1, -1):
                if _cell_str(row[idx]):
                    used_cols = max(used_cols, idx + 1)
                    break
        if used_cols == 0:
            continue
        grid = [row[:used_cols] for row in grid]

        # Identify leading non-empty rows and separate banner/title lines from table headers
        notes: List[str] = []
        row_idx = 0
        while row_idx < len(grid):
            non_empty = [(_cell_str(v), v) for v in grid[row_idx] if _cell_str(v)]
            if not non_empty:
                row_idx += 1
                continue
            # A single unique string spanning row (or >=50% of cols / col-0 banner when table has >=3 cols) is a title banner
            unique_texts = list(dict.fromkeys(t for t, _ in non_empty))
            if (
                used_cols >= 3
                and len(unique_texts) == 1
                and (_cell_str(grid[row_idx][0]) or len(non_empty) == used_cols)
                and (len(non_empty) == 1 or len(non_empty) >= max(2, used_cols // 2))
            ):
                notes.append(unique_texts[0])
                row_idx += 1
                continue
            break

        if row_idx >= len(grid):
            sections.append(f"## {sheet_name}\n\n" + "\n\n".join(notes))
            continue

        # Collect 1..3 consecutive header rows before the first data row
        header_rows = [grid[row_idx]]
        scan = row_idx + 1
        while scan < len(grid) and len(header_rows) < 3:
            candidate_row = grid[scan]
            non_empty_vals = [v for v in candidate_row if _cell_str(v)]
            if not non_empty_vals:
                break
            num_ratio = sum(1 for v in non_empty_vals if _is_numeric_or_data_cell(v)) / len(non_empty_vals)
            if num_ratio >= 0.4:
                break
            # Stop if previous row already had no gaps/repeats and this row looks like first categorical data row
            prev_texts = [_cell_str(v) for v in header_rows[-1]]
            # A single populated cell at col 0 below a multi-column header whose col 0
            # is empty (e.g. "MAT6pir_1" group divider row below ['', 'Type', 'Ti', ...])
            # is a sample/group subheader row, not a column header.
            if (
                len(non_empty_vals) == 1
                and _cell_str(candidate_row[0])
                and not prev_texts[0]
                and sum(1 for t in prev_texts if t) >= 2
                and _cell_str(candidate_row[0]).lower() not in {"(wt%)", "wt%", "(ppm)", "ppm", "unit", "units"}
            ):
                break
            has_merged_repeat = any(
                i > 0 and prev_texts[i] and prev_texts[i] == prev_texts[i - 1]
                for i in range(len(prev_texts))
            )
            prev_blanks = sum(1 for t in prev_texts if not t)
            if not has_merged_repeat and prev_blanks == 0 and scan > row_idx + 1:
                break
            if has_merged_repeat or prev_blanks > 0:
                header_rows.append(candidate_row)
                scan += 1
            else:
                # Peek next row: only treat candidate_row as a unit/sub-header row when it has
                # blanks or unit-like tokens (e.g. ppm, wt%, Ma) and the following row has numeric data.
                cand_texts = [_cell_str(v) for v in candidate_row]
                has_unit_hint = any(
                    not t or t.lower() in {"%", "wt%", "wt.%", "ppm", "ppb", "ma", "ka", "ga", "g/t", "mg/kg", "ug/g", "μg/g", "°c", "k", "m", "km", "unit", "units"}
                    or (t.startswith("(") and t.endswith(")"))
                    for t in cand_texts
                )
                if has_unit_hint and scan + 1 < len(grid):
                    next_vals = [v for v in grid[scan + 1] if _cell_str(v)]
                    if next_vals and sum(1 for v in next_vals if _is_numeric_or_data_cell(v)) / len(next_vals) >= 0.4:
                        header_rows.append(candidate_row)
                        scan += 1
                        continue
                break

        headers: List[str] = []
        for c_idx in range(used_cols):
            parts: List[str] = []
            for h_row in header_rows:
                part = _cell_str(h_row[c_idx])
                if part and (not parts or part != parts[-1]):
                    parts.append(part)
            headers.append(" / ".join(parts) if parts else f"Col {c_idx + 1}")

        data_start = row_idx + len(header_rows)
        lines = [f"## {sheet_name}"]
        for note in notes:
            lines.append(f"\n> {note}")
        lines.append("")
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join(["---"] * used_cols) + " |")
        for r_idx in range(data_start, len(grid)):
            cells = [_cell_str(v) for v in grid[r_idx]]
            if not any(cells):
                continue
            lines.append("| " + " | ".join(cells) + " |")
        sections.append("\n".join(lines))

    if not sections:
        return None
    return "\n\n".join(sections) + "\n"


def _convert_zip_with_flattened_excel(input_path: str, md_converter) -> Optional[str]:
    """Convert a ``.zip`` archive to Markdown while flattening multi-row Excel headers
    for any ``.xlsx`` / ``.xls`` entries inside the archive."""
    if not input_path.lower().endswith(".zip"):
        return None
    try:
        zf = zipfile.ZipFile(input_path, "r")
    except Exception:
        return None

    entries_md: List[str] = []
    with zf:
        for zinfo in zf.infolist():
            if zinfo.is_dir():
                continue
            name = zinfo.filename
            # Skip macOS metadata entries inside archives
            base = os.path.basename(name)
            if name.startswith("__MACOSX/") or base.startswith("._"):
                continue
            ext = os.path.splitext(base)[1].lower()
            try:
                raw_bytes = zf.read(zinfo.filename)
            except Exception:
                continue

            entry_text: Optional[str] = None
            if ext in (".xlsx", ".xls"):
                improved = _convert_excel_multirow_headers_to_markdown(base, file_bytes=raw_bytes)
                if improved and "Unnamed:" not in improved:
                    entry_text = improved
            if entry_text is None and md_converter is not None:
                try:
                    res = md_converter.convert_stream(io.BytesIO(raw_bytes), file_extension=ext or None)
                    text = res.text_content if hasattr(res, "text_content") else str(res)
                    if ext in (".xlsx", ".xls") and "Unnamed:" in text:
                        improved = _convert_excel_multirow_headers_to_markdown(base, file_bytes=raw_bytes)
                        if improved:
                            text = improved
                    entry_text = text
                except Exception:
                    continue

            if entry_text and entry_text.strip():
                entries_md.append(f"### File: `{name}`\n\n{entry_text.strip()}")

    if not entries_md:
        return None
    return f"## Content from the zip file `{os.path.basename(input_path)}`\n\n" + "\n\n".join(entries_md) + "\n"


def convert_file_to_markdown(
    input_path: str,
    output_path: Optional[str] = None,
    llm_client=None,
    llm_model: Optional[str] = None,
    converter=None,
    max_file_size_bytes: int = MAX_CONVERTIBLE_FILE_SIZE,
) -> Optional[str]:
    """
    Convert a single downloaded supplementary file to Markdown.
    
    Args:
        input_path: Path to the downloaded file (.xlsx, .docx, .pdf, etc.)
        output_path: Optional destination path for .md output. If None, saves as <input_path>.md.
        llm_client: Optional LLM client for MarkItDown image/document description.
        llm_model: Optional LLM model name.
        converter: Optional pre-instantiated MarkItDown instance to avoid repeated setup.
        max_file_size_bytes: Threshold to skip giant datasets/archives from memory blowout.

    Returns:
        The markdown text on success, or None if conversion failed or markitdown is not installed.
    """
    if not os.path.exists(input_path):
        return None

    if not is_markitdown_available():
        print("  [markitdown] Note: 'markitdown' is not installed. Run 'pip install -r requirements-conversion.txt' to enable Markdown conversion.")
        return None

    # Guard against huge scientific packages / raw archives blowing up memory
    try:
        file_size = os.path.getsize(input_path)
        if input_path.lower().endswith(".zip") and zipfile.is_zipfile(input_path):
            with zipfile.ZipFile(input_path) as archive:
                expanded_size = sum(member.file_size for member in archive.infolist())
            if expanded_size > max_file_size_bytes:
                print(f"  [markitdown] Notice: Skipping archive whose expanded size exceeds conversion limit: {input_path}")
                return None
        if file_size > max_file_size_bytes:
            print(f"  [markitdown] Notice: Skipping {os.path.basename(input_path)} ({file_size / (1024*1024):.1f} MB > {max_file_size_bytes / (1024*1024):.0f} MB limit) to prevent memory exhaustion.")
            return None
    except Exception:
        pass

    try:
        if converter is not None:
            md = converter
        else:
            from markitdown import MarkItDown
            kwargs = {}
            if llm_client:
                kwargs["llm_client"] = llm_client
                if llm_model:
                    kwargs["llm_model"] = llm_model
            md = MarkItDown(**kwargs)

        result = md.convert(input_path)
        content = result.text_content if hasattr(result, "text_content") else str(result)

        lower_in = input_path.lower()
        if lower_in.endswith((".xlsx", ".xls")) and "Unnamed:" in content:
            improved = _convert_excel_multirow_headers_to_markdown(input_path)
            if improved and "Unnamed:" not in improved:
                content = improved
        elif lower_in.endswith(".zip") and "Unnamed:" in content:
            improved = _convert_zip_with_flattened_excel(input_path, md)
            if improved and "Unnamed:" not in improved:
                content = improved

        dest = output_path or f"{input_path}.md"
        with open(dest, "w", encoding="utf-8") as f:
            f.write(content)

        return content
    except Exception as e:
        print(f"  [markitdown] Warning: failed to convert {input_path} to markdown: {e}")
        return None


def convert_directory_supplements(
    target_dir: str,
    create_index: bool = True,
    overwrite: bool = False,
) -> List[Tuple[str, str]]:
    """
    Batch convert all eligible supplementary files in a directory to Markdown sidecars.
    
    Args:
        target_dir: Output folder containing downloaded supplementary files.
        create_index: Whether to generate an aggregated SUPPLEMENTS_MARKDOWN_SUMMARY.md.
        overwrite: Whether to force re-conversion if .md sidecar already exists.

    Returns:
        List of (original_file_path, markdown_file_path) tuples.
    """
    if not os.path.isdir(target_dir):
        return []

    if not is_markitdown_available():
        print("  [markitdown] Notice: 'markitdown' library not installed. Skipping markdown conversion.")
        return []

    # Reuse a single MarkItDown instance across the batch to avoid repeated initialization
    shared_converter = None
    try:
        from markitdown import MarkItDown
        shared_converter = MarkItDown()
    except Exception:
        shared_converter = None

    converted: List[Tuple[str, str]] = []
    summary_entries: List[str] = []

    for fname in sorted(os.listdir(target_dir)):
        # Skip sidecar markdown files, hidden files, or manifests
        base_lower = fname.lower()
        if (
            fname.startswith(".")
            or fname.endswith(".md")
            or base_lower in IGNORED_FILENAMES
            or base_lower.endswith(("_manifest.json", ".log"))
        ):
            continue
        fpath = os.path.join(target_dir, fname)
        if not os.path.isfile(fpath):
            continue
        if is_convertible_file(fpath):
            out_md = f"{fpath}.md"
            text: Optional[str] = None
            if os.path.exists(out_md) and not overwrite and os.path.getsize(out_md) > 0:
                print(f"  [markitdown] Reusing existing {os.path.basename(out_md)}")
                try:
                    with open(out_md, "r", encoding="utf-8") as f:
                        text = f.read()
                except Exception:
                    text = None
            else:
                print(f"  [markitdown] Converting {fname} -> {os.path.basename(out_md)} ...")
                text = convert_file_to_markdown(fpath, output_path=out_md, converter=shared_converter)

            if text is not None:
                converted.append((fpath, out_md))
                all_lines = text.splitlines()
                preview_lines = all_lines[:15]
                preview = "\n".join(preview_lines)
                truncated_note = "\n\n*... (truncated, see full sidecar)*" if len(all_lines) > 15 else ""
                summary_entries.append(
                    f"### File: {fname}\n"
                    f"- **Source**: `{fname}`\n"
                    f"- **Markdown Sidecar**: `[{os.path.basename(out_md)}]({os.path.basename(out_md)})`\n\n"
                    f"{preview}{truncated_note}\n"
                )

    if create_index and summary_entries:
        index_path = os.path.join(target_dir, "SUPPLEMENTS_MARKDOWN_SUMMARY.md")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write("# Supplementary Materials Markdown Summary\n\n")
            f.write("Generated via Microsoft MarkItDown integration.\n\n")
            f.write("\n---\n\n".join(summary_entries))
        print(f"  [markitdown] Created summary index: {index_path}")

    return converted


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Convert supplementary documents (.docx, .xlsx, .pdf, .zip) to Markdown")
    parser.add_argument("target", help="File or directory to convert")
    parser.add_argument("-o", "--output", default=None, help="Output destination for single file conversion")
    parser.add_argument("--overwrite", action="store_true", help="Force re-conversion of existing .md files")
    args = parser.parse_args()

    if not os.path.exists(args.target):
        print(f"Error: Target '{args.target}' does not exist.", file=sys.stderr)
        sys.exit(1)

    if not is_markitdown_available():
        print("Error: 'markitdown' is not installed. Run 'pip install -r requirements-conversion.txt' first.", file=sys.stderr)
        sys.exit(1)

    if os.path.isdir(args.target):
        converted = convert_directory_supplements(args.target, overwrite=args.overwrite)
        print(f"Converted {len(converted)} file(s) in '{args.target}'.")
    else:
        text = convert_file_to_markdown(args.target, output_path=args.output)
        if text is not None:
            dest = args.output or f"{args.target}.md"
            print(f"Converted '{args.target}' -> '{dest}'.")
        else:
            print(f"Failed to convert '{args.target}'.", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
