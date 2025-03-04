# -*- coding: utf-8 -*-
import re

from natsort import natsorted
from PyQt5 import QtCore, QtWidgets

from ..ui import resource_rc as resource_rc
from ..ui.search_dialog import Ui_SearchDialog
from .range_editor import RangeEditor


class AdvancedSearch(Ui_SearchDialog, QtWidgets.QDialog):
    def __init__(
        self,
        channels_db,
        return_names=False,
        show_add_window=False,
        show_apply=False,
        show_pattern=True,
        apply_text="Apply",
        add_window_text="Add window",
        *args,
        **kwargs,
    ):

        super().__init__(*args, **kwargs)
        self.setupUi(self)

        self.result = set()
        self.add_window_request = False
        self.channels_db = channels_db

        self.apply_btn.clicked.connect(self._apply)
        self.add_btn.clicked.connect(self._add)
        self.add_window_btn.clicked.connect(self._add_window)
        self.cancel_btn.clicked.connect(self._cancel)

        self.search_box.editingFinished.connect(self.search_text_changed)
        self.match_kind.currentTextChanged.connect(self.search_box.textChanged.emit)
        self.matches.itemDoubleClicked.connect(self._match_double_clicked)
        self.selection.itemDoubleClicked.connect(self._selection_double_clicked)

        self.apply_pattern_btn.clicked.connect(self._apply_pattern)
        self.cancel_pattern_btn.clicked.connect(self._cancel_pattern)
        self.define_ranges_btn.clicked.connect(self._define_ranges)

        self.search_box.setFocus()

        self._return_names = return_names
        self.ranges = {}

        self.pattern_window = False

        self.apply_btn.setText(apply_text)
        self.add_window_btn.setText(add_window_text)

        if not show_add_window:
            self.add_window_btn.hide()

        if not show_apply:
            self.apply_btn.hide()

        if not show_pattern:
            self.tabs.removeTab(1)

        self.setWindowTitle("Search & select channels")

    def search_text_changed(self):
        text = self.search_box.text().strip()
        if len(text) >= 2:
            if self.match_kind.currentText() == "Wildcard":
                pattern = text.replace("*", "_WILDCARD_")
                pattern = re.escape(pattern)
                pattern = pattern.replace("_WILDCARD_", ".*")
            else:
                pattern = text

            try:
                pattern = re.compile(f"(?i){pattern}")
                matches = [name for name in self.channels_db if pattern.search(name)]
                self.matches.clear()
                self.matches.addItems(matches)
                if matches:
                    self.status.setText("")
                else:
                    self.status.setText("No match found")
            except Exception as err:
                self.status.setText(str(err))
                self.matches.clear()

    def _add(self, event):
        count = self.selection.count()
        names = set(self.selection.item(i).text() for i in range(count))

        to_add = set(item.text() for item in self.matches.selectedItems())

        names = natsorted(names | to_add)

        self.selection.clear()
        self.selection.addItems(names)

    def _apply(self, event):
        count = self.selection.count()

        if self._return_names:
            self.result = set(self.selection.item(i).text() for i in range(count))
        else:
            self.result = set()
            for i in range(count):
                for entry in self.channels_db[self.selection.item(i).text()]:
                    self.result.add(entry)
        self.close()

    def _apply_pattern(self, event):
        self.result = {
            "pattern": self.pattern.text().strip(),
            "match_type": self.pattern_match_type.currentText(),
            "filter_type": self.filter_type.currentText(),
            "filter_value": self.filter_value.value(),
            "raw": self.raw.checkState() == QtCore.Qt.Checked,
            "ranges": self.ranges,
        }

        self.pattern_window = True
        self.close()

    def _add_window(self, event):
        count = self.selection.count()

        if self._return_names:
            self.result = set(self.selection.item(i).text() for i in range(count))
        else:
            self.result = set()
            for i in range(count):
                for entry in self.channels_db[self.selection.item(i).text()]:
                    self.result.add(entry)

        self.add_window_request = True
        self.close()

    def _cancel(self, event):
        self.result = set()
        self.close()

    def _cancel_pattern(self, event):
        self.result = {}
        self.close()

    def _define_ranges(self, event=None):
        dlg = RangeEditor("", self.ranges)
        dlg.exec_()
        if dlg.pressed_button == "apply":
            self.ranges = dlg.result

    def _match_double_clicked(self, item):
        count = self.selection.count()
        names = set(self.selection.item(i).text() for i in range(count))

        new_name = item.text()

        if new_name not in names:
            names.add(new_name)

        self.selection.clear()
        self.selection.addItems(sorted(names))

    def _selection_double_clicked(self, item):
        count = self.selection.count()
        names = set(self.selection.item(i).text() for i in range(count))

        name = item.text()
        names.remove(name)

        self.selection.clear()
        self.selection.addItems(sorted(names))
