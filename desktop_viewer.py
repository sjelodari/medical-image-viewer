#!/usr/bin/env python3
"""Interactive desktop viewer for MRI / CT scans (NIfTI and DICOM).

Usage:
    python mri_viewer.py                       # opens the GUI, pick a folder/file
    python mri_viewer.py scan.nii.gz           # open a NIfTI file
    python mri_viewer.py image.dcm             # open one DICOM file
    python mri_viewer.py path/to/dicom_series/ # open one series
    python mri_viewer.py path/to/patient/      # scan sub-folders, list all series

Check up to 4 series in the list and press "View selected" to see them side by
side in a grid. Scrolling moves every panel through its slices together (synced
by position, so series with different slice counts stay aligned).

Controls:
    Scroll wheel / Up-Down arrows ... move through slices (all panels together)
    A / C / S ....................... axial / coronal / sagittal view
    Right-click drag ................ adjust contrast of the panel under cursor
    R ............................... reset contrast of all panels
    Slice slider .................... jump through slices
"""

import sys
import subprocess
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import Slider, Button, RadioButtons, CheckButtons


# ---------------------------------------------------------------- loading

def load_nifti(path):
    import nibabel as nib

    img = nib.load(str(path))
    # Reorient to RAS+ so axial/coronal/sagittal labels are anatomically correct
    img = nib.as_closest_canonical(img)
    data = np.asanyarray(img.dataobj).astype(np.float32)
    if data.ndim == 4:  # take first volume of a 4D series
        print(f"4D image with {data.shape[3]} volumes - showing volume 0")
        data = data[..., 0]
    zooms = img.header.get_zooms()[:3]
    return data, zooms


def load_dicom(path):
    import pydicom

    path = Path(path)
    files = sorted(path.glob("*")) if path.is_dir() else [path]
    slices = []
    for f in files:
        if f.is_dir():
            continue
        try:
            ds = pydicom.dcmread(str(f))
            if hasattr(ds, "pixel_array"):
                slices.append(ds)
        except Exception:
            continue  # skip non-DICOM files (e.g. DICOMDIR, .DS_Store)

    if not slices:
        raise ValueError(f"No readable DICOM images found in {path}")

    # Sort by position along the slice axis when available
    def sort_key(ds):
        if hasattr(ds, "ImagePositionPatient"):
            return float(ds.ImagePositionPatient[2])
        return float(getattr(ds, "InstanceNumber", 0))

    slices.sort(key=sort_key)

    vol = np.stack([s.pixel_array for s in slices]).astype(np.float32)
    # Apply rescale slope/intercept if present
    first = slices[0]
    slope = float(getattr(first, "RescaleSlope", 1.0))
    intercept = float(getattr(first, "RescaleIntercept", 0.0))
    vol = vol * slope + intercept

    # Reorder from (slice, row, col) to (x, y, z)-like layout
    vol = np.transpose(vol, (2, 1, 0))

    ps = getattr(first, "PixelSpacing", [1.0, 1.0])
    thickness = float(getattr(first, "SliceThickness", 1.0))
    zooms = (float(ps[1]), float(ps[0]), thickness)
    return vol, zooms


def load(path):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    name = path.name.lower()
    if name.endswith((".nii", ".nii.gz")):
        return load_nifti(path)
    return load_dicom(path)


# ---------------------------------------------------------------- series discovery

def _series_info(folder):
    """Read one DICOM file in `folder` for metadata. Returns dict or None."""
    import pydicom

    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.name == ".DS_Store":
            continue
        try:
            ds = pydicom.dcmread(str(f), stop_before_pixels=True)
        except Exception:
            continue
        is_image = getattr(ds, "Modality", "") in ("MR", "CT", "PT", "US", "XA")
        return {
            "path": folder,
            "desc": str(getattr(ds, "SeriesDescription", folder.name)),
            "modality": str(getattr(ds, "Modality", "?")),
            "count": sum(
                1 for x in folder.iterdir()
                if x.is_file() and x.name != ".DS_Store"
            ),
            "is_image": is_image,
        }
    return None


