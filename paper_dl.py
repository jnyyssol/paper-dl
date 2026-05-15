"""
paper_dl.py — Semi-automated academic paper PDF downloader.

Opens each paper from a CSV one by one in the browser, then watches a local
folder for a new PDF to appear. Once detected, it logs the download and moves
to the next paper. You still click "Download PDF" yourself; this script handles
the queue and bookkeeping.

Accepts raw Scopus exports (DOI, Title, Link columns) as well as any CSV with
at least one of: doi, link. Column names are matched case-insensitively.
"""

import argparse
import os
import sys
import time

import pandas as pd
import webbrowser


POLL_INTERVAL = 1  # seconds between folder checks
TEMP_EXTENSIONS = {".crdownload", ".part", ".download"}


def parse_args():
    parser = argparse.ArgumentParser(description="Semi-automated paper PDF downloader.")
    parser.add_argument("csv", help="CSV file with paper metadata")
    parser.add_argument("watch_folder", help="Folder to watch for new PDF downloads")
    return parser.parse_args()


def resolve_url(row):
    """Return the best URL for a row: prefer DOI, fall back to direct link."""
    raw_doi = row.get("doi")
    if pd.notna(raw_doi):
        doi = str(raw_doi).strip().removeprefix("doi:").removeprefix("https://doi.org/")
        if doi and doi.lower() != "nan":
            return f"https://doi.org/{doi}"

    raw_link = row.get("link")
    if pd.notna(raw_link) and str(raw_link).startswith("http"):
        return str(raw_link)

    return None


def list_pdfs(folder):
    """Return the set of complete (non-temporary) PDF filenames in folder."""
    try:
        return {
            f for f in os.listdir(folder)
            if f.lower().endswith(".pdf")
            and not any(f.endswith(ext) for ext in TEMP_EXTENSIONS)
        }
    except FileNotFoundError:
        print(f"[ERROR] Watch folder not found: {folder}")
        sys.exit(1)


def load_completed(history_file, watch_folder):
    """
    Return {row_idx: filename} for rows that should be skipped:
    - PDF filename from history exists in watch_folder → done, skip.
    - SKIPPED_NO_URL → nothing to do, skip.
    - SKIPPED_USER → retry (not included).
    """
    if not os.path.exists(history_file):
        return {}
    try:
        hist = pd.read_csv(history_file)
        existing_pdfs = list_pdfs(watch_folder)
        completed = {}
        for _, entry in hist.iterrows():
            f = str(entry.get("file", ""))
            if f == "SKIPPED_NO_URL" or f in existing_pdfs:
                completed[int(entry["row"])] = f
        return completed
    except Exception:
        return {}


def append_history(history_file, idx, title, filename):
    exists = os.path.exists(history_file)
    pd.DataFrame([{
        "row": idx,
        "title": title,
        "file": filename,
        "time": time.ctime(),
    }]).to_csv(history_file, mode="a", header=not exists, index=False)


def wait_for_pdf(watch_folder, before_files):
    """Block until a new PDF appears. Returns filename, or None if skipped."""
    print("   Waiting for new PDF... (Ctrl+C to skip/quit)")
    try:
        while True:
            time.sleep(POLL_INTERVAL)
            new_files = list_pdfs(watch_folder) - before_files
            if new_files:
                filename = next(iter(new_files))
                try:
                    if os.path.getsize(os.path.join(watch_folder, filename)) > 0:
                        return filename
                except OSError:
                    pass
    except KeyboardInterrupt:
        print()
        choice = input("   [S]kip, [Q]uit, or Enter to retry: ").strip().lower()
        if choice == "q":
            sys.exit(0)
        elif choice == "s":
            return None
        else:
            return wait_for_pdf(watch_folder, before_files)


def main():
    args = parse_args()

    try:
        df = pd.read_csv(args.csv)
    except Exception as e:
        print(f"[ERROR] Could not read CSV: {e}")
        sys.exit(1)

    # Normalize column names so Scopus exports (DOI, Title, Link) and
    # hand-crafted CSVs (doi, title, link) both work without preprocessing.
    df.columns = df.columns.str.strip().str.lower()

    history_file = os.path.splitext(args.csv)[0] + "_history.csv"
    completed = load_completed(history_file, args.watch_folder)
    remaining = len(df) - len(completed)

    print("-" * 60)
    print(f"CSV: {args.csv}  ({len(df)} rows, {len(completed)} done, {remaining} to go)")
    print(f"Watch: {args.watch_folder}  ({len(list_pdfs(args.watch_folder))} PDFs present)")
    print(f"Log:   {history_file}")
    print("-" * 60)

    for i, row in df.iterrows():
        if i in completed:
            print(f"[{i}] Already have: {completed[i]}")
            continue

        url = resolve_url(row)
        title = str(row.get("title", "Untitled"))[:60]

        if not url:
            print(f"[{i}] No usable URL — skipping.")
            append_history(history_file, i, title, "SKIPPED_NO_URL")
            continue

        print(f"\n[{i}/{len(df) - 1}] {title}")
        print(f"   {url}")
        webbrowser.open_new_tab(url)

        before_files = list_pdfs(args.watch_folder)
        filename = wait_for_pdf(args.watch_folder, before_files)

        if filename:
            print(f"   [+] Saved: {filename}")
            append_history(history_file, i, title, filename)
        else:
            print(f"   [~] Skipped.")
            append_history(history_file, i, title, "SKIPPED_USER")

    print("\nAll done.")


if __name__ == "__main__":
    main()
