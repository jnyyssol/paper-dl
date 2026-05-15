"""
paper_dl_gui.py — Tkinter GUI for paper-dl.
"""

import os
import sys
import time
import queue
import threading
import webbrowser

import pandas as pd
import tkinter as tk
from tkinter import ttk, filedialog

POLL_INTERVAL = 1
TEMP_EXTENSIONS = {".crdownload", ".part", ".download"}

STATUS_LABEL = {
    "pending":      "—",
    "waiting":      "⏳  waiting...",
    "done":         "✓  done",
    "skipped_user": "↩  skipped",
    "no_url":       "✗  no URL",
}


# ── shared logic (mirrors paper_dl.py) ────────────────────────────────────────

def list_pdfs(folder):
    try:
        return {
            f for f in os.listdir(folder)
            if f.lower().endswith(".pdf")
            and not any(f.endswith(ext) for ext in TEMP_EXTENSIONS)
        }
    except FileNotFoundError:
        return set()


def resolve_url(row):
    raw_doi = row.get("doi")
    if pd.notna(raw_doi):
        doi = str(raw_doi).strip().removeprefix("doi:").removeprefix("https://doi.org/")
        if doi and doi.lower() != "nan":
            return f"https://doi.org/{doi}"
    raw_link = row.get("link")
    if pd.notna(raw_link) and str(raw_link).startswith("http"):
        return str(raw_link)
    return None


def load_completed(history_file, watch_folder):
    """Return {row_idx: filename} for rows whose PDF exists or had no URL."""
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
        "row": idx, "title": title,
        "file": filename, "time": time.ctime(),
    }]).to_csv(history_file, mode="a", header=not exists, index=False)


# ── GUI ────────────────────────────────────────────────────────────────────────