def find_series(root):
    """Find all DICOM/NIfTI series under `root`. Returns a list of info dicts."""
    root = Path(root)

    # A single file: treat as one "series".
    if root.is_file():
        return [{
            "path": root, "desc": root.name, "modality": "",
            "count": 1, "is_image": True,
        }]

    series = []

    # NIfTI files sitting directly in the folder each count as a series.
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.name.lower().endswith((".nii", ".nii.gz")):
            series.append({
                "path": f, "desc": f.name, "modality": "NIfTI",
                "count": 1, "is_image": True,
            })

    # DICOM series: any folder that directly contains DICOM files.
    for folder in sorted(p for p in root.rglob("*") if p.is_dir()):
        if any(x.is_file() and x.name != ".DS_Store" for x in folder.iterdir()):
            info = _series_info(folder)
            if info:
                series.append(info)
    if any(x.is_file() and x.name != ".DS_Store" for x in root.iterdir()):
        info = _series_info(root)
        if info and info["path"] not in [s["path"] for s in series]:
            series.insert(0, info)

    return series


# ---------------------------------------------------------------- native dialogs

def choose_folder():
    """Native macOS 'choose folder' dialog. Returns a path str or None."""
    return _osascript('choose folder with prompt "Select an MRI folder"')


def choose_file():
    """Native macOS 'choose file' dialog. Returns a path str or None."""
    return _osascript('choose file with prompt "Select an MRI file"')


def _osascript(command):
    try:
        out = subprocess.run(
            ["osascript", "-e", f"POSIX path of ({command})"],
            capture_output=True, text=True,
        )
        path = out.stdout.strip()
        return path or None
    except Exception:
        return None


# ---------------------------------------------------------------- patient info

def _fmt_date(s):
    s = str(s)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def patient_info(path):
    """Read patient/study metadata from the first DICOM under `path`."""
    import pydicom

    path = Path(path)
    cands = [path] if path.is_file() else [
        f for f in sorted(path.rglob("*"))
        if f.is_file() and f.name != ".DS_Store"
    ]
    for f in cands:
        try:
            ds = pydicom.dcmread(str(f), stop_before_pixels=True)
        except Exception:
            continue
        if not hasattr(ds, "PatientName") and not hasattr(ds, "StudyDate"):
            continue
        name = str(getattr(ds, "PatientName", "") or "-").replace("^", " ").strip()
        field = getattr(ds, "MagneticFieldStrength", "")
        parts = []
        if name and name != "-":
            parts.append(f"Patient: {name}")
        if getattr(ds, "PatientID", ""):
            parts.append(f"ID: {ds.PatientID}")
        sex = str(getattr(ds, "PatientSex", "") or "").strip()
        age = str(getattr(ds, "PatientAge", "") or "").strip()
        dob = _fmt_date(getattr(ds, "PatientBirthDate", "") or "")
        who = "  ".join(x for x in (sex, age or dob) if x)
        if who:
            parts.append(who)
        study = str(getattr(ds, "StudyDescription", "") or "").strip()
        date = _fmt_date(getattr(ds, "StudyDate", "") or "")
        body = str(getattr(ds, "BodyPartExamined", "") or "").strip()
        st = "  ".join(x for x in (study or body, date) if x)
        if st:
            parts.append(f"Study: {st}")
        scanner = "  ".join(str(x) for x in (
            getattr(ds, "Manufacturer", ""),
            f"{field}T" if field else "") if x).strip()
        if scanner:
            parts.append(scanner)
        return "   |   ".join(parts) if parts else None
    return None


# ---------------------------------------------------------------- viewer app

PLANES = {"axial": 2, "coronal": 1, "sagittal": 0}
GRID_RECT = (0.30, 0.12, 0.67, 0.80)  # x, y, w, h region holding the panels
MAX_PANELS = 4

# Display filters. "highlight fluid" tints bright signal (edema/effusion/tears
# on fluid-sensitive sequences); the rest are contrast/edge enhancements.
FILTERS = (
    "none",
    "invert",
    "CLAHE (local contrast)",
    "sharpen",
    "denoise",
    "edges",
    "highlight fluid",
)


