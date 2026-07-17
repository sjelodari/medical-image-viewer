"""Data + rendering core for the MRI viewer (no GUI framework).

Loads NIfTI / DICOM volumes, discovers series, reads patient metadata, and
renders single slices (with window/level and enhancement filters) to PNG bytes.
Used by the web backend; safe to import without a display.
"""

import io
from pathlib import Path

import numpy as np

PLANES = {"axial": 2, "coronal": 1, "sagittal": 0}

FILTERS = ("none", "invert", "clahe", "sharpen", "denoise", "edges",
           "highlight")

FILTER_LABELS = {
    "none": "Original",
    "invert": "Invert",
    "clahe": "CLAHE (local contrast)",
    "sharpen": "Sharpen",
    "denoise": "Denoise",
    "edges": "Edges",
    "highlight": "Highlight fluid",
}


# ---------------------------------------------------------------- loading

def load_nifti(path):
    import nibabel as nib

    img = nib.as_closest_canonical(nib.load(str(path)))
    data = np.asanyarray(img.dataobj).astype(np.float32)
    if data.ndim == 4:
        data = data[..., 0]
    return data, tuple(float(z) for z in img.header.get_zooms()[:3])


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
            continue
    if not slices:
        raise ValueError(f"No readable DICOM images in {path}")

    def key(ds):
        if hasattr(ds, "ImagePositionPatient"):
            return float(ds.ImagePositionPatient[2])
        return float(getattr(ds, "InstanceNumber", 0))

    slices.sort(key=key)
    vol = np.stack([s.pixel_array for s in slices]).astype(np.float32)
    first = slices[0]
    vol = vol * float(getattr(first, "RescaleSlope", 1.0)) \
        + float(getattr(first, "RescaleIntercept", 0.0))
    vol = np.transpose(vol, (2, 1, 0))
    ps = getattr(first, "PixelSpacing", [1.0, 1.0])
    thick = float(getattr(first, "SliceThickness", 1.0))
    return vol, (float(ps[1]), float(ps[0]), thick)


def load(path):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Not found: {path}")
    if path.name.lower().endswith((".nii", ".nii.gz")):
        return load_nifti(path)
    return load_dicom(path)


# ---------------------------------------------------------------- discovery

def _series_info(folder):
    import pydicom

    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.name == ".DS_Store":
            continue
        try:
            ds = pydicom.dcmread(str(f), stop_before_pixels=True)
        except Exception:
            continue
        return {
            "path": str(folder),
            "desc": str(getattr(ds, "SeriesDescription", folder.name)),
            "modality": str(getattr(ds, "Modality", "?")),
            "count": sum(1 for x in folder.iterdir()
                         if x.is_file() and x.name != ".DS_Store"),
            "is_image": getattr(ds, "Modality", "") in
            ("MR", "CT", "PT", "US", "XA"),
        }
    return None


def find_series(root):
    root = Path(root)
    if root.is_file():
        return [{"path": str(root), "desc": root.name, "modality": "",
                 "count": 1, "is_image": True}]
    series = []
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.name.lower().endswith((".nii", ".nii.gz")):
            series.append({"path": str(f), "desc": f.name, "modality": "NIfTI",
                           "count": 1, "is_image": True})
    for folder in sorted(p for p in root.rglob("*") if p.is_dir()):
        if any(x.is_file() and x.name != ".DS_Store"
               for x in folder.iterdir()):
            info = _series_info(folder)
            if info:
                series.append(info)
    if any(x.is_file() and x.name != ".DS_Store" for x in root.iterdir()):
        info = _series_info(root)
        if info and info["path"] not in [s["path"] for s in series]:
            series.insert(0, info)
    return series


# ---------------------------------------------------------------- patient info

