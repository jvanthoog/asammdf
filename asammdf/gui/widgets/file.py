# -*- coding: utf-8 -*-
from datetime import datetime
from functools import partial
import json
import os
from pathlib import Path
from tempfile import gettempdir
from traceback import format_exc

from natsort import natsorted
import pandas as pd
import psutil
from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from ...blocks.utils import extract_cncomment_xml
from ...blocks.v4_constants import (
    BUS_TYPE_CAN,
    BUS_TYPE_ETHERNET,
    BUS_TYPE_FLEXRAY,
    BUS_TYPE_LIN,
    BUS_TYPE_USB,
    FLAG_AT_TO_STRING,
    FLAG_CG_BUS_EVENT,
)
from ...mdf import MDF, SUPPORTED_VERSIONS
from ..dialogs.advanced_search import AdvancedSearch
from ..dialogs.channel_group_info import ChannelGroupInfoDialog
from ..dialogs.channel_info import ChannelInfoDialog
from ..dialogs.window_selection_dialog import WindowSelectionDialog
from ..ui import resource_rc as resource_rc
from ..ui.file_widget import Ui_file_widget
from ..utils import (
    add_children,
    HelperChannel,
    load_dsp,
    load_lab,
    run_thread_with_progress,
    setup_progress,
    TERMINATED,
)
from .attachment import Attachment
from .can_bus_trace import CANBusTrace
from .database_item import DatabaseItem
from .gps import GPS
from .mdi_area import MdiAreaWidget, WithMDIArea
from .numeric import Numeric
from .plot import Plot
from .tabular import Tabular
from .tree_item import TreeItem