class PaperDLApp:
    def __init__(self, root):
        self.root = root
        self.root.title("paper-dl")
        self.root.minsize(720, 520)

        self.skip_event = threading.Event()
        self.quit_event = threading.Event()
        self.ui_queue  = queue.Queue()

        self.df           = None
        self.statuses     = {}   # row_idx -> (status_key, detail_str)
        self.row_items    = {}   # row_idx -> treeview iid
        self.history_file = None
        self.watch_folder = None

        self._build_ui()
        self._poll_queue()

    # ── layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # config panel
        cfg = ttk.LabelFrame(self.root, text="Configuration", padding=8)
        cfg.pack(fill="x", padx=10, pady=(10, 0))

        ttk.Label(cfg, text="CSV:").grid(row=0, column=0, sticky="w")
        self.csv_var = tk.StringVar()
        self.csv_entry = ttk.Entry(cfg, textvariable=self.csv_var)
        self.csv_entry.grid(row=0, column=1, padx=4, sticky="ew")
        ttk.Button(cfg, text="…", width=3, command=self._browse_csv).grid(row=0, column=2)

        ttk.Label(cfg, text="Watch folder:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.folder_var = tk.StringVar()
        self.folder_entry = ttk.Entry(cfg, textvariable=self.folder_var)
        self.folder_entry.grid(row=1, column=1, padx=4, sticky="ew", pady=(4, 0))
        ttk.Button(cfg, text="…", width=3, command=self._browse_folder).grid(row=1, column=2, pady=(4, 0))
        cfg.columnconfigure(1, weight=1)

        self.start_btn = ttk.Button(self.root, text="Start", command=self._start)
        self.start_btn.pack(pady=6)

        # paper list
        list_frame = ttk.Frame(self.root)
        list_frame.pack(fill="both", expand=True, padx=10)

        cols = ("idx", "title", "status")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="none")
        self.tree.heading("idx",    text="#")
        self.tree.heading("title",  text="Title")
        self.tree.heading("status", text="Status")
        self.tree.column("idx",    width=46,  stretch=False, anchor="center")
        self.tree.column("title",  width=520)
        self.tree.column("status", width=130, stretch=False, anchor="center")

        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.tree.tag_configure("pending",      background="#F5F5F5", foreground="#555555")
        self.tree.tag_configure("waiting",      background="#FFF9C4", foreground="#6D5500")
        self.tree.tag_configure("done",         background="#C8E6C9", foreground="#1B5E20")
        self.tree.tag_configure("skipped_user", background="#FFE0B2", foreground="#BF360C")
        self.tree.tag_configure("no_url",       background="#EEEEEE", foreground="#9E9E9E")

        # bottom bar — always visible
        bar = ttk.Frame(self.root, padding=(10, 6))
        bar.pack(fill="x", side="bottom")

        self.summary_var = tk.StringVar(value="Load a CSV to begin.")
        ttk.Label(bar, textvariable=self.summary_var).pack(side="left")

        self.stop_btn = ttk.Button(bar, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="right", padx=(4, 0))
        self.skip_btn = ttk.Button(bar, text="Skip", command=self._skip, state="disabled")
        self.skip_btn.pack(side="right")

        self.topmost_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Always on top", variable=self.topmost_var,
                        command=self._toggle_topmost).pack(side="right", padx=(0, 12))

    # ── browse callbacks ──────────────────────────────────────────────────────

    def _browse_csv(self):
        p = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if p:
            self.csv_var.set(p)

    def _browse_folder(self):
        p = filedialog.askdirectory()
        if p:
            self.folder_var.set(p)

    # ── start ─────────────────────────────────────────────────────────────────

    def _start(self):
        csv_path     = self.csv_var.get().strip()
        watch_folder = self.folder_var.get().strip()

        if not csv_path or not os.path.exists(csv_path):
            self.summary_var.set("CSV file not found."); return
        if not watch_folder or not os.path.exists(watch_folder):
            self.summary_var.set("Watch folder not found."); return

        try:
            df = pd.read_csv(csv_path)
        except Exception as e:
            self.summary_var.set(f"Error reading CSV: {e}"); return

        df.columns = df.columns.str.strip().str.lower()
        self.df           = df
        self.watch_folder = watch_folder
        self.history_file = os.path.splitext(csv_path)[0] + "_history.csv"

        completed = load_completed(self.history_file, watch_folder)

        # Build initial statuses and populate tree
        self.statuses  = {}
        self.row_items = {}
        self.tree.delete(*self.tree.get_children())

        for i, row in df.iterrows():
            title = str(row.get("title", "Untitled"))[:80]
            if i in completed:
                status, detail = "done", completed[i]
            elif not resolve_url(row):
                status, detail = "no_url", ""
            else:
                status, detail = "pending", ""
            self.statuses[i] = (status, detail)
            iid = self.tree.insert("", "end",
                values=(i, title, STATUS_LABEL[status]), tags=(status,))
            self.row_items[i] = iid

        self._update_summary()

        # Lock config fields
        self.csv_entry.config(state="disabled")
        self.folder_entry.config(state="disabled")
        self.start_btn.config(state="disabled")
        self.skip_btn.config(state="normal")
        self.stop_btn.config(state="normal")

        self.skip_event.clear()
        self.quit_event.clear()
        threading.Thread(target=self._worker, daemon=True).start()

    # ── background worker ─────────────────────────────────────────────────────

    def _worker(self):
        for i, row in self.df.iterrows():
            if self.quit_event.is_set():
                break

            status, _ = self.statuses.get(i, ("pending", ""))
            if status in ("done", "no_url"):
                self.ui_queue.put(("scroll_to", i))
                continue

            title = str(row.get("title", "Untitled"))[:60]
            self.skip_event.clear()
            self.ui_queue.put(("set_status", i, "waiting", ""))
            webbrowser.open_new_tab(resolve_url(row))

            before = list_pdfs(self.watch_folder)
            filename = self._wait_for_pdf(before)

            if self.quit_event.is_set():
                break

            if filename:
                append_history(self.history_file, i, title, filename)
                self.ui_queue.put(("set_status", i, "done", filename))
            else:
                append_history(self.history_file, i, title, "SKIPPED_USER")
                self.ui_queue.put(("set_status", i, "skipped_user", ""))

        self.ui_queue.put(("finished",))

    def _wait_for_pdf(self, before_files):
        while not self.quit_event.is_set() and not self.skip_event.is_set():
            time.sleep(POLL_INTERVAL)
            new_files = list_pdfs(self.watch_folder) - before_files
            if new_files:
                filename = next(iter(new_files))
                try:
                    if os.path.getsize(os.path.join(self.watch_folder, filename)) > 0:
                        return filename
                except OSError:
                    pass
        return None

    # ── queue → UI ────────────────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                self._handle(self.ui_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "set_status":
            _, i, status, detail = msg
            self.statuses[i] = (status, detail)
            iid = self.row_items[i]
            title = self.tree.item(iid, "values")[1]
            self.tree.item(iid, values=(i, title, STATUS_LABEL[status]), tags=(status,))
            self.tree.see(iid)
            self._update_summary()
        elif kind == "scroll_to":
            self.tree.see(self.row_items[msg[1]])
        elif kind == "finished":
            self.summary_var.set(self._summary_text() + "   — all done.")
            self._reset_controls()

    # ── summary ───────────────────────────────────────────────────────────────

    def _summary_text(self):
        counts = {"done": 0, "skipped_user": 0, "no_url": 0, "pending": 0, "waiting": 0}
        for s, _ in self.statuses.values():
            counts[s] = counts.get(s, 0) + 1
        to_go = counts["pending"] + counts["waiting"]
        return (f"Done: {counts['done']}   "
                f"Skipped: {counts['skipped_user']}   "
                f"No URL: {counts['no_url']}   "
                f"To go: {to_go}")

    def _update_summary(self):
        self.summary_var.set(self._summary_text())

    # ── button actions ────────────────────────────────────────────────────────

    def _toggle_topmost(self):
        self.root.attributes("-topmost", self.topmost_var.get())

    def _skip(self):
        self.skip_event.set()

    def _stop(self):
        self.quit_event.set()
        self.skip_event.set()   # unblock any active wait immediately
        # drain stale queue messages so the reset is clean
        while not self.ui_queue.empty():
            try:
                self.ui_queue.get_nowait()
            except queue.Empty:
                break
        self._reset_controls()
        self.summary_var.set("Stopped. Edit paths or press Start again.")

    def _reset_controls(self):
        self.csv_entry.config(state="normal")
        self.folder_entry.config(state="normal")
        self.start_btn.config(state="normal")
        self.skip_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")


def main():
    root = tk.Tk()
    PaperDLApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
