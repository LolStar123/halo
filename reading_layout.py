"""Full-size, left-to-right pages of one answer. No model calls or text rewriting."""
import math

from PySide6.QtCore import QRectF, QSizeF
from PySide6.QtGui import (QAbstractTextDocumentLayout, QColor, QFont, QPalette,
                          QTextCursor, QTextDocument, QTextDocumentFragment, QTextOption)


class ReadingLayout:
    GAP = 24
    HEADER = 22

    def __init__(self, view, entry):
        self.width = view.viewport().width()
        self.height = max(40, view.viewport().height() - self.HEADER)
        self.columns = max(1, min(3, (self.width + self.GAP) // (380 + self.GAP)))
        # Wide code/tables benefit from a full-width page, not narrow newspaper columns.
        if "```" in entry.text or "|" in entry.text:
            self.columns = 1
        self.column_width = (self.width - self.GAP * (self.columns - 1)) / self.columns
        self.document = QTextDocument()
        font = QFont(view.font())
        font.setPixelSize(view.body_px)
        self.document.setDefaultFont(font)
        self.document.setDocumentMargin(4)
        # Hanging list numbers need a real gutter, including two/three-digit steps.
        self.document.setIndentWidth(max(48, view.body_px * 2.5))
        option = self.document.defaultTextOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.document.setDefaultTextOption(option)
        cursor = entry.frame.firstCursorPosition()
        cursor.setPosition(entry.frame.lastPosition(), QTextCursor.KeepAnchor)
        QTextCursor(self.document).insertFragment(QTextDocumentFragment(cursor))
        self.document.setPageSize(QSizeF(self.column_width, self.height))
        self.document.documentLayout().documentSize()
        self.pages = max(1, self.document.pageCount())
        self.sheets = math.ceil(self.pages / self.columns)

    def paint(self, painter, sheet):
        font = QFont("Segoe UI Variable Small")
        font.setPixelSize(12)
        painter.setFont(font)
        for column in range(self.columns):
            page = sheet * self.columns + column
            if page >= self.pages:
                break
            left = column * (self.column_width + self.GAP)
            painter.setPen(QColor("#8798A8"))
            painter.drawText(QRectF(left, 0, self.column_width, self.HEADER),
                             f"{page + 1} / {self.pages}  ·  read down" +
                             (" →" if column + 1 < self.columns and page + 1 < self.pages else ""))
            if column:
                painter.setPen(QColor("#33404D"))
                painter.drawLine(int(left - self.GAP / 2), self.HEADER,
                                 int(left - self.GAP / 2), int(self.height + self.HEADER))
            painter.save()
            painter.setClipRect(QRectF(left, self.HEADER, self.column_width, self.height))
            painter.translate(left, self.HEADER - page * self.height)
            context = QAbstractTextDocumentLayout.PaintContext()
            context.palette.setColor(QPalette.Text, QColor("#EDF2F7"))
            context.clip = QRectF(0, page * self.height, self.column_width, self.height)
            self.document.documentLayout().draw(painter, context)
            painter.restore()