class FileWidget(WithMDIArea, Ui_file_widget, QtWidgets.QWidget):

    open_new_file = QtCore.pyqtSignal(str)
    full_screen_toggled = QtCore.pyqtSignal()

    def __init__(
        self,
        file_name,
        with_dots,
        subplots=False,
        subplots_link=False,
        ignore_value2text_conversions=False,
        line_interconnect="line",
        encryption_function=None,
        decryption_function=None,
        *args,
        **kwargs,
    ):

        super(Ui_file_widget, self).__init__(*args, **kwargs)
        WithMDIArea.__init__(self)
        self.setupUi(self)
        self._settings = QtCore.QSettings()
        self.uuid = os.urandom(6).hex()

        file_name = Path(file_name)
        self.subplots = subplots
        self.subplots_link = subplots_link
        self.ignore_value2text_conversions = ignore_value2text_conversions
        self._viewbox = pg.ViewBox()
        self._viewbox.setXRange(0, 10)

        self.file_name = file_name
        self.progress = None
        self.mdf = None
        self.info_index = None
        self.with_dots = with_dots

        self._show_filter_tree = False
        self.line_interconnect = line_interconnect

        progress = QtWidgets.QProgressDialog(
            f'Opening "{self.file_name}"', "", 0, 100, self.parent()
        )

        progress.setWindowModality(QtCore.Qt.ApplicationModal)
        progress.setCancelButton(None)
        progress.setAutoClose(True)
        progress.setWindowTitle("Opening measurement")
        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(":/open.png"), QtGui.QIcon.Normal, QtGui.QIcon.Off)
        progress.setWindowIcon(icon)
        progress.show()

        if file_name.suffix.lower() in (".erg", ".bsig"):

            extension = file_name.suffix.lower().strip(".")
            progress.setLabelText(f"Converting from {extension} to mdf")

            try:
                from mfile import BSIG, ERG
            except ImportError:
                from cmerg import BSIG, ERG

            if file_name.suffix.lower() == ".erg":
                cls = ERG
            else:
                cls = BSIG

            out_file = Path(gettempdir()) / file_name.name

            mdf_path = (
                cls(file_name).export_mdf().save(out_file.with_suffix(".tmp.mf4"))
            )
            self.mdf = MDF(mdf_path)

        elif file_name.suffix.lower() == ".csv":
            try:
                with open(file_name) as csv:
                    names = [n.strip() for n in csv.readline().split(",")]
                    units = [n.strip() for n in csv.readline().split(",")]

                    try:
                        float(units[0])
                    except:
                        units = {name: unit for name, unit in zip(names, units)}
                    else:
                        csv.seek(0)
                        csv.readline()
                        units = None

                    df = pd.read_csv(csv, header=None, names=names)
                    df.set_index(df[names[0]], inplace=True)
                    self.mdf = MDF()
                    self.mdf.append(df, units=units)
            except:
                progress.cancel()
                print(format_exc())
                raise Exception(
                    "Could not load CSV. The first line must contain the channel names. The seconds line "
                    "can optionally contain the channel units. The first column must be the time"
                )

        else:

            if file_name.suffix.lower() == ".dl3":
                progress.setLabelText("Converting from dl3 to mdf")
                datalyser_active = any(
                    proc.name() == "Datalyser3.exe" for proc in psutil.process_iter()
                )

                out_file = Path(gettempdir()) / file_name.name

                import win32com.client

                index = 0
                while True:
                    mdf_name = out_file.with_suffix(f".{index}.mdf")
                    if mdf_name.exists():
                        index += 1
                    else:
                        break

                try:
                    datalyser = win32com.client.Dispatch("Datalyser3.Datalyser3_COM")
                except:
                    raise Exception(
                        "Datalyser must be installed if you wasnt to open DL3 files"
                    )
                if not datalyser_active:
                    try:
                        datalyser.DCOM_set_datalyser_visibility(False)
                    except:
                        pass
                datalyser.DCOM_convert_file_mdf_dl3(file_name, str(mdf_name), 0)
                if not datalyser_active:
                    datalyser.DCOM_TerminateDAS()
                file_name = mdf_name

            target = MDF
            kwargs = {
                "name": file_name,
                "callback": self.update_progress,
                "encryption_function": encryption_function,
                "decryption_function": decryption_function,
                "use_display_names": True,
            }

            self.mdf = run_thread_with_progress(
                self,
                target=target,
                kwargs=kwargs,
                factor=33,
                offset=0,
                progress=progress,
            )

            if self.mdf is TERMINATED:
                return

        channels_db_items = sorted(self.mdf.channels_db, key=lambda x: x.lower())
        self.channels_db_items = channels_db_items

        progress.setLabelText("Loading graphical elements")

        progress.setValue(37)

        self.channel_view.currentIndexChanged.connect(
            partial(self._update_channel_tree, widget=self.channels_tree)
        )
        self.filter_view.currentIndexChanged.connect(
            partial(self._update_channel_tree, widget=self.filter_tree)
        )
        self.channel_view.currentTextChanged.connect(
            partial(self._update_channel_tree, widget=self.channels_tree)
        )
        self.filter_view.currentTextChanged.connect(
            partial(self._update_channel_tree, widget=self.filter_tree)
        )

        self.channels_tree.setDragEnabled(True)

        self.mdi_area = MdiAreaWidget()
        self.mdi_area.add_window_request.connect(self.add_window)
        self.mdi_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.mdi_area.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.splitter.addWidget(self.mdi_area)

        self.channels_tree.itemDoubleClicked.connect(self.show_info)
        self.filter_tree.itemDoubleClicked.connect(self.show_info)

        self.channel_view.setCurrentIndex(-1)
        self.filter_view.setCurrentIndex(-1)
        self.channel_view.setCurrentText(
            self._settings.value("channels_view", "Internal file structure")
        )
        self.filter_view.setCurrentText(
            self._settings.value("filter_view", "Internal file structure")
        )

        progress.setValue(70)

        self.raster_type_channel.toggled.connect(self.set_raster_type)

        progress.setValue(90)

        self.output_options.setCurrentIndex(0)

        self.mdf_version.insertItems(0, SUPPORTED_VERSIONS)
        self.mdf_version.setCurrentText("4.10")
        self.mdf_compression.insertItems(
            0, ("no compression", "deflate", "transposed deflate")
        )
        self.mdf_split_size.setValue(4)

        self.extract_bus_format.insertItems(0, SUPPORTED_VERSIONS)
        self.extract_bus_format.setCurrentText("4.10")
        index = self.extract_bus_format.findText(self.mdf.version)
        if index >= 0:
            self.extract_bus_format.setCurrentIndex(index)
        self.extract_bus_compression.insertItems(
            0, ("no compression", "deflate", "transposed deflate")
        )
        self.extract_bus_btn.clicked.connect(self.extract_bus_logging)
        self.extract_bus_csv_btn.clicked.connect(self.extract_bus_csv_logging)
        self.load_can_database_btn.clicked.connect(self.load_can_database)
        self.load_lin_database_btn.clicked.connect(self.load_lin_database)

        progress.setValue(99)

        self.empty_channels.insertItems(0, ("skip", "zeros"))
        self.empty_channels_bus.insertItems(0, ("skip", "zeros"))
        self.empty_channels_mat.insertItems(0, ("skip", "zeros"))
        self.empty_channels_csv.insertItems(0, ("skip", "zeros"))
        self.mat_format.insertItems(0, ("4", "5", "7.3"))
        self.oned_as.insertItems(0, ("row", "column"))

        self.output_format.currentTextChanged.connect(self.output_format_changed)

        # info tab
        try:
            file_stats = os.stat(self.mdf.name)
        except:
            file_stats = None
        file_info = QtWidgets.QTreeWidgetItem()
        file_info.setText(0, "File information")

        self.info.addTopLevelItem(file_info)

        children = []

        item = QtWidgets.QTreeWidgetItem()
        item.setText(0, "Path")
        item.setText(1, str(self.mdf.name))
        children.append(item)

        item = QtWidgets.QTreeWidgetItem()
        item.setText(0, "Size")
        if file_stats is not None:
            item.setText(1, f"{file_stats.st_size / 1024 / 1024:.1f} MB")
        else:
            try:
                item.setText(1, f"{self.mdf.file_limit / 1024 / 1024:.1f} MB")
            except:
                item.setText(1, f"Unknown size")
        children.append(item)

        if file_stats is not None:
            date_ = datetime.fromtimestamp(file_stats.st_ctime)
        else:
            date_ = datetime.now()
        item = QtWidgets.QTreeWidgetItem()
        item.setText(0, "Created")
        item.setText(1, date_.strftime("%d-%b-%Y %H-%M-%S"))
        children.append(item)

        if file_stats is not None:
            date_ = datetime.fromtimestamp(file_stats.st_mtime)
        else:
            date_ = datetime.now()
        item = QtWidgets.QTreeWidgetItem()
        item.setText(0, "Last modified")
        item.setText(1, date_.strftime("%d-%b-%Y %H:%M:%S"))
        children.append(item)

        file_info.addChildren(children)

        mdf_info = QtWidgets.QTreeWidgetItem()
        mdf_info.setText(0, "MDF information")

        self.info.addTopLevelItem(mdf_info)

        children = []

        item = QtWidgets.QTreeWidgetItem()
        item.setText(0, "Version")
        item.setText(1, self.mdf.version)
        children.append(item)

        item = QtWidgets.QTreeWidgetItem()
        item.setText(0, "Program identification")
        item.setText(
            1,
            self.mdf.identification.program_identification.decode("ascii").strip(
                " \r\n\t\0"
            ),
        )
        children.append(item)

        item = QtWidgets.QTreeWidgetItem()
        item.setText(0, "Measurement start time")
        item.setText(
            1, self.mdf.header.start_time.strftime("%d-%b-%Y %H:%M:%S + %fus UTC")
        )
        children.append(item)

        item = QtWidgets.QTreeWidgetItem()
        item.setText(0, "Measurement comment")
        item.setText(1, self.mdf.header.comment)
        item.setTextAlignment(0, QtCore.Qt.AlignTop)
        children.append(item)

        channel_groups = QtWidgets.QTreeWidgetItem()
        channel_groups.setText(0, "Channel groups")
        channel_groups.setText(1, str(len(self.mdf.groups)))
        children.append(channel_groups)

        channel_groups_children = []
        for i, group in enumerate(self.mdf.groups):
            channel_group = group.channel_group
            if hasattr(channel_group, "comment"):
                comment = channel_group.comment
            else:
                comment = ""
            if comment:
                name = f"Channel group {i} ({comment})"
            else:
                name = f"Channel group {i}"

            cycles = channel_group.cycles_nr

            channel_group_item = QtWidgets.QTreeWidgetItem()
            channel_group_item.setText(0, name)

            if self.mdf.version < "4.00":
                size = channel_group.samples_byte_nr * cycles
            else:
                if channel_group.flags & 0x1:
                    size = channel_group.samples_byte_nr + (
                        channel_group.invalidation_bytes_nr << 32
                    )
                else:
                    size = (
                        channel_group.samples_byte_nr
                        + channel_group.invalidation_bytes_nr
                    ) * cycles

                if group.channel_group.acq_source:
                    source = group.channel_group.acq_source
                    if source.bus_type == BUS_TYPE_CAN:
                        ico = ":/bus_can.png"
                    elif source.bus_type == BUS_TYPE_LIN:
                        ico = ":/bus_lin.png"
                    elif source.bus_type == BUS_TYPE_ETHERNET:
                        ico = ":/bus_eth.png"
                    elif source.bus_type == BUS_TYPE_USB:
                        ico = ":/bus_usb.png"
                    elif source.bus_type == BUS_TYPE_FLEXRAY:
                        ico = ":/bus_flx.png"
                    else:
                        ico = None

                    if ico is not None:
                        icon = QtGui.QIcon()
                        icon.addPixmap(
                            QtGui.QPixmap(ico), QtGui.QIcon.Normal, QtGui.QIcon.Off
                        )
                        channel_group_item.setIcon(0, icon)

            item = QtWidgets.QTreeWidgetItem()
            item.setText(0, "Channels")
            item.setText(1, f"{len(group.channels)}")
            channel_group_item.addChild(item)

            item = QtWidgets.QTreeWidgetItem()
            item.setText(0, "Cycles")
            item.setText(1, str(cycles))
            if cycles:
                item.setForeground(1, QtGui.QBrush(QtCore.Qt.darkGreen))
            channel_group_item.addChild(item)

            item = QtWidgets.QTreeWidgetItem()
            item.setText(0, "Raw size")
            item.setText(1, f"{size / 1024 / 1024:.1f} MB")
            if cycles:
                item.setForeground(1, QtGui.QBrush(QtCore.Qt.darkGreen))
            channel_group_item.addChild(item)

            channel_groups_children.append(channel_group_item)

        channel_groups.addChildren(channel_groups_children)

        channels = QtWidgets.QTreeWidgetItem()
        channels.setText(0, "Channels")
        channels.setText(
            1, str(sum(len(entry) for entry in self.mdf.channels_db.values()))
        )
        children.append(channels)

        mdf_info.addChildren(children)

        self.info.expandAll()

        self.info.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeToContents
        )

        # self.channels_tree.itemChanged.connect(self.select)
        self.create_window_btn.clicked.connect(self._create_window)

        self.clear_filter_btn.clicked.connect(self.clear_filter)
        self.clear_channels_btn.clicked.connect(self.clear_channels)
        self.select_all_btn.clicked.connect(self.select_all_channels)

        self.aspects.setCurrentIndex(0)

        self.aspects.currentChanged.connect(self.aspect_changed)

        progress.setValue(100)
        progress.deleteLater()

        self.load_channel_list_btn.clicked.connect(self.load_channel_list)
        self.save_channel_list_btn.clicked.connect(self.save_channel_list)
        self.load_filter_list_btn.clicked.connect(self.load_filter_list)
        self.save_filter_list_btn.clicked.connect(self.save_filter_list)
        self.advanced_search_btn.clicked.connect(self.search)
        self.advanced_serch_filter_btn.clicked.connect(self.search)
        self.raster_search_btn.clicked.connect(self.raster_search)

        self.filter_tree.itemChanged.connect(self.filter_changed)
        self._selected_filter = set()
        self._filter_timer = QtCore.QTimer()
        self._filter_timer.setSingleShot(True)
        self._filter_timer.timeout.connect(self.update_selected_filter_channels)

        self.scramble_btn.clicked.connect(self.scramble)
        self.setAcceptDrops(True)

        self.apply_btn.clicked.connect(self.apply_processing)

        if self.mdf.version >= "4.00" and self.mdf.attachments:
            for i, attachment in enumerate(self.mdf.attachments, 1):
                att = Attachment(attachment, self.mdf._decryption_function)
                att.number.setText(f"{i}.")

                fields = []

                field = QtWidgets.QTreeWidgetItem()
                field.setText(0, "ATBLOCK address")
                field.setText(1, f"0x{attachment.address:X}")
                fields.append(field)

                field = QtWidgets.QTreeWidgetItem()
                field.setText(0, "File name")
                field.setText(1, str(attachment.file_name))
                fields.append(field)

                field = QtWidgets.QTreeWidgetItem()
                field.setText(0, "MIME type")
                field.setText(1, attachment.mime)
                fields.append(field)

                field = QtWidgets.QTreeWidgetItem()
                field.setText(0, "Comment")
                field.setText(1, attachment.comment)
                fields.append(field)

                field = QtWidgets.QTreeWidgetItem()
                field.setText(0, "Flags")
                if attachment.flags:
                    flags = []
                    for flag, string in FLAG_AT_TO_STRING.items():
                        if attachment.flags & flag:
                            flags.append(string)
                    text = f'{attachment.flags} [0x{attachment.flags:X}= {", ".join(flags)}]'
                else:
                    text = "0"
                field.setText(1, text)
                fields.append(field)

                field = QtWidgets.QTreeWidgetItem()
                field.setText(0, "MD5 sum")
                field.setText(1, attachment.md5_sum.hex().upper())
                fields.append(field)

                size = attachment.original_size
                if size <= 1 << 10:
                    text = f"{size} B"
                elif size <= 1 << 20:
                    text = f"{size/1024:.1f} KB"
                elif size <= 1 << 30:
                    text = f"{size/1024/1024:.1f} MB"
                else:
                    text = f"{size/1024/1024/1024:.1f} GB"

                field = QtWidgets.QTreeWidgetItem()
                field.setText(0, "Size")
                field.setText(1, text)
                fields.append(field)

                att.fields.addTopLevelItems(fields)

                item = QtWidgets.QListWidgetItem()
                item.setSizeHint(att.sizeHint())
                self.attachments.addItem(item)
                self.attachments.setItemWidget(item, att)
        else:
            self.aspects.removeTab(4)

        if self.mdf.version >= "4.00":
            if not any(
                group.channel_group.flags & FLAG_CG_BUS_EVENT
                for group in self.mdf.groups
            ):
                self.aspects.removeTab(2)
        else:
            self.aspects.removeTab(2)

        self._splitter_sizes = None

    def set_raster_type(self, event):
        if self.raster_type_channel.isChecked():
            self.raster_channel.setEnabled(True)
            self.raster.setEnabled(False)
            self.raster.setValue(0)
        else:
            self.raster_channel.setEnabled(False)
            self.raster_channel.setCurrentIndex(0)
            self.raster.setEnabled(True)

    def _update_channel_tree(self, index=None, widget=None):
        if widget is None:
            widget = self.channels_tree
        if widget is self.channels_tree and self.channel_view.currentIndex() == -1:
            return
        elif widget is self.filter_tree and (
            self.filter_view.currentIndex() == -1 or not self._show_filter_tree
        ):
            return

        view = self.channel_view if widget is self.channels_tree else self.filter_view

        iterator = QtWidgets.QTreeWidgetItemIterator(widget)
        signals = set()

        if widget.mode == "Internal file structure":
            while iterator.value():
                item = iterator.value()

                if item.entry[1] != 0xFFFFFFFFFFFFFFFF:
                    if item.checkState(0) == QtCore.Qt.Checked:
                        signals.add(item.entry)

                iterator += 1
        else:
            while iterator.value():
                item = iterator.value()

                if item.checkState(0) == QtCore.Qt.Checked:
                    signals.add(item.entry)

                iterator += 1

        widget.clear()
        widget.mode = view.currentText()

        if widget.mode == "Natural sort":
            items = []
            for i, group in enumerate(self.mdf.groups):
                for j, ch in enumerate(group.channels):
                    entry = i, j

                    channel = TreeItem(entry, ch.name, mdf_uuid=self.uuid)
                    channel.setText(0, ch.name)
                    if entry in signals:
                        channel.setCheckState(0, QtCore.Qt.Checked)
                    else:
                        channel.setCheckState(0, QtCore.Qt.Unchecked)
                    items.append(channel)

            if len(items) < 30000:
                items = natsorted(items, key=lambda x: x.name)
            else:
                items.sort(key=lambda x: x.name)
            widget.addTopLevelItems(items)

        elif widget.mode == "Internal file structure":
            for i, group in enumerate(self.mdf.groups):
                entry = i, 0xFFFFFFFFFFFFFFFF
                channel_group = TreeItem(entry, mdf_uuid=self.uuid)
                comment = group.channel_group.comment
                comment = extract_cncomment_xml(comment)

                if self.mdf.version >= "4.00" and group.channel_group.acq_source:
                    source = group.channel_group.acq_source
                    if source.bus_type == BUS_TYPE_CAN:
                        ico = ":/bus_can.png"
                    elif source.bus_type == BUS_TYPE_LIN:
                        ico = ":/bus_lin.png"
                    elif source.bus_type == BUS_TYPE_ETHERNET:
                        ico = ":/bus_eth.png"
                    elif source.bus_type == BUS_TYPE_USB:
                        ico = ":/bus_usb.png"
                    elif source.bus_type == BUS_TYPE_FLEXRAY:
                        ico = ":/bus_flx.png"
                    else:
                        ico = None

                    if ico is not None:
                        icon = QtGui.QIcon()
                        icon.addPixmap(
                            QtGui.QPixmap(ico), QtGui.QIcon.Normal, QtGui.QIcon.Off
                        )
                        channel_group.setIcon(0, icon)

                if comment:
                    channel_group.setText(0, f"Channel group {i} ({comment})")
                else:
                    channel_group.setText(0, f"Channel group {i}")
                channel_group.setFlags(
                    channel_group.flags()
                    | QtCore.Qt.ItemIsTristate
                    | QtCore.Qt.ItemIsUserCheckable
                )

                widget.addTopLevelItem(channel_group)

                channels = [
                    HelperChannel(name=ch.name, entry=(i, j))
                    for j, ch in enumerate(group.channels)
                ]

                add_children(
                    channel_group,
                    channels,
                    group.channel_dependencies,
                    signals,
                    entries=None,
                    mdf_uuid=self.uuid,
                    version=self.mdf.version,
                )
        else:
            items = []
            for entry in signals:
                gp_index, ch_index = entry
                ch = self.mdf.groups[gp_index].channels[ch_index]
                channel = TreeItem(entry, ch.name, mdf_uuid=self.uuid)
                channel.setText(0, ch.name)
                channel.setCheckState(0, QtCore.Qt.Checked)
                items.append(channel)

            if len(items) < 30000:
                items = natsorted(items, key=lambda x: x.name)
            else:
                items.sort(key=lambda x: x.name)
            widget.addTopLevelItems(items)

        setting = "channels_view" if widget is self.channels_tree else "filter_view"
        self._settings.setValue(setting, view.currentText())

    def output_format_changed(self, name):
        if name == "MDF":
            self.output_options.setCurrentIndex(0)
        elif name == "MAT":
            self.output_options.setCurrentIndex(2)

            self.export_compression_mat.clear()
            self.export_compression_mat.addItems(["enabled", "disabled"])
            self.export_compression_mat.setCurrentIndex(0)
        elif name == "CSV":
            self.output_options.setCurrentIndex(3)

        else:
            self.output_options.setCurrentIndex(1)
            if name == "Parquet":
                self.export_compression.setEnabled(True)
                self.export_compression.clear()
                self.export_compression.addItems(["GZIP", "SNAPPY"])
                self.export_compression.setCurrentIndex(0)
            elif name == "HDF5":
                self.export_compression.setEnabled(True)
                self.export_compression.clear()
                self.export_compression.addItems(["gzip", "lzf", "szip"])
                self.export_compression.setCurrentIndex(0)
            elif name:
                self.export_compression.clear()
                self.export_compression.setEnabled(False)

    def search(self, event=None):
        toggle_frames = False
        if self.aspects.tabText(self.aspects.currentIndex()) == "Channels":
            show_add_window = True
            show_apply = True
            apply_text = "Check channels"
            widget = self.channels_tree
            view = self.channel_view

            if self._frameless_windows:
                toggle_frames = True
                self.toggle_frames()
        else:
            show_add_window = False
            show_apply = True
            apply_text = "Check channels"
            widget = self.filter_tree
            view = self.filter_view
        dlg = AdvancedSearch(
            self.mdf.channels_db,
            show_add_window=show_add_window,
            show_apply=show_apply,
            apply_text=apply_text,
            parent=self,
        )
        dlg.setModal(True)
        dlg.exec_()
        result, pattern_window = dlg.result, dlg.pattern_window

        if result:
            if pattern_window:
                options = [
                    "New pattern based plot window",
                    "New pattern based numeric window",
                    "New pattern based tabular window",
                ]

                dialog = WindowSelectionDialog(options=options, parent=self)
                dialog.setModal(True)
                dialog.exec_()

                if dialog.result():
                    window_type = dialog.selected_type()

                    if window_type == "New pattern based plot window":
                        self.load_window(
                            {
                                "type": "Plot",
                                "title": result["pattern"],
                                "configuration": {"channels": [], "pattern": result},
                            }
                        )
                    elif window_type == "New pattern based numeric window":
                        self.load_window(
                            {
                                "type": "Numeric",
                                "title": result["pattern"],
                                "configuration": {"channels": [], "pattern": result},
                            }
                        )
                    elif window_type == "New pattern based tabular window":
                        self.load_window(
                            {
                                "type": "Tabular",
                                "title": result["pattern"],
                                "configuration": {
                                    "channels": [],
                                    "pattern": result,
                                    "filters": [],
                                    "time_as_date": False,
                                    "sorted": False,
                                    "filtered": False,
                                },
                            }
                        )

            else:

                names = set()
                if view.currentText() == "Internal file structure":
                    iterator = QtWidgets.QTreeWidgetItemIterator(widget)

                    dg_cntr = -1
                    ch_cntr = 0

                    while iterator.value():
                        item = iterator.value()
                        if item.parent() is None:
                            iterator += 1
                            dg_cntr += 1
                            ch_cntr = 0
                            continue

                        if (dg_cntr, ch_cntr) in result:
                            item.setCheckState(0, QtCore.Qt.Checked)
                            names.add((dg_cntr, ch_cntr))

                        iterator += 1
                        ch_cntr += 1
                elif view.currentText() == "Selected channels only":
                    iterator = QtWidgets.QTreeWidgetItemIterator(self.filter_tree)

                    signals = set()
                    while iterator.value():
                        item = iterator.value()

                        if item.checkState(0) == QtCore.Qt.Checked:
                            signals.add(item.text(0))

                        iterator += 1

                    names = signals | result

                    widget.clear()

                    items = []
                    for entry in names:
                        gp_index, ch_index = entry
                        ch = self.mdf.groups[gp_index].channels[ch_index]
                        channel = TreeItem(entry, ch.name, mdf_uuid=self.uuid)
                        channel.setText(0, ch.name)
                        channel.setCheckState(0, QtCore.Qt.Checked)
                        items.append(channel)

                    if len(items) < 30000:
                        items = natsorted(items, key=lambda x: x.name)
                    else:
                        items.sort(key=lambda x: x.name)
                    widget.addTopLevelItems(items)

                else:
                    iterator = QtWidgets.QTreeWidgetItemIterator(widget)
                    while iterator.value():
                        item = iterator.value()

                        if item.entry in result:
                            item.setCheckState(0, QtCore.Qt.Checked)
                            names.add(item.entry)

                        iterator += 1

                if dlg.add_window_request:
                    options = [
                        "New plot window",
                        "New numeric window",
                        "New tabular window",
                    ] + [
                        mdi.windowTitle()
                        for mdi in self.mdi_area.subWindowList()
                        if not isinstance(mdi.widget(), CANBusTrace)
                    ]

                    dialog = WindowSelectionDialog(options=options, parent=self)
                    dialog.setModal(True)
                    dialog.exec_()

                    if dialog.result():
                        window_type = dialog.selected_type()

                        signals = [
                            (None, *self.mdf.whereis(name)[0], self.uuid)
                            if isinstance(name, str)
                            else (None, *name, self.uuid)
                            for name in names
                        ]

                        if window_type == "New plot window":
                            self.add_window(["Plot", signals])
                        elif window_type == "New numeric window":
                            self.add_window(["Numeric", signals])
                        elif window_type == "New tabular window":
                            self.add_window(["Tabular", signals])
                        else:
                            for mdi in self.mdi_area.subWindowList():
                                if mdi.windowTitle() == window_type:
                                    self.add_new_channels(signals, mdi.widget())
                                    break

        if toggle_frames:
            self.toggle_frames()

    def to_config(self):
        config = {}

        iterator = QtWidgets.QTreeWidgetItemIterator(self.channels_tree)

        signals = []
        if self.channel_view.currentText() == "Internal file structure":
            while iterator.value():
                item = iterator.value()
                if item.parent() is None:
                    iterator += 1
                    continue

                if item.checkState(0) == QtCore.Qt.Checked:
                    signals.append(item.text(0))

                iterator += 1
        else:
            while iterator.value():
                item = iterator.value()

                if item.checkState(0) == QtCore.Qt.Checked:
                    signals.append(item.text(0))

                iterator += 1

        config["selected_channels"] = signals

        windows = []
        for window in self.mdi_area.subWindowList():
            wid = window.widget()
            geometry = window.geometry()
            window_config = {
                "title": window.windowTitle(),
                "configuration": wid.to_config(),
                "geometry": [
                    geometry.x(),
                    geometry.y(),
                    geometry.width(),
                    geometry.height(),
                ],
            }
            if isinstance(wid, Numeric):
                window_config["type"] = "Numeric"
            elif isinstance(wid, Plot):
                window_config["type"] = "Plot"
            elif isinstance(wid, Tabular):
                window_config["type"] = "Tabular"
            elif isinstance(wid, GPS):
                window_config["type"] = "GPS"
            elif isinstance(wid, CANBusTrace):
                continue

            windows.append(window_config)

        config["windows"] = windows

        return config

    def save_channel_list(self, event=None, file_name=None):

        if file_name is None:
            file_name, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Select output channel list file", "", "TXT files (*.txt)"
            )

        if file_name:
            Path(file_name).write_text(
                json.dumps(self.to_config(), indent=4, sort_keys=True)
            )

    def load_channel_list(self, event=None, file_name=None):
        if file_name is None:
            file_name, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "Select channel list file",
                "",
                "Config file (*.cfg);;TXT files (*.txt);;Display files (*.dsp);;CANape Lab file (*.lab);;All file types (*.cfg *.dsp *.lab *.txt)",
                "All file types (*.cfg *.dsp *.lab *.txt)",
            )

        if file_name:
            if not isinstance(file_name, dict):
                file_name = Path(file_name)

                extension = file_name.suffix.lower()
                if extension == ".dsp":
                    info = load_dsp(file_name)
                    channels = info.get("display", [])

                elif extension == ".lab":
                    info = load_lab(file_name)
                    if info:
                        section, ok = QtWidgets.QInputDialog.getItem(
                            None,
                            "Select section",
                            "Available sections:",
                            list(info),
                            0,
                            False,
                        )
                        if ok:
                            channels = info[section]
                        else:
                            return

                elif extension in (".cfg", ".txt"):
                    with open(file_name, "r") as infile:
                        info = json.load(infile)
                    channels = info.get("selected_channels", [])

            else:
                info = file_name
                channels = info.get("selected_channels", [])

            if channels:
                iterator = QtWidgets.QTreeWidgetItemIterator(self.channels_tree)

                if self.channel_view.currentText() == "Internal file structure":
                    while iterator.value():
                        item = iterator.value()
                        if item.parent() is None:
                            iterator += 1
                            continue

                        channel_name = item.text(0)
                        if channel_name in channels:
                            item.setCheckState(0, QtCore.Qt.Checked)
                            channels.pop(channels.index(channel_name))
                        else:
                            item.setCheckState(0, QtCore.Qt.Unchecked)

                        iterator += 1
                else:
                    while iterator.value():
                        item = iterator.value()

                        channel_name = item.text(0)
                        if channel_name in channels:
                            item.setCheckState(0, QtCore.Qt.Checked)
                            channels.pop(channels.index(channel_name))
                        else:
                            item.setCheckState(0, QtCore.Qt.Unchecked)

                        iterator += 1

            for window in info.get("windows", []):
                self.load_window(window)

    def save_filter_list(self):
        file_name, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Select output filter list file", "", "TXT files (*.txt)"
        )

        if file_name:
            with open(file_name, "w") as output:
                iterator = QtWidgets.QTreeWidgetItemIterator(self.filter_tree)

                signals = []
                if self.filter_view.currentText() == "Internal file structure":
                    while iterator.value():
                        item = iterator.value()
                        if item.parent() is None:
                            iterator += 1
                            continue

                        if item.checkState(0) == QtCore.Qt.Checked:
                            signals.append(item.text(0))

                        iterator += 1
                else:
                    while iterator.value():
                        item = iterator.value()

                        if item.checkState(0) == QtCore.Qt.Checked:
                            signals.append(item.text(0))

                        iterator += 1

                output.write("\n".join(signals))

    def load_filter_list(self, event=None, file_name=None):
        if file_name is None:
            file_name, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "Select channel list file",
                "",
                "Config file (*.cfg);;TXT files (*.txt);;Display files (*.dsp);;CANape Lab file (*.lab);;All file types (*.cfg *.dsp *.lab *.txt)",
                "All file types (*.cfg *.dsp *.lab *.txt)",
            )

        if file_name:
            if not isinstance(file_name, dict):
                file_name = Path(file_name)

                extension = file_name.suffix.lower()
                if extension == ".dsp":
                    info = load_dsp(file_name)
                    channels = info.get("display", [])

                elif extension == ".lab":
                    info = load_lab(file_name)
                    if info:
                        section, ok = QtWidgets.QInputDialog.getItem(
                            None,
                            "Select section",
                            "Available sections:",
                            list(info),
                            0,
                            False,
                        )
                        if ok:
                            channels = info[section]
                        else:
                            return

                elif extension == ".cfg":
                    with open(file_name, "r") as infile:
                        info = json.load(infile)
                    channels = info.get("selected_channels", [])
                elif extension == ".txt":
                    try:
                        with open(file_name, "r") as infile:
                            info = json.load(infile)
                        channels = info.get("selected_channels", [])
                    except:
                        with open(file_name, "r") as infile:
                            channels = [line.strip() for line in infile.readlines()]
                            channels = [name for name in channels if name]

            else:
                info = file_name
                channels = info.get("selected_channels", [])

            if channels:

                iterator = QtWidgets.QTreeWidgetItemIterator(self.filter_tree)

                if self.channel_view.currentText() == "Internal file structure":
                    while iterator.value():
                        item = iterator.value()
                        if item.parent() is None:
                            iterator += 1
                            continue

                        channel_name = item.text(0)
                        if channel_name in channels:
                            item.setCheckState(0, QtCore.Qt.Checked)
                            channels.pop(channels.index(channel_name))
                        else:
                            item.setCheckState(0, QtCore.Qt.Unchecked)

                        iterator += 1
                elif self.channel_view.currentText() == "Natural sort":
                    while iterator.value():
                        item = iterator.value()

                        channel_name = item.text(0)
                        if channel_name in channels:
                            item.setCheckState(0, QtCore.Qt.Checked)
                            channels.pop(channels.index(channel_name))
                        else:
                            item.setCheckState(0, QtCore.Qt.Unchecked)

                        iterator += 1

                else:
                    items = []
                    self.filter_tree.clear()

                    for i, gp in enumerate(self.mdf.groups):
                        for j, ch in enumerate(gp.channels):
                            if ch.name in channels:
                                entry = i, j
                                channel = TreeItem(entry, ch.name, mdf_uuid=self.uuid)
                                channel.setText(0, ch.name)
                                channel.setCheckState(0, QtCore.Qt.Checked)
                                items.append(channel)

                                channels.pop(channels.index(ch.name))

                    if len(items) < 30000:
                        items = natsorted(items, key=lambda x: x.name)
                    else:
                        items.sort(key=lambda x: x.name)
                    self.filter_tree.addTopLevelItems(items)

    def compute_cut_hints(self):
        t_min = []
        t_max = []
        for i, group in enumerate(self.mdf.groups):
            cycles_nr = group.channel_group.cycles_nr
            if cycles_nr:
                master_min = self.mdf.get_master(i, record_offset=0, record_count=1)
                if len(master_min):
                    t_min.append(master_min[0])
                self.mdf._master_channel_cache.clear()
                master_max = self.mdf.get_master(
                    i, record_offset=cycles_nr - 1, record_count=1
                )
                if len(master_max):
                    t_max.append(master_max[0])
                self.mdf._master_channel_cache.clear()

        if t_min:
            time_range = t_min, t_max

            self.cut_start.setRange(*time_range)
            self.cut_stop.setRange(*time_range)

            self.cut_interval.setText(
                "Cut interval ({:.6f}s - {:.6f}s)".format(*time_range)
            )
        else:
            self.cut_start.setRange(0, 0)
            self.cut_stop.setRange(0, 0)

            self.cut_interval.setText("Empty measurement")

    def update_progress(self, current_index, max_index):
        self.progress = current_index, max_index

    def show_info(self, item, column):
        group_index, index = item.entry
        if index == 0xFFFFFFFFFFFFFFFF:
            group = self.mdf.groups[group_index]

            msg = ChannelGroupInfoDialog(self.mdf, group, group_index, self)
            msg.show()
        else:
            channel = self.mdf.get_channel_metadata(group=group_index, index=index)

            msg = ChannelInfoDialog(channel, self)
            msg.show()

    def clear_filter(self):
        iterator = QtWidgets.QTreeWidgetItemIterator(self.filter_tree)

        while iterator.value():
            item = iterator.value()
            item.setCheckState(0, QtCore.Qt.Unchecked)

            if item.parent() is None:
                item.setExpanded(False)

            iterator += 1

    def clear_channels(self):
        iterator = QtWidgets.QTreeWidgetItemIterator(self.channels_tree)

        if self.channel_view.currentIndex() == 1:
            while iterator.value():
                item = iterator.value()
                if item.parent() is None:
                    item.setExpanded(False)
                else:
                    item.setCheckState(0, QtCore.Qt.Unchecked)
                iterator += 1
        else:
            while iterator.value():
                item = iterator.value()
                item.setCheckState(0, QtCore.Qt.Unchecked)
                iterator += 1

    def select_all_channels(self):
        iterator = QtWidgets.QTreeWidgetItemIterator(self.channels_tree)

        if self.channel_view.currentIndex() == 1:
            while iterator.value():
                item = iterator.value()
                if item.parent() is None:
                    item.setExpanded(False)
                else:
                    item.setCheckState(0, QtCore.Qt.Checked)
                iterator += 1
        else:
            while iterator.value():
                item = iterator.value()
                item.setCheckState(0, QtCore.Qt.Checked)
                iterator += 1

    def close(self):
        mdf_name = self.mdf.name
        self.mdf.close()
        if self.file_name.suffix.lower() in (".dl3", ".erg"):
            mdf_name.unlink()
        self.channels_tree.clear()
        self.filter_tree.clear()

        self.mdf = None

    def _create_window(self, event, window_type=None):

        if window_type is None:
            dialog = WindowSelectionDialog(
                options=(
                    "Plot",
                    "Numeric",
                    "Tabular",
                    "CAN Bus Trace",
                    "LIN Bus Trace",
                ),
                parent=self,
            )
            dialog.setModal(True)
            dialog.exec_()

            if dialog.result():
                window_type = dialog.selected_type()
            else:
                window_type = None

        if window_type is None:
            return
        elif window_type in ("CAN Bus Trace", "LIN Bus Trace"):
            signals = []
        elif window_type == "GPS":
            names = sorted(self.mdf.channels_db)
            latitude, ok = QtWidgets.QInputDialog.getItem(
                self,
                "Select the Latitude signals",
                "Latitude signal name:",
                names,
                editable=False,
            )
            if not ok:
                return

            longitude, ok = QtWidgets.QInputDialog.getItem(
                self,
                "Select the Longitude signals",
                "Longitude signal name:",
                names,
                editable=False,
            )
            if not ok:
                return

            signals = [
                (None, *self.mdf.whereis(name)[0]) for name in [latitude, longitude]
            ]
        else:

            try:
                iter(event)
                signals = event
            except:

                iterator = QtWidgets.QTreeWidgetItemIterator(self.channels_tree)

                signals = []

                if self.channel_view.currentIndex() == 1:
                    while iterator.value():
                        item = iterator.value()
                        if item.parent() is None:
                            iterator += 1
                            continue

                        if item.checkState(0) == QtCore.Qt.Checked:
                            group, index = item.entry
                            ch = self.mdf.groups[group].channels[index]
                            if not ch.component_addr:
                                signals.append((None, group, index, self.uuid))

                        iterator += 1
                else:
                    while iterator.value():
                        item = iterator.value()

                        if item.checkState(0) == QtCore.Qt.Checked:
                            group, index = item.entry
                            ch = self.mdf.groups[group].channels[index]
                            if not ch.component_addr:
                                signals.append((None, group, index, self.uuid))

                        iterator += 1

        self.add_window((window_type, signals))

    def scramble(self, event):

        progress = setup_progress(
            parent=self,
            title="Scrambling measurement",
            message=f'Scrambling "{self.file_name}"',
            icon_name="scramble",
        )

        # scrambling self.mdf
        target = MDF.scramble
        kwargs = {"name": self.file_name, "callback": self.update_progress}

        mdf = run_thread_with_progress(
            self, target=target, kwargs=kwargs, factor=100, offset=0, progress=progress
        )

        if mdf is TERMINATED:
            progress.cancel()
            return

        self.progress = None
        progress.cancel()

        path = Path(self.file_name)

        self.open_new_file.emit(str(path.with_suffix(f".scrambled{path.suffix}")))

    def extract_bus_logging(self, event):
        version = self.extract_bus_format.currentText()

        self.output_info_bus.setPlainText("")

        database_files = {}

        count = self.can_database_list.count()
        if count:
            database_files["CAN"] = []
            for i in range(count):
                item = self.can_database_list.item(i)
                widget = self.can_database_list.itemWidget(item)
                database_files["CAN"].append(
                    (widget.database.text(), widget.bus.currentIndex())
                )

        count = self.lin_database_list.count()
        if count:
            database_files["LIN"] = []
            for i in range(count):
                item = self.lin_database_list.item(i)
                widget = self.can_database_list.itemWidget(item)
                database_files["LIN"].append(
                    (widget.database.text(), widget.bus.currentIndex())
                )

        compression = self.extract_bus_compression.currentIndex()
        ignore_invalid_signals = (
            self.ignore_invalid_signals_mdf.checkState() == QtCore.Qt.Checked
        )

        if version < "4.00":
            filter = "MDF version 3 files (*.dat *.mdf)"
            suffix = ".mdf"
        else:
            filter = "MDF version 4 files (*.mf4)"
            suffix = ".mf4"

        if not database_files:
            return

        file_name, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Select output measurement file",
            "",
            f"{filter};;All files (*.*)",
            filter,
        )

        if file_name:

            file_name = Path(file_name).with_suffix(suffix)

            progress = setup_progress(
                parent=self,
                title="Extract Bus logging",
                message=f'Extracting Bus signals from "{self.file_name}"',
                icon_name="down",
            )

            # convert self.mdf
            target = self.mdf.extract_bus_logging
            kwargs = {
                "database_files": database_files,
                "version": version,
                "ignore_invalid_signals": ignore_invalid_signals,
                "prefix": self.prefix.text().strip(),
                "consolidated_j1939": self.consolidated_j1939.checkState()
                == QtCore.Qt.Checked,
            }

            mdf = run_thread_with_progress(
                self,
                target=target,
                kwargs=kwargs,
                factor=70,
                offset=0,
                progress=progress,
            )

            if mdf is TERMINATED:
                progress.cancel()
                return

            # then save it
            progress.setLabelText(f'Saving file to "{file_name}"')

            target = mdf.save
            kwargs = {
                "dst": file_name,
                "compression": compression,
                "overwrite": True,
            }

            run_thread_with_progress(
                self,
                target=target,
                kwargs=kwargs,
                factor=30,
                offset=70,
                progress=progress,
            )

            self.progress = None
            progress.cancel()

            bus_call_info = dict(self.mdf.last_call_info)

            message = []

            for bus, call_info in bus_call_info.items():

                found_id_count = sum(len(e) for e in call_info["found_ids"].values())

                message += [
                    f"{bus} bus summary:",
                    f'- {found_id_count} of {len(call_info["total_unique_ids"])} IDs in the MDF4 file were matched in the DBC and converted',
                ]
                if call_info["unknown_id_count"]:
                    message.append(
                        f'- {call_info["unknown_id_count"]} unknown IDs in the MDF4 file'
                    )
                else:
                    message.append(f"- no unknown IDs inf the MDF4 file")

                message += [
                    "",
                    "Detailed information:",
                    "",
                    f"The following {bus} IDs were in the MDF log file and matched in the DBC:",
                ]
                for dbc_name, found_ids in call_info["found_ids"].items():
                    for msg_id, msg_name in sorted(found_ids):
                        message.append(f"- 0x{msg_id:X} --> {msg_name} in <{dbc_name}>")

                message += [
                    "",
                    f"The following {bus} IDs were in the MDF log file, but not matched in the DBC:",
                ]
                for msg_id in sorted(call_info["unknown_ids"]):
                    message.append(f"- 0x{msg_id:X}")
                message.append("\n\n")

            self.output_info_bus.setPlainText("\n".join(message))

            self.open_new_file.emit(str(file_name))

    def extract_bus_csv_logging(self, event):
        version = self.extract_bus_format.currentText()

        self.output_info_bus.setPlainText("")

        database_files = {}

        count = self.can_database_list.count()
        if count:
            database_files["CAN"] = []
            for i in range(count):
                item = self.can_database_list.item(i)
                widget = self.can_database_list.itemWidget(item)
                database_files["CAN"].append(
                    (widget.database.text(), widget.bus.currentIndex())
                )

        count = self.lin_database_list.count()
        if count:
            database_files["LIN"] = []
            for i in range(count):
                item = self.lin_database_list.item(i)
                widget = self.can_database_list.itemWidget(item)
                database_files["LIN"].append(
                    (widget.database.text(), widget.bus.currentIndex())
                )

        ignore_invalid_signals = (
            self.ignore_invalid_signals_csv.checkState() == QtCore.Qt.Checked
        )
        single_time_base = self.single_time_base_bus.checkState() == QtCore.Qt.Checked
        time_from_zero = self.time_from_zero_bus.checkState() == QtCore.Qt.Checked
        empty_channels = self.empty_channels_bus.currentText()
        raster = self.export_raster_bus.value()
        time_as_date = self.bus_time_as_date.checkState() == QtCore.Qt.Checked
        delimiter = self.delimiter_bus.text() or ","
        doublequote = self.doublequote_bus.checkState() == QtCore.Qt.Checked
        escapechar = self.escapechar_bus.text() or None
        lineterminator = (
            self.lineterminator_bus.text().replace("\\r", "\r").replace("\\n", "\n")
        )
        quotechar = self.quotechar_bus.text() or '"'
        quoting = self.quoting_bus.currentText()

        file_name, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Select output CSV file",
            "",
            "CSV (*.csv);;All files (*.*)",
            "CSV (*.csv)",
        )

        if file_name:

            progress = setup_progress(
                parent=self,
                title="Extract Bus logging to CSV",
                message=f'Extracting Bus signals from "{self.file_name}"',
                icon_name="csv",
            )

            # convert self.mdf
            target = self.mdf.extract_bus_logging
            kwargs = {
                "database_files": database_files,
                "version": version,
                "ignore_invalid_signals": ignore_invalid_signals,
                "prefix": self.prefix.text().strip(),
                "consolidated_j1939": self.consolidated_j1939.checkState()
                == QtCore.Qt.Checked,
            }

            mdf = run_thread_with_progress(
                self,
                target=target,
                kwargs=kwargs,
                factor=70,
                offset=0,
                progress=progress,
            )

            if mdf is TERMINATED:
                progress.cancel()
                return

            # then save it
            progress.setLabelText(f'Saving file to "{file_name}"')

            mdf.configure(
                integer_interpolation=self.mdf._integer_interpolation,
                float_interpolation=self.mdf._float_interpolation,
            )

            target = mdf.export
            kwargs = {
                "fmt": "csv",
                "filename": file_name,
                "single_time_base": single_time_base,
                "time_from_zero": time_from_zero,
                "empty_channels": empty_channels,
                "raster": raster or None,
                "time_as_date": time_as_date,
                "ignore_value2text_conversions": self.ignore_value2text_conversions,
                "delimiter": delimiter,
                "doublequote": doublequote,
                "escapechar": escapechar,
                "lineterminator": lineterminator,
                "quotechar": quotechar,
                "quoting": quoting,
            }

            run_thread_with_progress(
                self,
                target=target,
                kwargs=kwargs,
                factor=30,
                offset=70,
                progress=progress,
            )

            self.progress = None
            progress.cancel()

            bus_call_info = dict(self.mdf.last_call_info)

            message = []

            for bus, call_info in bus_call_info.items():

                found_id_count = sum(len(e) for e in call_info["found_ids"].values())

                message += [
                    f"{bus} bus summary:",
                    f'- {found_id_count} of {len(call_info["total_unique_ids"])} IDs in the MDF4 file were matched in the DBC and converted',
                ]
                if call_info["unknown_id_count"]:
                    message.append(
                        f'- {call_info["unknown_id_count"]} unknown IDs in the MDF4 file'
                    )
                else:
                    message.append(f"- no unknown IDs inf the MDF4 file")

                message += [
                    "",
                    "Detailed information:",
                    "",
                    f"The following {bus} IDs were in the MDF log file and matched in the DBC:",
                ]
                for dbc_name, found_ids in call_info["found_ids"].items():
                    for msg_id, msg_name in sorted(found_ids):
                        message.append(f"- 0x{msg_id:X} --> {msg_name} in <{dbc_name}>")

                message += [
                    "",
                    f"The following {bus} IDs were in the MDF log file, but not matched in the DBC:",
                ]
                for msg_id in sorted(call_info["unknown_ids"]):
                    message.append(f"- 0x{msg_id:X}")
                message.append("\n\n")

            self.output_info_bus.setPlainText("\n".join(message))

    def load_can_database(self, event):
        file_names, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "Select CAN database file",
            "",
            "ARXML or DBC (*.dbc *.arxml)",
            "ARXML or DBC (*.dbc *.arxml)",
        )

        if file_names:
            for database in file_names:
                item = QtWidgets.QListWidgetItem()
                widget = DatabaseItem(database, bus_type="CAN")

                self.can_database_list.addItem(item)
                self.can_database_list.setItemWidget(item, widget)
                item.setSizeHint(widget.sizeHint())

    def load_lin_database(self, event):
        file_names, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "Select LIN database file",
            "",
            "ARXML or DBC database (*.dbc *.arxml);;LDF database (*.ldf);;All supported formats (*.dbc *.arxml *ldf)",
            "All supported formats (*.dbc *.arxml *ldf)",
        )

        if file_names:
            for database in file_names:
                item = QtWidgets.QListWidgetItem()
                widget = DatabaseItem(database, bus_type="LIN")

                self.lin_database_list.addItem(item)
                self.lin_database_list.setItemWidget(item, widget)
                item.setSizeHint(widget.sizeHint())

    def keyPressEvent(self, event):
        key = event.key()
        modifier = event.modifiers()

        if key == QtCore.Qt.Key_F and modifier == QtCore.Qt.ControlModifier:
            self.search()

        elif key == QtCore.Qt.Key_F8:
            self.full_screen_toggled.emit()

        elif (
            key in (QtCore.Qt.Key_V, QtCore.Qt.Key_H, QtCore.Qt.Key_C, QtCore.Qt.Key_T)
            and modifier == QtCore.Qt.ShiftModifier
        ):
            if key == QtCore.Qt.Key_V:
                mode = "tile vertically"
            elif key == QtCore.Qt.Key_H:
                mode = "tile horizontally"
            elif key == QtCore.Qt.Key_C:
                mode = "cascade"
            elif key == QtCore.Qt.Key_T:
                mode = "tile"

            if mode == "tile":
                self.mdi_area.tileSubWindows()
            elif mode == "cascade":
                self.mdi_area.cascadeSubWindows()
            elif mode == "tile vertically":
                self.mdi_area.tile_vertically()
            elif mode == "tile horizontally":
                self.mdi_area.tile_horizontally()

        elif key == QtCore.Qt.Key_F and modifier == QtCore.Qt.ShiftModifier:
            self.toggle_frames()

        elif key == QtCore.Qt.Key_L and modifier == QtCore.Qt.ShiftModifier:
            sizes = self.splitter.sizes()
            if sizes[0]:
                self._splitter_sizes = sizes
                self.splitter.setSizes([0, sum(sizes)])
            else:
                self.splitter.setSizes(self._splitter_sizes)

        elif key == QtCore.Qt.Key_Period and modifier == QtCore.Qt.NoModifier:
            self.set_line_style()

        else:
            super().keyPressEvent(event)

    def aspect_changed(self, index):

        if self.aspects.tabText(self.aspects.currentIndex()) == "Modify && Export":

            if not self.raster_channel.count():
                self.raster_channel.setSizeAdjustPolicy(
                    QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
                )
                self.raster_channel.addItems(self.channels_db_items)
                self.raster_channel.setMinimumWidth(100)

            if not self._show_filter_tree:
                self._show_filter_tree = True

                widget = self.filter_tree

                if self.filter_view.currentText() == "Natural sort":
                    items = []
                    for i, group in enumerate(self.mdf.groups):
                        for j, ch in enumerate(group.channels):
                            entry = i, j

                            channel = TreeItem(entry, ch.name)
                            channel.setText(0, ch.name)
                            channel.setCheckState(0, QtCore.Qt.Unchecked)
                            items.append(channel)

                    if len(items) < 30000:
                        items = natsorted(items, key=lambda x: x.name)
                    else:
                        items.sort(key=lambda x: x.name)
                    widget.addTopLevelItems(items)

                elif self.filter_view.currentText() == "Internal file structure":
                    for i, group in enumerate(self.mdf.groups):
                        entry = i, 0xFFFFFFFFFFFFFFFF
                        channel_group = TreeItem(entry)
                        comment = group.channel_group.comment
                        comment = extract_cncomment_xml(comment)

                        if comment:
                            channel_group.setText(0, f"Channel group {i} ({comment})")
                        else:
                            channel_group.setText(0, f"Channel group {i}")
                        channel_group.setFlags(
                            channel_group.flags()
                            | QtCore.Qt.ItemIsTristate
                            | QtCore.Qt.ItemIsUserCheckable
                        )

                        widget.addTopLevelItem(channel_group)

                        channels = [
                            HelperChannel(name=ch.name, entry=(i, j))
                            for j, ch in enumerate(group.channels)
                        ]

                        add_children(
                            channel_group,
                            channels,
                            group.channel_dependencies,
                            set(),
                            entries=None,
                            version=self.mdf.version,
                        )

    def toggle_frames(self, event=None):
        self._frameless_windows = not self._frameless_windows

        for w in self.mdi_area.subWindowList():
            if self._frameless_windows:
                w.setWindowFlags(w.windowFlags() | QtCore.Qt.FramelessWindowHint)
            else:
                w.setWindowFlags(w.windowFlags() & (~QtCore.Qt.FramelessWindowHint))

    def autofit_sub_plots(self):
        geometries = []
        for window in self.mdi_area.subWindowList():
            geometry = window.geometry()
            geometries.append(geometry)

        if len(set((g.width(), g.x()) for g in geometries)) == 1:
            self.mdi_area.tile_vertically()
        elif len(set((g.height(), g.y()) for g in geometries)) == 1:
            self.mdi_area.tile_horizontally()
        else:
            self.mdi_area.tileSubWindows()

    def _current_options(self):
        options = {
            "needs_cut": self.cut_group.isChecked(),
            "cut_start": self.cut_start.value(),
            "cut_stop": self.cut_stop.value(),
            "cut_time_from_zero": self.cut_time_from_zero.checkState()
            == QtCore.Qt.Checked,
            "whence": int(self.whence.checkState() == QtCore.Qt.Checked),
            "needs_resample": self.resample_group.isChecked(),
            "raster_type_step": self.raster_type_step.isChecked(),
            "raster_type_channel": self.raster_type_channel.isChecked(),
            "raster": self.raster.value(),
            "raster_channel": self.raster_channel.currentText(),
            "resample_time_from_zero": self.resample_time_from_zero.checkState()
            == QtCore.Qt.Checked,
            "output_format": self.output_format.currentText(),
        }

        output_format = self.output_format.currentText()

        if output_format == "MDF":

            new = {
                "mdf_version": self.mdf_version.currentText(),
                "mdf_compression": self.mdf_compression.currentIndex(),
                "mdf_split": self.mdf_split.checkState() == QtCore.Qt.Checked,
                "mdf_split_size": self.mdf_split_size.value() * 1024 * 1024,
            }

        elif output_format == "MAT":

            new = {
                "single_time_base": self.single_time_base_mat.checkState()
                == QtCore.Qt.Checked,
                "time_from_zero": self.time_from_zero_mat.checkState()
                == QtCore.Qt.Checked,
                "time_as_date": self.time_as_date_mat.checkState() == QtCore.Qt.Checked,
                "use_display_names": self.use_display_names_mat.checkState()
                == QtCore.Qt.Checked,
                "reduce_memory_usage": self.reduce_memory_usage_mat.checkState()
                == QtCore.Qt.Checked,
                "compression": self.export_compression_mat.currentText() == "enabled",
                "empty_channels": self.empty_channels_mat.currentText(),
                "mat_format": self.mat_format.currentText(),
                "oned_as": self.oned_as.currentText(),
                "raw": self.raw_mat.checkState() == QtCore.Qt.Checked,
            }

        elif output_format == "CSV":

            new = {
                "single_time_base": self.single_time_base_csv.checkState()
                == QtCore.Qt.Checked,
                "time_from_zero": self.time_from_zero_csv.checkState()
                == QtCore.Qt.Checked,
                "time_as_date": self.time_as_date_csv.checkState() == QtCore.Qt.Checked,
                "use_display_names": self.use_display_names_csv.checkState()
                == QtCore.Qt.Checked,
                "reduce_memory_usage": False,
                "compression": False,
                "empty_channels": self.empty_channels_csv.currentText(),
                "raw": self.raw_csv.checkState() == QtCore.Qt.Checked,
                "delimiter": self.delimiter.text() or ",",
                "doublequote": self.doublequote.checkState() == QtCore.Qt.Checked,
                "escapechar": self.escapechar.text() or None,
                "lineterminator": self.lineterminator.text()
                .replace("\\r", "\r")
                .replace("\\n", "\n"),
                "quotechar": self.quotechar.text() or '"',
                "quoting": self.quoting.currentText(),
                "mat_format": None,
                "oned_as": None,
            }

        else:

            new = {
                "single_time_base": self.single_time_base.checkState()
                == QtCore.Qt.Checked,
                "time_from_zero": self.time_from_zero.checkState() == QtCore.Qt.Checked,
                "time_as_date": self.time_as_date.checkState() == QtCore.Qt.Checked,
                "use_display_names": self.use_display_names.checkState()
                == QtCore.Qt.Checked,
                "reduce_memory_usage": self.reduce_memory_usage.checkState()
                == QtCore.Qt.Checked,
                "compression": self.export_compression.currentText(),
                "empty_channels": self.empty_channels.currentText(),
                "mat_format": None,
                "oned_as": None,
                "raw": self.raw.checkState() == QtCore.Qt.Checked,
            }

        options.update(new)

        class Options:
            def __init__(self, opts):
                self._opts = opts
                for k, v in opts.items():
                    setattr(self, k, v)

        return Options(options)

    def _get_filtered_channels(self):
        iterator = QtWidgets.QTreeWidgetItemIterator(self.filter_tree)

        channels = []
        count = 0
        total = 0

        if self.filter_view.currentText() == "Internal file structure":
            while iterator.value():
                item = iterator.value()

                group, index = item.entry
                if index != 0xFFFFFFFFFFFFFFFF:
                    total += 1

                if item.checkState(0) == QtCore.Qt.Checked:

                    if index != 0xFFFFFFFFFFFFFFFF:
                        channels.append((None, group, index))
                        count += 1

                iterator += 1
        else:
            while iterator.value():
                item = iterator.value()

                if item.checkState(0) == QtCore.Qt.Checked:
                    group, index = item.entry
                    channels.append((None, group, index))
                    count += 1

                total += 1

                iterator += 1

        if not channels:
            return False, channels
        else:
            if total == count:
                return False, channels
            else:
                return True, channels

    def apply_processing(self, event):

        steps = 1
        if self.cut_group.isChecked():
            steps += 1
        if self.resample_group.isChecked():
            steps += 1
        needs_filter, channels = self._get_filtered_channels()
        if needs_filter:
            steps += 1

        opts = self._current_options()

        output_format = opts.output_format

        if output_format == "MDF":
            version = opts.mdf_version

            if version < "4.00":
                filter = "MDF version 3 files (*.dat *.mdf)"
                default = filter
            else:
                filter = (
                    "MDF version 4 files (*.mf4);;Zipped MDF version 4 files (*.mf4z)"
                )
                if Path(self.mdf.original_name).suffix.lower() == ".mf4z":
                    default = "Zipped MDF version 4 files (*.mf4z)"
                else:
                    default = "MDF version 4 files (*.mf4)"

            file_name, _ = QtWidgets.QFileDialog.getSaveFileName(
                self,
                "Select output measurement file",
                "",
                f"{filter};;All files (*.*)",
                default,
            )

        else:
            filters = {
                "CSV": "CSV files (*.csv)",
                "HDF5": "HDF5 files (*.hdf)",
                "MAT": "Matlab MAT files (*.mat)",
                "Parquet": "Apache Parquet files (*.parquet)",
            }

            file_name, _ = QtWidgets.QFileDialog.getSaveFileName(
                self,
                "Select export file",
                "",
                f"{filters[output_format]};;All files (*.*)",
                filters[output_format],
            )

        if file_name:
            if output_format == "HDF5":
                try:
                    from h5py import File as HDF5
                except ImportError:
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Export to HDF5 unavailale",
                        "h5py package not found; export to HDF5 is unavailable",
                    )
                    return

            elif output_format == "MAT":
                if opts.mat_format == "7.3":
                    try:
                        from hdf5storage import savemat
                    except ImportError:
                        QtWidgets.QMessageBox.critical(
                            self,
                            "Export to mat unavailale",
                            "hdf5storage package not found; export to mat 7.3 is unavailable",
                        )
                        return
                else:
                    try:
                        from scipy.io import savemat
                    except ImportError:
                        QtWidgets.QMessageBox.critical(
                            self,
                            "Export to mat unavailale",
                            "scipy package not found; export to mat is unavailable",
                        )
                        return

            elif output_format == "Parquet":
                try:
                    from fastparquet import write as write_parquet
                except ImportError:
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Export to parquet unavailale",
                        "fastparquet package not found; export to parquet is unavailable",
                    )
                    return
        else:
            return

        split_size = opts.mdf_split_size if output_format == "MDF" else 0
        self.mdf.configure(read_fragment_size=split_size)

        mdf = None
        progress = None
        integer_interpolation = self.mdf._integer_interpolation
        float_interpolation = self.mdf._float_interpolation

        if needs_filter:

            progress = setup_progress(
                parent=self,
                title="Filtering measurement",
                message=f'Filtering selected channels from "{self.file_name}"',
                icon_name="filter",
            )

            # filtering self.mdf
            target = self.mdf.filter
            kwargs = {
                "channels": channels,
                "version": opts.mdf_version if output_format == "MDF" else "4.10",
            }

            result = run_thread_with_progress(
                self,
                target=target,
                kwargs=kwargs,
                factor=99,
                offset=0,
                progress=progress,
            )

            if result is TERMINATED:
                progress.cancel()
                return
            else:
                mdf = result

            mdf.configure(
                read_fragment_size=split_size,
                write_fragment_size=split_size,
                integer_interpolation=integer_interpolation,
                float_interpolation=float_interpolation,
            )

        if opts.needs_cut:

            if progress is None:
                progress = setup_progress(
                    parent=self,
                    title="Cutting measurement",
                    message=f"Cutting from {opts.cut_start}s to {opts.cut_stop}s",
                    icon_name="cut",
                )
            else:
                icon = QtGui.QIcon()
                icon.addPixmap(
                    QtGui.QPixmap(":/cut.png"), QtGui.QIcon.Normal, QtGui.QIcon.Off
                )
                progress.setWindowIcon(icon)
                progress.setWindowTitle("Cutting measurement")
                progress.setLabelText(
                    f"Cutting from {opts.cut_start}s to {opts.cut_stop}s"
                )

            # cut self.mdf
            target = self.mdf.cut if mdf is None else mdf.cut
            kwargs = {
                "start": opts.cut_start,
                "stop": opts.cut_stop,
                "whence": opts.whence,
                "version": opts.mdf_version if output_format == "MDF" else "4.10",
                "time_from_zero": opts.cut_time_from_zero,
            }

            result = run_thread_with_progress(
                self,
                target=target,
                kwargs=kwargs,
                factor=99,
                offset=0,
                progress=progress,
            )

            if result is TERMINATED:
                progress.cancel()
                return
            else:
                if mdf is None:
                    mdf = result
                else:
                    mdf.close()
                    mdf = result

            mdf.configure(
                read_fragment_size=split_size,
                write_fragment_size=split_size,
                integer_interpolation=integer_interpolation,
                float_interpolation=float_interpolation,
            )

        if opts.needs_resample:

            if opts.raster_type_channel:
                raster = opts.raster_channel
                message = f'Resampling using channel "{raster}"'
            else:
                raster = opts.raster
                message = f"Resampling to {raster}s raster"

            if progress is None:
                progress = setup_progress(
                    parent=self,
                    title="Resampling measurement",
                    message=message,
                    icon_name="resample",
                )
            else:
                icon = QtGui.QIcon()
                icon.addPixmap(
                    QtGui.QPixmap(":/resample.png"), QtGui.QIcon.Normal, QtGui.QIcon.Off
                )
                progress.setWindowIcon(icon)
                progress.setWindowTitle("Resampling measurement")
                progress.setLabelText(message)

            # resample self.mdf
            target = self.mdf.resample if mdf is None else mdf.resample
            kwargs = {
                "raster": raster,
                "version": opts.mdf_version if output_format == "MDF" else "4.10",
                "time_from_zero": opts.resample_time_from_zero,
            }

            result = run_thread_with_progress(
                self,
                target=target,
                kwargs=kwargs,
                factor=99,
                offset=0,
                progress=progress,
            )

            if result is TERMINATED:
                progress.cancel()
                return
            else:
                if mdf is None:
                    mdf = result
                else:
                    mdf.close()
                    mdf = result

            mdf.configure(
                read_fragment_size=split_size,
                write_fragment_size=split_size,
                integer_interpolation=integer_interpolation,
                float_interpolation=float_interpolation,
            )

        if output_format == "MDF":
            if mdf is None:
                if progress is None:
                    progress = setup_progress(
                        parent=self,
                        title="Converting measurement",
                        message=f'Converting "{self.file_name}" from {self.mdf.version} to {version}',
                        icon_name="convert",
                    )
                else:
                    icon = QtGui.QIcon()
                    icon.addPixmap(
                        QtGui.QPixmap(":/convert.png"),
                        QtGui.QIcon.Normal,
                        QtGui.QIcon.Off,
                    )
                    progress.setWindowIcon(icon)
                    progress.setWindowTitle("Converting measurement")
                    progress.setLabelText(
                        f'Converting "{self.file_name}" from {self.mdf.version} to {version}'
                    )

                # convert self.mdf
                target = self.mdf.convert
                kwargs = {"version": version}

                result = run_thread_with_progress(
                    self,
                    target=target,
                    kwargs=kwargs,
                    factor=99,
                    offset=0,
                    progress=progress,
                )

                if result is TERMINATED:
                    progress.cancel()
                    return
                else:
                    mdf = result

            mdf.configure(
                read_fragment_size=split_size,
                write_fragment_size=split_size,
                integer_interpolation=integer_interpolation,
                float_interpolation=float_interpolation,
            )

            # then save it
            progress.setLabelText(f'Saving output file "{file_name}"')

            target = mdf.save
            kwargs = {
                "dst": file_name,
                "compression": opts.mdf_compression,
                "overwrite": True,
            }

            run_thread_with_progress(
                self,
                target=target,
                kwargs=kwargs,
                factor=99,
                offset=0,
                progress=progress,
            )

            self.progress = None
            progress.cancel()

        else:
            if progress is None:
                progress = setup_progress(
                    parent=self,
                    title="Export measurement",
                    message=f"Exporting to {output_format}",
                    icon_name="export",
                )
            else:
                icon = QtGui.QIcon()
                icon.addPixmap(
                    QtGui.QPixmap(":/export.png"), QtGui.QIcon.Normal, QtGui.QIcon.Off
                )
                progress.setWindowIcon(icon)
                progress.setWindowTitle("Export measurement")
                progress.setLabelText(f"Exporting to {output_format}")

            delimiter = self.delimiter.text() or ","
            doublequote = self.doublequote.checkState() == QtCore.Qt.Checked
            escapechar = self.escapechar.text() or None
            lineterminator = (
                self.lineterminator.text().replace("\\r", "\r").replace("\\n", "\n")
            )
            quotechar = self.quotechar.text() or '"'
            quoting = self.quoting.currentText()

            target = self.mdf.export if mdf is None else mdf.export
            kwargs = {
                "fmt": opts.output_format.lower(),
                "filename": file_name,
                "single_time_base": opts.single_time_base,
                "use_display_names": opts.use_display_names,
                "time_from_zero": opts.time_from_zero,
                "empty_channels": opts.empty_channels,
                "format": opts.mat_format,
                "raster": None,
                "oned_as": opts.oned_as,
                "reduce_memory_usage": opts.reduce_memory_usage,
                "compression": opts.compression,
                "time_as_date": opts.time_as_date,
                "ignore_value2text_conversions": self.ignore_value2text_conversions,
                "raw": opts.raw,
                "delimiter": delimiter,
                "doublequote": doublequote,
                "escapechar": escapechar,
                "lineterminator": lineterminator,
                "quotechar": quotechar,
                "quoting": quoting,
            }

            result = run_thread_with_progress(
                self,
                target=target,
                kwargs=kwargs,
                factor=99,
                offset=0,
                progress=progress,
            )

            self.progress = None
            progress.cancel()

    def raster_search(self, event):
        dlg = AdvancedSearch(
            self.mdf.channels_db,
            show_add_window=False,
            show_apply=True,
            apply_text="Set raster channel",
            show_pattern=False,
            parent=self,
        )
        dlg.setModal(True)
        dlg.exec_()
        result = dlg.result
        if result:
            dg_cntr, ch_cntr = next(iter(result))

            name = self.mdf.groups[dg_cntr].channels[ch_cntr].name

            self.raster_channel.setCurrentText(name)

    def filter_changed(self, item, column):
        name = item.text(0)
        if item.checkState(0) == QtCore.Qt.Checked:
            self._selected_filter.add(name)
        else:
            if name in self._selected_filter:
                self._selected_filter.remove(name)
        self._filter_timer.start(10)

    def update_selected_filter_channels(self):
        self.selected_filter_channels.clear()
        self.selected_filter_channels.addItems(sorted(self._selected_filter))
