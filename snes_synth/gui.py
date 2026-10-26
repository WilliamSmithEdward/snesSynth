"""Desktop frontend for the local SNES audio extraction pipeline."""

from __future__ import annotations

import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any


BG = "#11141c"
PANEL = "#1a1f2b"
FIELD = "#222938"
INK = "#edf0f8"
MUTED = "#a2aec4"
ACCENT = "#9cf0cb"
LINE = "#333d50"


def _track_ids(value: str) -> list[int] | None:
    """Parse explicit IDs without silently changing their order or numbering."""
    if not value.strip():
        return None
    try:
        values = [int(item.strip(), 16 if item.strip().lower().startswith("0x") else 10)
                  for item in value.split(",")]
    except ValueError as exc:
        raise ValueError("Track IDs must be comma-separated numbers, such as 1,2,3.") from exc
    if any(value < 0 for value in values):
        raise ValueError("Track IDs cannot be negative.")
    return list(dict.fromkeys(values))


class SynthApp:
    """One background operation at a time; all widget access stays on the UI thread."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("SNES Synth · ROM audio studio")
        self.root.geometry("1100x850")
        self.root.minsize(920, 760)
        self.root.configure(background=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self.controls: list[tuple[ttk.Widget, str]] = []
        self.last_output: Path | None = None

        default_source = Path.cwd() / "game.sfc"
        self.source = tk.StringVar(value=str(default_source) if default_source.exists() else "")
        self.output = tk.StringVar(value=str(Path.cwd() / "output" / "game"))
        self.duration = tk.StringVar(value="auto")
        self.fade = tk.StringVar(value="8")
        self.tracks = tk.StringVar()
        self.stems = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Ready to analyze a ROM or SPC file")
        self.meta_title = tk.StringVar(value="Your soundtrack starts here")
        self.meta_driver = tk.StringVar(value="Analyze a file to identify its audio driver and available tracks.")
        self.meta_details = tk.StringVar(value="No file analyzed yet")
        self._style()
        self._build()
        self.root.after(80, self._drain)

    def _style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=INK, font=("Segoe UI", 10),
                        bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Card.TLabel", background=PANEL)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("CardMuted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Eyebrow.TLabel", foreground=ACCENT, font=("Segoe UI Semibold", 9))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 30))
        style.configure("CardTitle.TLabel", background=PANEL, font=("Segoe UI Semibold", 14))
        style.configure("TEntry", fieldbackground=FIELD, foreground=INK, insertcolor=INK,
                        padding=9, borderwidth=1)
        style.map("TEntry", foreground=[("disabled", MUTED)],
                  fieldbackground=[("disabled", PANEL)])
        style.configure("TButton", background=FIELD, foreground=INK, padding=(15, 10),
                        borderwidth=0, font=("Segoe UI Semibold", 10))
        style.map("TButton", background=[("active", LINE), ("disabled", PANEL)],
                  foreground=[("disabled", "#66728a")])
        style.configure("Primary.TButton", background=ACCENT, foreground="#10251e")
        style.map("Primary.TButton", background=[("active", "#c2ffe4"), ("disabled", LINE)],
                  foreground=[("disabled", "#7e8b9f")])
        style.configure("TCheckbutton", background=PANEL, foreground=INK, padding=(0, 4))
        style.map("TCheckbutton", background=[("active", PANEL)],
                  foreground=[("disabled", MUTED)])
        style.configure("Horizontal.TProgressbar", troughcolor=FIELD, background=ACCENT,
                        borderwidth=0, thickness=3)
        style.configure("Vertical.TScrollbar", background=LINE, troughcolor=BG,
                        borderwidth=0, arrowsize=12)

    def _control(self, widget: ttk.Widget, idle_state: str = "normal") -> ttk.Widget:
        self.controls.append((widget, idle_state))
        return widget

    def _build(self) -> None:
        main = ttk.Frame(self.root, padding=(30, 24, 30, 18))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(4, weight=1)

        heading = ttk.Frame(main)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 20))
        ttk.Label(heading, text="LOCAL AUDIO STUDIO", style="Eyebrow.TLabel").pack(anchor="w")
        ttk.Label(heading, text="SNES Synth", style="Title.TLabel").pack(anchor="w", pady=(2, 3))
        ttk.Label(heading, text="Preserve the playback. Explore the score. Reuse the instruments.",
                  style="Muted.TLabel").pack(anchor="w")

        source_card = ttk.Frame(main, style="Card.TFrame", padding=18)
        source_card.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        source_card.columnconfigure(1, weight=1)
        ttk.Label(source_card, text="Source file", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 15))
        self._control(ttk.Entry(source_card, textvariable=self.source)).grid(
            row=0, column=1, sticky="ew", padx=(0, 10))
        self._control(ttk.Button(source_card, text="Browse…", command=self._pick_source)).grid(
            row=0, column=2)
        ttk.Label(source_card, text="Export folder", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 15), pady=(10, 0))
        self._control(ttk.Entry(source_card, textvariable=self.output)).grid(
            row=1, column=1, sticky="ew", padx=(0, 10), pady=(10, 0))
        self._control(ttk.Button(source_card, text="Choose…", command=self._pick_output)).grid(
            row=1, column=2, pady=(10, 0))

        options = ttk.Frame(main, style="Card.TFrame", padding=18)
        options.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        options.columnconfigure(0, weight=1)
        options.columnconfigure(1, weight=1)
        options.columnconfigure(2, weight=2)
        for col, text in enumerate(("Length / seconds or auto", "Fade / seconds", "Track IDs / empty = all")):
            ttk.Label(options, text=text, style="CardMuted.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, 16))
        for col, variable in enumerate((self.duration, self.fade, self.tracks)):
            self._control(ttk.Entry(options, textvariable=variable, width=12)).grid(
                row=1, column=col, sticky="ew", padx=(0, 16) if col < 2 else 0, pady=(5, 8))
        self._control(ttk.Checkbutton(options, text="Also render 8 individual voice stems",
                                     variable=self.stems)).grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Label(options, text="Auto length follows the converted score. WAV emulates the original sound processor; MIDI + SF2 provides the editable score and sample bank.",
                  style="CardMuted.TLabel", wraplength=950).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(7, 0))

        info = ttk.Frame(main, style="Card.TFrame", padding=18)
        info.grid(row=3, column=0, sticky="ew", pady=(0, 14))
        info.columnconfigure(0, weight=1)
        ttk.Label(info, textvariable=self.meta_title, style="CardTitle.TLabel", wraplength=900).grid(
            row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.meta_driver, style="CardMuted.TLabel", wraplength=920).grid(
            row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Label(info, textvariable=self.meta_details, style="CardMuted.TLabel", wraplength=920,
                  font=("Consolas", 9)).grid(row=2, column=0, sticky="w", pady=(8, 0))

        log_box = ttk.Frame(main)
        log_box.grid(row=4, column=0, sticky="nsew")
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(1, weight=1)
        ttk.Label(log_box, text="ACTIVITY", style="Eyebrow.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 7))
        self.log = tk.Text(log_box, background="#0c1017", foreground="#c3d0e3",
                           insertbackground=INK, relief="flat", borderwidth=0,
                           highlightthickness=1, highlightbackground=LINE,
                           font=("Consolas", 9), wrap="word", height=7,
                           padx=12, pady=10, state="disabled")
        self.log.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        self._append("Choose a ROM or SPC, then analyze it. All processing stays on this computer.")

        self.progress = ttk.Progressbar(main, mode="indeterminate")
        self.progress.grid(row=5, column=0, sticky="ew", pady=(15, 9))
        footer = ttk.Frame(main)
        footer.grid(row=6, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel", wraplength=430).grid(
            row=0, column=0, sticky="w", padx=(0, 12))
        self.open_button = ttk.Button(footer, text="Open exports", command=self._open_output,
                                      state="disabled")
        self.open_button.grid(row=0, column=1, padx=(0, 8))
        self._control(ttk.Button(footer, text="Analyze", command=self._analyze)).grid(
            row=0, column=2, padx=(0, 8))
        self._control(ttk.Button(footer, text="Extract soundtrack", style="Primary.TButton",
                                command=self._extract)).grid(row=0, column=3)

    def _pick_source(self) -> None:
        selected = filedialog.askopenfilename(
            title="Choose a SNES ROM or SPC file",
            filetypes=[("SNES ROM / audio", "*.sfc *.smc *.spc"), ("All files", "*.*")])
        if selected:
            source = Path(selected)
            self.source.set(str(source))
            self.output.set(str(source.parent / "output" / source.stem))

    def _pick_output(self) -> None:
        selected = filedialog.askdirectory(title="Choose an export folder", mustexist=False)
        if selected:
            self.output.set(selected)

    def _input_path(self) -> Path:
        value = self.source.get().strip()
        if not value:
            raise ValueError("Choose a ROM or SPC file first.")
        source = Path(value).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"Source file does not exist: {source}")
        return source

    def _analyze(self) -> None:
        try:
            source = self._input_path()
        except (ValueError, OSError) as exc:
            messagebox.showerror("Check the source", str(exc), parent=self.root)
            return
        self._start("Analyzing audio data…", "analyze", (source,))

    def _extract(self) -> None:
        try:
            source = self._input_path()
            if not self.output.get().strip():
                raise ValueError("Choose an export folder first.")
            output = Path(self.output.get().strip()).expanduser().resolve()
            if output.is_file():
                raise ValueError("The export folder points to a file. Choose a directory.")
            duration_text = self.duration.get().strip().lower()
            duration = None if duration_text == "auto" else float(duration_text)
            fade = float(self.fade.get())
            if duration is not None and (not math.isfinite(duration) or not 0 < duration <= 3600):
                raise ValueError("Length must be auto or a number of seconds greater than zero and at most 3600.")
            if not math.isfinite(fade) or fade < 0 or fade > (duration if duration is not None else 3600):
                raise ValueError("Fade must be nonnegative and no longer than the selected length (at most 3600 seconds).")
            tracks = _track_ids(self.tracks.get())
            stems = self.stems.get()
        except (ValueError, OSError) as exc:
            messagebox.showerror("Check export settings", str(exc), parent=self.root)
            return
        self._start("Extracting soundtrack…", "extract", (source, output, duration, fade, stems, tracks))

    def _start(self, status: str, operation: str, args: tuple[Any, ...]) -> None:
        if self.busy:
            return
        self.busy = True
        self.status.set(status)
        for control, _ in self.controls:
            control.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.progress.start(12)
        self._append(status)
        threading.Thread(target=self._worker, args=(operation, args), daemon=True).start()

    def _worker(self, operation: str, args: tuple[Any, ...]) -> None:
        try:
            from .pipeline import analyze, extract

            if operation == "analyze":
                result = analyze(args[0])
            else:
                source, output, duration, fade, stems, tracks = args
                result = extract(source, output, duration=duration, fade=fade, stems=stems,
                                 tracks=tracks, log=lambda text: self.events.put(("log", str(text))))
            self.events.put(("complete", (operation, result, args)))
        except Exception as exc:
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def _finish(self) -> None:
        self.busy = False
        self.progress.stop()
        for control, idle_state in self.controls:
            control.configure(state=idle_state)
        if self.last_output and self.last_output.is_dir():
            self.open_button.configure(state="normal")

    def _drain(self) -> None:
        try:
            while True:
                event, data = self.events.get_nowait()
                if event == "log":
                    self._append(data)
                elif event == "error":
                    self._finish()
                    self.status.set("Operation failed — see activity for details")
                    self._append(f"ERROR: {data}")
                    messagebox.showerror("SNES Synth", data, parent=self.root)
                elif event == "complete":
                    operation, result, args = data
                    if operation == "analyze":
                        self._show_analysis(result)
                        self.status.set("Analysis complete")
                    else:
                        self.last_output = args[1]
                        if isinstance(result, dict) and isinstance(result.get("source"), dict):
                            self._show_analysis(result["source"], list_tracks=False)
                        if isinstance(result, dict) and result.get("status") == "partial":
                            complete = result.get("complete_tracks", 0)
                            count = result.get("track_count", len(result.get("tracks", [])))
                            self.status.set(f"Partial export: {complete}/{count} tracks complete — see activity")
                            self._append("Some exports failed. Available files were retained; see manifest.json for each track's errors.")
                        else:
                            self.status.set("Extraction complete")
                        self._append(f"Exports saved to {self.last_output}")
                        if isinstance(result, dict):
                            for warning in result.get("warnings", []):
                                self._append(f"Note: {warning}")
                    self._finish()
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _show_analysis(self, result: dict[str, Any], *, list_tracks: bool = True) -> None:
        title = str(result.get("title") or result.get("game") or "Untitled ROM / SPC")
        driver = str(result.get("driver") or "Unknown driver")
        track_count = result.get("track_count")
        count = f" · {track_count} tracks" if track_count is not None else ""
        self.meta_title.set(title)
        self.meta_driver.set(f"Audio driver: {driver}{count}")
        details = []
        if result.get("mapper"):
            details.append(str(result["mapper"]))
        size = result.get("size")
        if isinstance(size, (float, int)):
            details.append(f"{size:,} bytes")
        digest = result.get("sha256")
        if digest:
            details.append(f"SHA-256 {str(digest)[:20]}…")
        self.meta_details.set("   ·   ".join(details) or "Analysis complete")
        self._append(f"Identified: {title} | {driver}{count}")
        for warning in result.get("warnings", []):
            self._append(f"Note: {warning}")
        if list_tracks and result.get("tracks"):
            self._append("Available track IDs (leave Track IDs empty to export all nonempty tracks):")
            for track in result["tracks"]:
                self._append(f"  {track['id']:02d}  {track.get('title', 'Untitled')} [{track.get('category', 'track')}]")

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", str(text).rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_output(self) -> None:
        if not self.last_output:
            return
        try:
            if os.name == "nt":
                os.startfile(str(self.last_output))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.last_output)])
            else:
                subprocess.Popen(["xdg-open", str(self.last_output)])
        except OSError as exc:
            messagebox.showerror("Open exports", str(exc), parent=self.root)

    def _close(self) -> None:
        if self.busy:
            messagebox.showinfo("Operation in progress", "Keep this window open until the current operation finishes.",
                                parent=self.root)
            return
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    SynthApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