def _fmt_date(s):
    s = str(s)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def patient_info(path):
    import pydicom

    path = Path(path)
    cands = [path] if path.is_file() else [
        f for f in sorted(path.rglob("*"))
        if f.is_file() and f.name != ".DS_Store"]
    for f in cands:
        try:
            ds = pydicom.dcmread(str(f), stop_before_pixels=True)
        except Exception:
            continue
        if not hasattr(ds, "PatientName") and not hasattr(ds, "StudyDate"):
            continue
        field = getattr(ds, "MagneticFieldStrength", "")
        return {
            "name": str(getattr(ds, "PatientName", "") or "").replace(
                "^", " ").strip(),
            "id": str(getattr(ds, "PatientID", "") or ""),
            "sex": str(getattr(ds, "PatientSex", "") or ""),
            "age": str(getattr(ds, "PatientAge", "") or ""),
            "dob": _fmt_date(getattr(ds, "PatientBirthDate", "") or ""),
            "study": str(getattr(ds, "StudyDescription", "") or ""),
            "date": _fmt_date(getattr(ds, "StudyDate", "") or ""),
            "body": str(getattr(ds, "BodyPartExamined", "") or ""),
            "scanner": " ".join(str(x) for x in (
                getattr(ds, "Manufacturer", ""),
                f"{field}T" if field else "") if x).strip(),
        }
    return None


# ---------------------------------------------------------------- rendering

def default_window(vol):
    finite = vol[np.isfinite(vol)]
    lo = float(np.percentile(finite, 0.5))
    hi = float(np.percentile(finite, 99.5))
    return (lo + hi) / 2.0, max(hi - lo, 1.0)   # level, width


def n_slices(vol, plane):
    return int(vol.shape[PLANES[plane]])


def aspect(zooms, plane):
    z = zooms
    if plane == "axial":
        return z[1] / z[0]
    if plane == "coronal":
        return z[2] / z[0]
    return z[2] / z[1]


def _slice(vol, plane, index):
    return np.take(vol, index, axis=PLANES[plane]).T


def render_png(vol, zooms, plane, index, filt="none", level=None, width=None,
               downscale=1):
    """Render one slice to PNG bytes applying window/level + a filter.

    The output is resized so pixels are physically square (correct anatomical
    proportions), using the voxel spacing in `zooms`.
    """
    from PIL import Image

    raw = _slice(vol, plane, index).astype(np.float32)
    if level is None or width is None:
        level, width = default_window(vol)
    lo, hi = level - width / 2.0, level + width / 2.0
    g = np.clip((raw - lo) / max(hi - lo, 1e-6), 0.0, 1.0)

    rgb = None
    if filt == "none":
        gray = g
    elif filt == "invert":
        gray = 1.0 - g
    else:
        from skimage import exposure, filters
        if filt == "clahe":
            gray = exposure.equalize_adapthist(g, clip_limit=0.02)
        elif filt == "sharpen":
            gray = filters.unsharp_mask(g, radius=2, amount=1.5)
        elif filt == "denoise":
            gray = filters.gaussian(g, sigma=1.0)
        elif filt == "edges":
            e = filters.sobel(g)
            gray = e / (e.max() or 1.0)
        elif filt == "highlight":
            base = np.dstack([g, g, g])
            nz = g[g > 0.05]
            thr = np.percentile(nz, 90) if nz.size else 1.0
            mask = g >= thr
            base[mask] = np.clip(
                base[mask] * 0.3 + np.array([1.0, 0.35, 0.0]), 0.0, 1.0)
            rgb = base
            gray = None
        else:
            gray = g

    if rgb is not None:
        arr = (np.flipud(rgb) * 255).astype(np.uint8)
        im = Image.fromarray(arr, mode="RGB")
    else:
        arr = (np.flipud(gray) * 255).astype(np.uint8)
        im = Image.fromarray(arr, mode="L")

    # make pixels physically square using the voxel spacing
    a = aspect(zooms, plane)
    target_h = max(int(round(im.height * a)), 1)
    if target_h != im.height:
        im = im.resize((im.width, target_h), Image.BILINEAR)

    if downscale > 1:
        im = im.resize((max(im.width // downscale, 1),
                        max(im.height // downscale, 1)), Image.BILINEAR)

    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()
