"""Image post-processing (numpy only; Blender ships numpy): line art extraction and seamless textures.

Array convention: float32, shape (height, width, channels), row 0 is the top of the image, values 0..1.
Blender's Image.pixels is flat RGBA starting at the bottom; the conversion happens in the ui layer.
"""

from __future__ import annotations

import numpy as np


def composite(rgba: np.ndarray, background: tuple[float, float, float]) -> np.ndarray:
    """Composite a render with transparency onto a solid background and return RGB"""
    rgb = rgba[..., :3]
    a = rgba[..., 3:4] if rgba.shape[-1] == 4 else np.ones(rgb.shape[:2] + (1,), rgba.dtype)
    bg = np.asarray(background, dtype=rgba.dtype).reshape(1, 1, 3)
    # Render output is premultiplied (0 where transparent), so just add background x (1 - a)
    return np.clip(rgb + bg * (1.0 - a), 0.0, 1.0)


def _sobel(gray: np.ndarray) -> np.ndarray:
    p = np.pad(gray, 1, mode="edge")
    gx = (p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[1:-1, :-2] + p[2:, :-2])
    gy = (p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:])
    return np.hypot(gx, gy)


def lineart(depth: np.ndarray, normal: np.ndarray | None = None, *, depth_threshold: float = 0.04, normal_threshold: float = 0.6, thicken: int = 1) -> np.ndarray:
    """Extract outlines and creases from a depth map (grey, near white / far black) and a normal map: white lines on black (canny convention), returns RGB"""
    d = depth[..., 0] if depth.ndim == 3 else depth
    edges = _sobel(d) > depth_threshold
    if normal is not None:
        n = normal[..., :3] * 2.0 - 1.0
        mag = sum(_sobel(n[..., c]) ** 2 for c in range(3)) ** 0.5
        edges |= mag > normal_threshold
    for _ in range(max(0, thicken)):
        e = np.pad(edges, 1)
        edges = e[1:-1, 1:-1] | e[:-2, 1:-1] | e[2:, 1:-1] | e[1:-1, :-2] | e[1:-1, 2:]
    out = edges.astype(np.float32)
    return np.repeat(out[..., None], 3, axis=2)


def _smooth(n: int) -> np.ndarray:
    t = (np.arange(n, dtype=np.float32) + 0.5) / n
    return t * t * (3.0 - 2.0 * t)


def make_tileable(img: np.ndarray, band: float = 0.125) -> np.ndarray:
    """Seamless texture by edge cross-fading: a band on the right fades into the start on the left, then the same top to bottom.

    The output is one band shorter than the input in each direction (1/8 by default), but its ends meet exactly:
    the last column of out is source column W-b-1, and the first column of out is almost entirely source column W-b; the two are neighbours in the source.
    The middle is kept as is, so there is no ghosting across the whole image as with "shift by half and blend".
    """

    def one_axis(a: np.ndarray, axis: int) -> np.ndarray:
        n = a.shape[axis]
        b = max(2, int(round(n * band)))
        if n < 4 * b:
            return a
        t = _smooth(b)
        shape = [1] * a.ndim
        shape[axis] = b
        t = t.reshape(shape)
        head = np.take(a, np.arange(0, b), axis=axis)
        tail = np.take(a, np.arange(n - b, n), axis=axis)
        blended = tail * (1.0 - t) + head * t
        middle = np.take(a, np.arange(b, n - b), axis=axis)
        return np.concatenate([blended, middle], axis=axis)

    out = one_axis(img.astype(np.float32), 1)
    out = one_axis(out, 0)
    return out


def seam_error(img: np.ndarray) -> float:
    """Average jump across the seam when tiled (smaller is more seamless); used by unit tests and the self-test"""
    a = img.astype(np.float32)
    lr = np.abs(a[:, 0] - a[:, -1]).mean()
    tb = np.abs(a[0, :] - a[-1, :]).mean()
    return float(max(lr, tb))
