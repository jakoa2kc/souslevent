"""Tiny Qt helper shared by both desktop apps: RGBA numpy buffer → QLabel pixmap.

Kept in one place so the legend/colourbar panels of the manual app (:mod:`sillage.app.main_window`)
and the automatic app (:mod:`sillage.auto.window`) render identically and cannot drift.
"""

from __future__ import annotations


def set_label_image(label, rgba) -> None:
    """Set ``label``'s pixmap from an ``(H, W, 4)`` uint8 RGBA array.

    ``.copy()`` is essential: ``QImage(buf, …)`` does NOT take ownership of the buffer, so once the
    temporary ``bytes(rgba.data)`` is freed the QImage would point at released memory (an
    intermittent native use-after-free that ``try/except`` cannot catch). ``.copy()`` forces the
    QImage to own its pixels before the source goes away.
    """
    from PySide6.QtGui import QImage, QPixmap

    h, w = rgba.shape[:2]
    img = QImage(bytes(rgba.data), w, h, 4 * w, QImage.Format_RGBA8888).copy()
    label.setPixmap(QPixmap.fromImage(img))