class Panel:
    """One loaded volume shown in one grid cell."""

    def __init__(self, series):
        self.series = series
        self.vol, self.zooms = load(series["path"])
        finite = self.vol[np.isfinite(self.vol)]
        self.vmin_full = float(np.percentile(finite, 0.5))
        self.vmax_full = float(np.percentile(finite, 99.5))
        self.vmin, self.vmax = self.vmin_full, self.vmax_full
        self.ax = None
        self.im = None

    def n_slices(self, plane):
        return self.vol.shape[PLANES[plane]]

    def index_for(self, pos, plane):
        n = self.n_slices(plane)
        return int(round(pos * (n - 1))) if n > 1 else 0

    def slice_at(self, pos, plane):
        idx = self.index_for(pos, plane)
        return np.take(self.vol, idx, axis=PLANES[plane]).T, idx

    def aspect(self, plane):
        z = self.zooms
        if plane == "axial":
            return z[1] / z[0]
        if plane == "coronal":
            return z[2] / z[0]
        return z[2] / z[1]

    def reset_contrast(self):
        self.vmin, self.vmax = self.vmin_full, self.vmax_full


class MRIApp:
    def __init__(self, initial_path=None):
        self.series = []
        self.panels = []
        self.plane = "axial"
        self.filter_name = "none"
        self.pos = 0.5          # shared slice position, 0..1
        self.dark = False
        self.show_patient = True
        self._drag_start = None

        self.fig = plt.figure(figsize=(12, 8))
        self.fig.canvas.manager.set_window_title("Medical Image Viewer")

        # placeholder shown before anything is loaded
        self._hint = self.fig.text(
            0.63, 0.5, "Open a folder or file, check up to 4 series,\n"
            "then press \"View selected\".",
            ha="center", va="center", color="0.5", fontsize=13)

        # open buttons
        self.btn_folder = Button(
            self.fig.add_axes([0.02, 0.93, 0.13, 0.045]), "Open Folder...")
        self.btn_folder.on_clicked(lambda e: self._open_dialog(folder=True))
        self.btn_file = Button(
            self.fig.add_axes([0.16, 0.93, 0.10, 0.045]), "Open File...")
        self.btn_file.on_clicked(lambda e: self._open_dialog(folder=False))

        # series checklist (rebuilt when a folder is loaded)
        self.series_ax = self.fig.add_axes([0.02, 0.55, 0.24, 0.35])
        self.series_ax.set_title("Series (check up to 4)", fontsize=9, loc="left")
        self.series_ax.axis("off")
        self.series_check = None

        self.btn_view = Button(
            self.fig.add_axes([0.02, 0.49, 0.24, 0.05]), "View selected")
        self.btn_view.on_clicked(lambda e: self._view_selected())

        # enhancement / filter selector
        self.filter_ax = self.fig.add_axes([0.02, 0.25, 0.24, 0.20])
        self.filter_ax.set_title("Enhance / filter", fontsize=9, loc="left")
        self.filter_radio = RadioButtons(self.filter_ax, FILTERS, active=0)
        for lbl in self.filter_radio.labels:
            lbl.set_fontsize(8)
        self.filter_radio.on_clicked(self._on_filter)

        # plane selector
        self.plane_ax = self.fig.add_axes([0.02, 0.115, 0.24, 0.09])
        self.plane_ax.set_title("Plane", fontsize=9, loc="left")
        self.plane_radio = RadioButtons(
            self.plane_ax, ("axial", "coronal", "sagittal"), active=0)
        for lbl in self.plane_radio.labels:
            lbl.set_fontsize(8)
        self.plane_radio.on_clicked(self._on_plane)

        self.btn_reset = Button(
            self.fig.add_axes([0.02, 0.065, 0.075, 0.04]), "Reset")
        self.btn_reset.label.set_fontsize(8)
        self.btn_reset.on_clicked(lambda e: self.reset_contrast())

        self.btn_dark = Button(
            self.fig.add_axes([0.103, 0.065, 0.075, 0.04]), "Dark: off")
        self.btn_dark.label.set_fontsize(8)
        self.btn_dark.on_clicked(lambda e: self._toggle_dark())

        self.btn_patient = Button(
            self.fig.add_axes([0.186, 0.065, 0.075, 0.04]), "Info: on")
        self.btn_patient.label.set_fontsize(8)
        self.btn_patient.on_clicked(lambda e: self._toggle_patient())

        # patient / study info banner along the top of the image area
        self._patient_txt = self.fig.text(
            0.30, 0.965, "", fontsize=9, va="center", ha="left")

        self._disclaimer = self.fig.text(
            0.02, 0.02,
            "Image-enhancement tools only - not a diagnosis. "
            "Consult a radiologist.",
            fontsize=7, color="0.5")

        # slice-position slider
        self.slider_ax = self.fig.add_axes([0.32, 0.05, 0.63, 0.03])
        self.slider = Slider(self.slider_ax, "Slice", 0.0, 1.0,
                             valinit=self.pos, valstep=0.001)
        self.slider.on_changed(self._on_slider)

        c = self.fig.canvas
        c.mpl_connect("scroll_event", self._on_scroll)
        c.mpl_connect("key_press_event", self._on_key)
        c.mpl_connect("button_press_event", self._on_press)
        c.mpl_connect("button_release_event", self._on_release)
        c.mpl_connect("motion_notify_event", self._on_motion)

        if initial_path:
            self.open_path(Path(initial_path))

    # ---- opening ----------------------------------------------------

    def _open_dialog(self, folder):
        path = choose_folder() if folder else choose_file()
        if path:
            self.open_path(Path(path))

    def open_path(self, path):
        if not path.exists():
            self._notify(f"Path not found:\n{path}")
            return
        self.series = find_series(path)
        if not self.series:
            self._notify(f"No MRI series found under:\n{path}")
            return
        info = patient_info(path)
        self._patient_txt.set_text(info or "No patient metadata in this file.")
        self._build_series_list()
        # auto-view the first image series
        first = next((i for i, s in enumerate(self.series) if s["is_image"]), None)
        if first is not None:
            self._show([self.series[first]])

    def _build_series_list(self):
        self.series_ax.clear()
        self.series_ax.set_title("Series (check up to 4)", fontsize=9, loc="left")
        labels, actives = [], []
        first_image = next(
            (i for i, s in enumerate(self.series) if s["is_image"]), None)
        for i, s in enumerate(self.series):
            tag = "" if s["is_image"] else " (report)"
            labels.append(f"{i + 1}. {s['desc'][:20]} [{s['count']}]{tag}")
            actives.append(i == first_image)
        self.series_check = CheckButtons(self.series_ax, labels, actives)
        for lbl in self.series_check.labels:
            lbl.set_fontsize(8)

    def _checked_series(self):
        if self.series_check is None:
            return []
        status = self.series_check.get_status()
        return [s for s, on in zip(self.series, status) if on]

    def _view_selected(self):
        chosen = [s for s in self._checked_series() if s["is_image"]]
        if not chosen:
            self._notify("Check one to four image series first.")
            return
        if len(chosen) > MAX_PANELS:
            self._notify(f"Showing the first {MAX_PANELS} of {len(chosen)} "
                         "checked series.")
            chosen = chosen[:MAX_PANELS]
        self._show(chosen)

    def _show(self, chosen):
        panels = []
        for s in chosen:
            try:
                panels.append(Panel(s))
            except Exception as e:
                self._notify(f"Could not load {s['desc']}:\n{e}")
        if not panels:
            return
        # remove the axes of whatever was shown before
        for p in self.panels:
            if p.ax is not None:
                p.ax.remove()
        self.panels = panels
        if self._hint is not None:
            self._hint.remove()
            self._hint = None
        self.plane = self.plane_radio.value_selected
        self._build_grid()
        self._render(rebuild=True)
        print(f"Showing {len(panels)} series: "
              + ", ".join(p.series["desc"] for p in panels))

    # ---- grid layout ------------------------------------------------

    def _build_grid(self):
        for p in self.panels:
            if p.ax is not None:
                p.ax.remove()
        k = len(self.panels)
        cols = 1 if k == 1 else 2
        rows = 1 if k <= 2 else 2
        gx, gy, gw, gh = GRID_RECT
        pad = 0.02
        cw = (gw - pad * (cols - 1)) / cols
        ch = (gh - pad * (rows - 1)) / rows
        for i, p in enumerate(self.panels):
            r, c = divmod(i, cols)
            x = gx + c * (cw + pad)
            # rows fill top-to-bottom
            y = gy + gh - ch - r * (ch + pad)
            p.ax = self.fig.add_axes([x, y, cw, ch])
            p.ax.axis("off")
            p.im = None

    # ---- rendering --------------------------------------------------

    def _max_slices(self):
        return max((p.n_slices(self.plane) for p in self.panels), default=1)

    def _apply_filter(self, img, panel):
        """Return (display_array, cmap, vmin, vmax) for the current filter.

        display_array is 2D (use cmap) or an RGB array (cmap is None).
        """
        lo, hi = panel.vmin, panel.vmax
        f = self.filter_name
        if f == "none":
            return img, "gray", lo, hi
        if f == "invert":
            return img, "gray_r", lo, hi

        from skimage import exposure, filters

        span = max(hi - lo, 1e-6)
        g = np.clip((img - lo) / span, 0.0, 1.0)   # normalize to 0..1
        if f.startswith("CLAHE"):
            out = exposure.equalize_adapthist(g, clip_limit=0.02)
            return out, "gray", 0.0, 1.0
        if f == "sharpen":
            out = filters.unsharp_mask(g, radius=2, amount=1.5)
            return out, "gray", 0.0, 1.0
        if f == "denoise":
            out = filters.gaussian(g, sigma=1.0)
            return out, "gray", 0.0, 1.0
        if f == "edges":
            out = filters.sobel(g)
            return out, "inferno", 0.0, float(out.max() or 1.0)
        if f == "highlight fluid":
            rgb = np.dstack([g, g, g])
            nz = g[g > 0.05]
            thr = np.percentile(nz, 90) if nz.size else 1.0
            mask = g >= thr
            # tint bright (fluid-like) signal orange-red over the grayscale
            rgb[mask] = np.clip(
                rgb[mask] * 0.3 + np.array([1.0, 0.35, 0.0]), 0.0, 1.0)
            return rgb, None, None, None
        return img, "gray", lo, hi

    def _render(self, rebuild=False):
        if not self.panels:
            return
        col = self._colors()
        for p in self.panels:
            raw, idx = p.slice_at(self.pos, self.plane)
            n = p.n_slices(self.plane)
            disp, cmap, vmin, vmax = self._apply_filter(raw, p)
            p.ax.clear()
            p.ax.axis("off")
            p.ax.set_facecolor(col["bg"])
            if disp.ndim == 3:
                p.im = p.ax.imshow(
                    disp, origin="lower", aspect=p.aspect(self.plane))
            else:
                p.im = p.ax.imshow(
                    disp, cmap=cmap, origin="lower",
                    vmin=vmin, vmax=vmax, aspect=p.aspect(self.plane))
            p.ax.set_title(f"{p.series['desc']}  {idx + 1}/{n}",
                           fontsize=9, color=col["fg"])
        # sync slider without recursion
        self.slider.eventson = False
        self.slider.set_val(self.pos)
        self.slider.eventson = True
        self.fig.canvas.draw_idle()

    # ---- event handlers --------------------------------------------

    def _on_slider(self, val):
        self.pos = float(val)
        self._render()

    def _step(self, direction):
        if not self.panels:
            return
        n = self._max_slices()
        step = 1.0 / (n - 1) if n > 1 else 0.0
        self.pos = float(np.clip(self.pos + direction * step, 0.0, 1.0))
        self._render()

    def _on_scroll(self, event):
        self._step(1 if event.button == "up" else -1)

    def _on_plane(self, label):
        if not self.panels:
            return
        self.plane = label
        self._build_grid()
        self._render(rebuild=True)

    def _on_filter(self, label):
        self.filter_name = label
        if self.panels:
            self._render(rebuild=True)

    # ---- theme ------------------------------------------------------

    def _colors(self):
        if self.dark:
            return {"bg": "#1e1e1e", "fg": "#e6e6e6",
                    "btn": "#3a3a3a", "hint": "#9a9a9a"}
        return {"bg": "white", "fg": "black",
                "btn": "0.85", "hint": "0.5"}

    def _toggle_dark(self):
        self.dark = not self.dark
        self.btn_dark.label.set_text("Dark: on" if self.dark else "Dark: off")
        self._apply_theme()

    def _toggle_patient(self):
        self.show_patient = not self.show_patient
        self._patient_txt.set_visible(self.show_patient)
        self.btn_patient.label.set_text(
            "Info: on" if self.show_patient else "Info: off")
        self.fig.canvas.draw_idle()

    def _apply_theme(self):
        col = self._colors()
        self.fig.patch.set_facecolor(col["bg"])

        # control panels (checklist / filter / plane) and their titles
        for ax in (self.series_ax, self.filter_ax, self.plane_ax):
            ax.set_facecolor(col["bg"])
            ax.title.set_color(col["fg"])

        # widget option labels
        for widget in (self.series_check, self.filter_radio, self.plane_radio):
            if widget is not None:
                for lbl in widget.labels:
                    lbl.set_color(col["fg"])

        # push-buttons
        for btn in (self.btn_folder, self.btn_file, self.btn_view,
                    self.btn_reset, self.btn_dark, self.btn_patient):
            btn.ax.set_facecolor(col["btn"])
            btn.color = col["btn"]
            btn.label.set_color(col["fg"])

        # slider
        self.slider.label.set_color(col["fg"])
        self.slider.valtext.set_color(col["fg"])
        self.slider.ax.set_facecolor(col["btn"])

        # banners
        self._patient_txt.set_color(col["fg"])
        self._disclaimer.set_color(col["hint"])

        # image panels
        for p in self.panels:
            if p.ax is not None:
                p.ax.set_facecolor(col["bg"])
                p.ax.title.set_color(col["fg"])

        self.fig.canvas.draw_idle()

    def _on_key(self, event):
        key = (event.key or "").lower()
        if key == "up":
            self._step(1)
        elif key == "down":
            self._step(-1)
        elif key in ("a", "c", "s"):
            self.plane_radio.set_active({"a": 0, "c": 1, "s": 2}[key])
        elif key == "r":
            self.reset_contrast()

    def reset_contrast(self):
        for p in self.panels:
            p.reset_contrast()
        self._render()

    def _panel_at(self, event):
        for p in self.panels:
            if event.inaxes is p.ax:
                return p
        return None

    def _on_press(self, event):
        if event.button == 3:
            p = self._panel_at(event)
            if p is not None:
                self._drag_start = (event.x, event.y, p.vmin, p.vmax, p)

    def _on_release(self, event):
        if event.button == 3:
            self._drag_start = None

    def _on_motion(self, event):
        if self._drag_start is None:
            return
        x0, y0, vmin0, vmax0, p = self._drag_start
        span = max(vmax0 - vmin0, 1e-6)
        width = max(span * (1 + (event.x - x0) / 200.0), span * 0.01)
        level = (vmin0 + vmax0) / 2 + span * (event.y - y0) / 200.0
        p.vmin, p.vmax = level - width / 2, level + width / 2
        p.im.set_clim(p.vmin, p.vmax)
        self.fig.canvas.draw_idle()

    # ---- misc -------------------------------------------------------

    def _notify(self, message):
        print(message)
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display dialog {message!r} buttons {{"OK"}} '
                 f'with icon caution'],
                capture_output=True,
            )
        except Exception:
            pass

    def _raise_window(self):
        try:
            self.fig.canvas.draw()
            import os
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to set frontmost '
                 'of the first process whose unix id is %d to true'
                 % os.getpid()],
                capture_output=True,
            )
        except Exception:
            pass

    def run(self):
        self._raise_window()
        print("Medical Image Viewer window opened. Check series and press 'View selected' "
              "to compare. Close the window when done (don't press Ctrl+C).")
        plt.show()


def main():
    initial = sys.argv[1] if len(sys.argv) > 1 else None
    MRIApp(initial).run()


if __name__ == "__main__":
    main()
