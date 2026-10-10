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

import os
import sys
from typing import List, Optional, Tuple


CONVERTIBLE_EXTENSIONS = {
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".pdf", ".csv", ".tsv", ".json", ".xml", ".zip"
}


IGNORED_FILENAMES = {
    "download_manifest.json",
    "manifest.json",
    "cookies.json",
    "package.json",
    "supplements_markdown_summary.md",
}


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


def convert_file_to_markdown(
    input_path: str,
    output_path: Optional[str] = None,
    llm_client=None,
    llm_model: Optional[str] = None,
) -> Optional[str]:
    """
    Convert a single downloaded supplementary file to Markdown.
    
    Args:
        input_path: Path to the downloaded file (.xlsx, .docx, .pdf, etc.)
        output_path: Optional destination path for .md output. If None, saves as <input_path>.md.
        llm_client: Optional LLM client for MarkItDown image/document description.
        llm_model: Optional LLM model name.

    Returns:
        The markdown text on success, or None if conversion failed or markitdown is not installed.
    """
    if not os.path.exists(input_path):
        return None

    if not is_markitdown_available():
        print("  [markitdown] Note: 'markitdown' is not installed. Run 'pip install markitdown' to enable Markdown conversion.")
        return None

    try:
        from markitdown import MarkItDown
        kwargs = {}
        if llm_client:
            kwargs["llm_client"] = llm_client
            if llm_model:
                kwargs["llm_model"] = llm_model

        md = MarkItDown(**kwargs)
        result = md.convert(input_path)
        content = result.text_content if hasattr(result, "text_content") else str(result)

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
                text = convert_file_to_markdown(fpath, output_path=out_md)

            if text is not None:
                converted.append((fpath, out_md))
                preview_lines = text.splitlines()[:15]
                preview = "\n".join(preview_lines)
                fence = "~~~~markdown" if "```" in preview else "```markdown"
                fence_end = "~~~~" if "```" in preview else "```"
                summary_entries.append(
                    f"### File: {fname}\n"
                    f"- **Source**: `{fname}`\n"
                    f"- **Markdown Sidecar**: `[{os.path.basename(out_md)}]({os.path.basename(out_md)})`\n\n"
                    f"{fence}\n{preview}\n...\n{fence_end}\n"
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
        print("Error: 'markitdown' is not installed. Run 'pip install markitdown' first.", file=sys.stderr)
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
