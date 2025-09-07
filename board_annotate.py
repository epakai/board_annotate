#! /usr/bin/env python
"""Board Annotate Inkscape extension"""

# Board Annotate - inkscape extension to annotate circuit boards
# Copyright (C) 2025 Joshua Honeycutt

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301  USA

import os
import copy
import base64
import math
import random
from enum import Enum
from typing import (Generator, Self, List, Tuple, Optional, Dict, Any)

import argparse
import yaml
import inkex
import inkex.gui
from gi.repository import Gtk, GdkPixbuf, Gio, GLib, GObject, Gdk


# Globals
INKSCAPE_SVG: inkex.SvgDocumentElement = None
YAML_FILE: str = ''
YAML_CONFIG = yaml.load("", Loader=yaml.SafeLoader)


class BoardAnnotateExtension(inkex.EffectExtension):
    """Board Annotate Inkscape extension"""

    def add_arguments(self, pars: argparse.ArgumentParser) -> None:
        """Handle arguments from board_annotate.inx dialog"""
        pars.add_argument('--yaml-file', type=str,
                          help='Board YAML configuration')
        pars.add_argument('--tab', dest='tab',
                          help='The selected UI tab when Apply was pressed')

    def effect(self) -> None:
        """Handle info from board_annotate.inx dialog,
        and start GUI selection"""
        global YAML_FILE
        global YAML_CONFIG
        global INKSCAPE_SVG

        YAML_FILE = self.options.yaml_file
        INKSCAPE_SVG = self.svg

        if (YAML_FILE is not None and not os.path.isdir(YAML_FILE)):
            # see https://gitlab.com/inkscape/inkscape/-/issues/2822
            # for why isdir check is used
            with open(YAML_FILE, 'r', encoding='utf-8') as file:
                YAML_CONFIG = yaml.safe_load(file)

        # validate color settings in the yaml config
        # (so user gets warned before trying to match chips)
        AnnotateColors.validate_colors(YAML_CONFIG)
        try:
            sorted_selection = self.sort_check_selection()
        except ValueError as error:
            inkex.utils.errormsg(error)
            raise inkex.utils.AbortExtension
        # TODO probably could do some more early checks

        SelectionApp(start_loop=True,
                     selection=sorted_selection)

    def sort_check_selection(self) -> inkex.elements._selected.ElementList:
        """Sort selection rectangles left to right or top to bottom
        also checks for invalid selections, and a valid gutter setting"""
        if len(INKSCAPE_SVG.selection) == 0:
            inkex.utils.errormsg(
                "No items selected. Board annotate needs "
                "at least one rectangle selected to annotate")
            raise inkex.utils.AbortExtension

        for item in INKSCAPE_SVG.selection:
            if str(item) != 'rect':
                item_id = item.get_id()
                inkex.utils.errormsg(
                    f"Invalid items selected\n"
                    f"Found '{str(item)}':'{item_id}' in selection\n"
                    f"Board annotate only works on rectangles")
                raise inkex.utils.AbortExtension
        if YAML_CONFIG['gutter'] == "horizontal":  # left to right
            return sorted(INKSCAPE_SVG.selection,
                          key=lambda e: float(e.bounding_box().center_x))
        if YAML_CONFIG['gutter'] == "vertical":  # top to bottom
            return sorted(INKSCAPE_SVG.selection,
                          key=lambda e: float(e.bounding_box().center_y))

        raise ValueError("YAML config gutter is not "
                         "'horizontal' or 'vertical' ")


class ChipItem(GObject.Object):
    '''Info and images for a chip'''
    name = ''
    description = ''
    image_path = ''
    image = None
    widget = None

    def __init__(self, builder: Gtk.Builder, name: str, description: str,
                 image_path: str) -> None:
        super().__init__()

        self.name = name
        self.description = description
        self.image_path = image_path
        if image_path and not os.path.isabs(image_path):
            self.image_path = os.path.join(
                os.path.dirname(YAML_FILE), image_path)

        self.tooltip_image = (None if image_path == "" else
                              GdkPixbuf.Pixbuf.new_from_file_at_size(
                                  self.image_path, 256, 256))

        if self.tooltip_image is not None:
            icon_width = self.tooltip_image.get_width() / 8
            icon_height = self.tooltip_image.get_height() / 8
            self.icon_image = GdkPixbuf.Pixbuf.new(
                self.tooltip_image.get_colorspace(),
                self.tooltip_image.get_has_alpha(),
                self.tooltip_image.get_bits_per_sample(),
                icon_width, icon_height)
            self.tooltip_image.scale(self.icon_image,
                                     0, 0, icon_width, icon_height,
                                     0, 0, 1/8, 1/8,
                                     GdkPixbuf.InterpType.BILINEAR)

        self.widget = builder.get_object('chip_item')
        chip_name = builder.get_object('chip_name')
        chip_name.set_markup('<big><b>' + self.name + '</b></big>')
        Gtk.Widget.set_tooltip_text(chip_name, self.description)

        if self.icon_image:
            self.image = GdkPixbuf.Pixbuf.new_from_file(self.image_path)
            chip_image = builder.get_object('chip_image')
            chip_image.set_from_pixbuf(self.icon_image)
            chip_image.set_has_tooltip(True)
            chip_image.connect('query-tooltip', self.on_query_tooltip)

    def on_query_tooltip(self, widget: Gtk.Widget, tooltip_x: int,
                         tooltip_y: int, keyboard_mode: bool,
                         tooltip: Gtk.Tooltip) -> bool:
        '''Set image tooltip to larger image'''
        # pylint: disable=unused-argument
        tooltip.set_icon(self.tooltip_image)
        return True

    @classmethod
    def as_widget(cls, chipitem: Self) -> Gtk.Widget:
        '''Return associated widget for bind_model use'''
        return chipitem.widget


class SelectionWindow(inkex.gui.Window):
    """Window for matching chips to the board image rectangles"""
    primary = True
    name = "board_annotate"

    def __init__(self, widget: Gtk.Widget, *args: List[str],
                 **kwargs: List[List[str]]):
        super().__init__(widget, *args, **kwargs)

        # Chips defined in user provide yaml
        self.chip_items = Gio.ListStore.new(ChipItem)
        for chip in YAML_CONFIG['chips']:
            builder = Gtk.Builder()
            builder.add_from_file(self.gapp.get_ui_file(self.name))
            self.chip_items.append(
                ChipItem(builder, chip['name'], chip['description'],
                         chip['chip_photo']))

        chip_list_box = self.widget('chip_list_box')
        chip_list_box.bind_model(self.chip_items, ChipItem.as_widget)
        chip_list_box.connect('row-activated', self.update_match)

        self.widget('chip_reverse').connect('toggled', self.update_reverse)

        # User selected rectangles to be matched with a chip
        selection_items = self.widget('selection_items')
        for rect in self.gapp.kwargs['selection']:
            svg_render = svg_without_selections_as_pixbuf(
                INKSCAPE_SVG, self.gapp.kwargs['selection'])
            icon_image = rect_icon_image(rect, svg_render)

            context_image = chip_context_image(
                rect, self.gapp.kwargs['selection'])
            selection_items.append([context_image, icon_image, rect.get("id"),
                                    "", False, icon_image.copy()])

        self.selections_icon_view = self.widget('selections_icon_view')
        self.selections_icon_view.set_pixbuf_column(5)
        self.selections_icon_view.set_text_column(2)
        self.selections_icon_view.set_item_width(64)
        self.selections_icon_view.connect('selection-changed',
                                          self.update_selection)
        self.selections_icon_view.select_path(
            selection_items.get_path(selection_items.get_iter_first()))

        accel_group = Gtk.AccelGroup()
        self.window.add_accel_group(accel_group)
        for accel in ['k', '<Shift>k']:
            key, mods = Gtk.accelerator_parse(accel)
            accel_group.connect(key, mods, 0, self.prev_selection)
        for accel in ['j', '<Shift>j']:
            key, mods = Gtk.accelerator_parse(accel)
            accel_group.connect(key, mods, 0, self.next_selection)
        self.widget('previous_match_button').connect(
            'clicked', self.prev_selection)
        self.widget('next_match_button').connect(
            'clicked', self.next_selection)

        self.widget('close_button').connect('clicked', self.on_close_clicked)
        self.widget('apply_button').connect('clicked', self.on_apply_clicked,
                                            selection_items, self.chip_items)
        self.populate_status_bar(selection_items)

        self.window.show_all()
        self.window.connect("destroy", Gtk.main_quit)

    def prev_selection(self, accel: Gtk.AccelGroup = None,
                       key: Optional[int] = None,
                       mods: Gdk.ModifierType = None,
                       accel_flags: Gtk.AccelFlags = None) -> None:
        '''Move selections_icon_view selection back, and update display'''
        # pylint: disable=unused-argument
        path = self.selections_icon_view.get_selected_items()
        model = self.selections_icon_view.get_model()
        icon_view_iter = model.get_iter(path)
        icon_view_iter = model.iter_previous(icon_view_iter)
        if icon_view_iter is None:
            # loop to the end of the list
            icon_view_iter = model.get_iter_first()
            while model.iter_next(icon_view_iter):
                icon_view_iter = model.iter_next(icon_view_iter)

            self.selections_icon_view.select_path(
                model.get_path(icon_view_iter))
            self.selections_icon_view.set_cursor(
                model.get_path(icon_view_iter), None, False)
        else:
            self.selections_icon_view.select_path(
                model.get_path(icon_view_iter))
            self.selections_icon_view.set_cursor(
                model.get_path(icon_view_iter), None, False)

    def next_selection(self, accel: Gtk.AccelGroup = None,
                       key: Optional[int] = None,
                       mods: Gdk.ModifierType = None,
                       accel_flags: Gtk.AccelFlags = None) -> None:
        '''Move selections_icon_view selection forward, and update display'''
        # pylint: disable=unused-argument
        path = self.selections_icon_view.get_selected_items()
        model = self.selections_icon_view.get_model()
        icon_view_iter = model.get_iter(path)
        icon_view_iter = model.iter_next(icon_view_iter)
        if icon_view_iter is None:
            self.selections_icon_view.select_path(model.get_path(
                model.get_iter_first()))
            self.selections_icon_view.set_cursor(model.get_path(
                model.get_iter_first()), None, False)
        else:
            self.selections_icon_view.select_path(
                model.get_path(icon_view_iter))
            self.selections_icon_view.set_cursor(
                model.get_path(icon_view_iter), None, False)

    def update_reverse(self, checkbox: Gtk.CheckButton) -> None:
        '''Update selection when 'On Reverse' checkbox is toggled'''
        selection_model = self.selections_icon_view.get_model()
        selection_path = self.selections_icon_view.get_selected_items()
        selection_model.set_value(
            selection_model.get_iter(selection_path), 4,
            checkbox.get_active())

    def update_match(self, box: Gtk.ListBox = None,
                     row: Gtk.ListBoxRow = None,
                     userdata: None = None) -> None:
        ''' Update selection when match info has changed '''
        # pylint: disable=unused-argument
        selection_items = self.selections_icon_view.get_model()
        selection_path = self.selections_icon_view.get_selected_items()
        if box:
            selected_chip_row = box.get_selected_row()
            if selected_chip_row:
                selected_chip_index = selected_chip_row.get_index()
                selection_items.set_value(
                    selection_items.get_iter(selection_path), 3,
                    self.chip_items[selected_chip_index].name)

        selection_items[selection_path][1].saturate_and_pixelate(
            selection_items[selection_path][5], 0.5, True)

        self.widget('selection_label').set_text(
                selection_items[selection_path][2] +
                (" [" + selection_items[selection_path][3] + "]"
                 if selection_items[selection_path][3] else ""))

        apply_button = self.widget('apply_button')
        if self.populate_status_bar(selection_items) == 0:
            apply_button.set_sensitive(True)
        else:
            apply_button.set_sensitive(False)

    def populate_status_bar(self, selection_items: Gtk.ListStore) -> int:
        '''Set the status bar message (remaining match count)'''
        item_iter = selection_items.get_iter_first()
        item_remaining_count: int = selection_items.iter_n_children()
        while item_iter:
            if selection_items.get_value(item_iter, 3) != "":
                item_remaining_count -= 1
            item_iter = selection_items.iter_next(item_iter)

        status_bar = self.widget('status_bar')
        context_id = status_bar.get_context_id("update_match")
        if item_remaining_count == 0:
            status_bar.push(context_id, "All images are assigned")
        else:
            status_bar.push(context_id, str(item_remaining_count) +
                            " images left to assign.")

        return item_remaining_count

    def update_selection(self, view: Gtk.IconView) -> None:
        ''' Update the view when a selection as changed '''
        selection_items = self.widget('selection_items')
        path = view.get_selected_items()
        if path:
            self.widget('selection_context_image').set_from_pixbuf(
                selection_items[path][0])
            self.widget('selection_label').set_text(
                selection_items[path][2] +
                (" [" + selection_items[path][3] + "]"
                 if selection_items[path][3] else ""))
            self.widget('chip_reverse').set_active(
                selection_items[path][4])
            match = selection_items[path][3]
            if match:
                chip_index = 0
                while self.chip_items.get_item(chip_index).name != match:
                    chip_index += 1

                chip_box = self.widget('chip_list_box')
                row = chip_box.get_row_at_index(chip_index)
                chip_box.select_row(row)
            else:
                self.widget('chip_list_box').unselect_all()

    def on_close_clicked(self, button: Gtk.Button) -> None:
        """Exit the extension"""
        # pylint: disable=unused-argument
        Gtk.main_quit()

    def on_apply_clicked(self,
                         button: Gtk.Button,  # pylint: disable=unused-argument
                         selection_items: Gtk.ListStore,
                         chip_items: Gtk.ListStore) -> None:
        """Modify the svg and exit the extension"""
        annotate_board(selection_items, chip_items)
        Gtk.main_quit()


class SelectionApp(inkex.gui.GtkApp):
    """inkex GtkApp"""
    ui_dir = os.path.join(os.path.dirname(__file__))
    app_name = "org.epakai.extension.board_annotate"
    windows = [SelectionWindow]


class Gutter:
    """Gutter for positioning annotations"""
    index = 0
    offset = 0.0

    class Position(Enum):
        """Possible positions for a gutter"""
        ABOVE = 1
        BELOW = 2
        LEFT = 3
        RIGHT = 4

    def __init__(self, position: Position,
                 board_image: inkex.Image) -> None:
        """Set up the gutter in position near the board_image"""
        self.position = position
        self.image_ratio = (YAML_CONFIG['image_ratio']
                            if 'image_ratio' in YAML_CONFIG
                            else 0.6)
        match position:
            case self.Position.ABOVE:
                self.gutter_size = board_image.top
            case self.Position.BELOW:
                self.gutter_size = (INKSCAPE_SVG.viewbox_height -
                                    board_image.bottom)
            case self.Position.LEFT:
                self.gutter_size = board_image.left
            case self.Position.RIGHT:
                self.gutter_size = (INKSCAPE_SVG.viewbox_width -
                                    board_image.right)

        stroke_width = INKSCAPE_SVG.viewport_to_unit("1mm")
        self.image_display_size = (self.gutter_size * self.image_ratio -
                                   stroke_width)

        match position:
            case self.Position.ABOVE:
                self.main_image_edge = board_image.top
            case self.Position.BELOW:
                self.main_image_edge = board_image.bottom
            case self.Position.LEFT:
                self.main_image_edge = board_image.left
            case self.Position.RIGHT:
                self.main_image_edge = board_image.right

    def get_approximate_corners(self) -> Tuple[List[float], List[float]]:
        """
        return a tuple of the 2 corners closest to the main image edge
        ([x,y],[x,y])
        Gutter's don't know their contents, so the second corner is made up
        (based on a square image)
        """
        match self.position:
            case self.Position.ABOVE | self.Position.BELOW:
                return ([self.offset, self.main_image_edge],
                        [self.offset + self.image_display_size,
                         self.main_image_edge])
            case self.Position.LEFT | self.Position.RIGHT:
                return ([self.main_image_edge, self.offset],
                        [self.main_image_edge,
                         self.offset + self.image_display_size])

    def get_position_size(self, width: int, height: int
                          ) -> Tuple[float, float, float, float]:
        """return tuple of x, y, width, height
        where the annotation surround should be placed"""
        # Work around annotations without images
        if width == 0:
            width = self.image_display_size
        if height == 0:
            height = self.image_display_size

        stroke_width = INKSCAPE_SVG.viewport_to_unit("1mm")
        match self.position:
            case self.Position.ABOVE:
                return (
                    self.offset + (0.5 * stroke_width),
                    0 + (0.5 * stroke_width),
                    (self.image_display_size * (width/height) + stroke_width),
                    self.main_image_edge - stroke_width)
            case self.Position.BELOW:
                return (
                    self.offset + (0.5 * stroke_width),
                    self.main_image_edge + (0.5 * stroke_width),
                    (self.image_display_size * (width/height) + stroke_width),
                    (INKSCAPE_SVG.viewbox_height -
                     self.main_image_edge - stroke_width))
            case self.Position.LEFT:
                return (
                    0 + (0.5 * stroke_width),
                    self.offset + (0.5 * stroke_width),
                    self.main_image_edge - stroke_width,
                    (self.image_display_size * (height/width) + stroke_width))
            case  self.Position.RIGHT:
                return (
                    self.main_image_edge + (0.5 * stroke_width),
                    self.offset + (0.5 * stroke_width),
                    (INKSCAPE_SVG.viewbox_width -
                     self.main_image_edge - stroke_width),
                    (self.image_display_size * (height/width) + stroke_width))

    def get_image_position_size(self, width: int, height: int
                                ) -> Tuple[float, float, float, float]:
        """return tuple of x, y, width, height
        where the image should be placed"""
        stroke_width = INKSCAPE_SVG.viewport_to_unit("1mm")
        match self.position:
            case self.Position.ABOVE:
                return (
                    self.offset + stroke_width,
                    (self.main_image_edge -
                     self.image_display_size - stroke_width),
                    self.image_display_size * (width / height),
                    self.image_display_size)
            case self.Position.BELOW:
                return (
                    self.offset + stroke_width,
                    self.main_image_edge + stroke_width,
                    self.image_display_size * (width / height),
                    self.image_display_size)
            case self.Position.LEFT:
                return (
                    (self.main_image_edge -
                     self.image_display_size - stroke_width),
                    self.offset + stroke_width,
                    self.image_display_size,
                    self.image_display_size * (height / width))
            case self.Position.RIGHT:
                return (
                    self.main_image_edge + stroke_width,
                    self.offset + stroke_width,
                    self.image_display_size,
                    self.image_display_size * (height / width))

    def increment(self, width: float, height: float) -> None:
        """set up for placing the next annotation"""
        self.index += 1
        stroke_width = INKSCAPE_SVG.viewport_to_unit("1mm")
        match self.position:
            case self.Position.ABOVE | self.Position.BELOW:
                self.offset += width + stroke_width
            case self.Position.LEFT | self.Position.RIGHT:
                self.offset += height + stroke_width


class Annotation(inkex.Layer):
    """Annotation as an inkscape layer"""
    def __init__(self, rectangle: inkex.Rectangle, name: str, description: str,
                 image_path: str, gdkpixbuf: GdkPixbuf.Pixbuf,
                 color: str, reverse: bool) -> None:
        self.rectangle = rectangle
        self.name = name
        self.description = description
        self.image_path = image_path
        self.gdkpixbuf = gdkpixbuf
        self.color = inkex.Color(color)
        self.reverse = reverse

        self.gutter: Optional[Gutter] = None  # set in draw
        self.svg_image: inkex.Image = None  # set in draw_image
        self.surround: inkex.Rectangle = None  # set in draw_surround

        if self.gdkpixbuf is not None:
            self.image_width = gdkpixbuf.get_width()
            self.image_height = gdkpixbuf.get_height()
        else:
            self.image_width, self.image_height = 0.0, 0.0

        super().__init__()

        self.set("inkscape:label", self.name)
        self.set("inkscape:highlight-color", self.color)

    def draw_existing(self, duplicate: Self) -> None:
        """Connect a chip rectangle to an existing annotation"""
        self.color = duplicate.color
        self.set("inkscape:label", self.name)
        self.set("inkscape:highlight-color", self.color)

        self.draw_connector(duplicate)
        self.update_rectangle_style()
        # gutter remains in the same state, no increment

    def draw(self, gutter: Gutter) -> None:
        """Draw the annotation"""
        # save gutter in case a duplicate needs it
        self.gutter = gutter

        INKSCAPE_SVG.add(self)  # Add the Annotation layer

        self.draw_image()
        self.draw_surround()
        self.draw_text()
        self.draw_connector()
        self.update_rectangle_style()

        # Increment the gutter after everything is drawn
        surround_bb = self.surround.bounding_box()
        gutter.increment(surround_bb.width, surround_bb.height)

    def draw_image(self) -> None:
        """embed and place the annotation image in the original svg"""
        assert self.gutter is not None
        if self.image_path and self.gdkpixbuf:
            position_size = self.gutter.get_image_position_size(
                self.image_width, self.image_height)
            # TODO inkscape 1.5 adds inkex.Image
            # After that you can remove BoardAnnotateImage and get_image_type
            self.svg_image = BoardAnnotateImage.new(*position_size)
            self.svg_image.embed_image(self.image_path)
            self.add(self.svg_image)
            self.svg_image.label = "chip image"

    def draw_surround(self) -> None:
        """draw the annotation surrounding rectangle"""
        assert self.gutter is not None
        position_size = self.gutter.get_position_size(
            self.image_width, self.image_height)
        stroke_width = INKSCAPE_SVG.viewport_to_unit("1mm")
        self.surround = inkex.Rectangle.new(*position_size)
        self.surround.style['stroke-width'] = stroke_width
        self.surround.style.set_color(self.color, 'stroke')
        self.surround.style.set_color(inkex.Color('none'), 'fill')
        # Prevent connectors being drawn through adjacent annotations
        # TODO would like to avoid them passing through the gutter at all
        self.surround.set("inkscape:connector-avoid", 'true')
        self.add(self.surround)
        self.surround.label = "surround"

    def draw_text(self) -> None:
        """Place invisible boxes, and shape the text inside them.
        One for title, and one for description."""
        # TODO try to fit title_box to the title, then give rest to desc_box
        # TODO better fonts
        # TODO better way to set font size based on box dimension
        assert self.gutter is not None
        surround_bb = self.surround.bounding_box()
        title_box = None
        text_ratio = 1 - self.gutter.image_ratio
        stroke_width = INKSCAPE_SVG.viewport_to_unit("1mm")
        half_stroke_width = 0.5 * stroke_width
        above_half_height = 0.5 * (self.svg_image.top -
                                   (surround_bb.top +
                                    half_stroke_width))
        below_half_height = 0.5 * ((surround_bb.bottom -
                                    half_stroke_width) -
                                   self.svg_image.bottom)
        vertical_half_height = 0.5 * (surround_bb.height - stroke_width)

        title_box = None
        match self.gutter.position:
            case Gutter.Position.ABOVE:
                title_box = inkex.Rectangle.new(
                    surround_bb.left + half_stroke_width,
                    surround_bb.top + half_stroke_width,
                    surround_bb.width - stroke_width,
                    above_half_height)
            case Gutter.Position.BELOW:
                title_box = inkex.Rectangle.new(
                    surround_bb.left + half_stroke_width,
                    self.svg_image.bottom,
                    surround_bb.width - stroke_width,
                    below_half_height)
            case Gutter.Position.LEFT:
                title_box = inkex.Rectangle.new(
                    surround_bb.left + half_stroke_width,
                    surround_bb.top + half_stroke_width,
                    surround_bb.width * text_ratio - half_stroke_width,
                    vertical_half_height)
            case Gutter.Position.RIGHT:
                title_box = inkex.Rectangle.new(
                    self.svg_image.right,
                    surround_bb.top + half_stroke_width,
                    surround_bb.width * text_ratio - half_stroke_width,
                    vertical_half_height)

        title_box.style.set_color(inkex.Color('none'), 'stroke')
        title_box.style.set_color(inkex.Color('none'), 'fill')
        self.add(title_box)
        title_box.label = "title shape"

        title = inkex.TextElement()
        title.text = self.name
        title.style['font-size'] = INKSCAPE_SVG.viewport_to_unit("10pt")
        title.style['text-anchor'] = "middle"
        title.style['shape-inside'] = title_box.get_id(as_url=2)
        self.add(title)
        title.label = "title text"

        desc_box = None
        match self.gutter.position:
            case Gutter.Position.ABOVE:
                desc_box = inkex.Rectangle.new(
                    surround_bb.left + half_stroke_width,
                    (surround_bb.top + above_half_height +
                     half_stroke_width),
                    surround_bb.width - stroke_width,
                    above_half_height)
            case Gutter.Position.BELOW:
                desc_box = inkex.Rectangle.new(
                    surround_bb.left + half_stroke_width,
                    (surround_bb.bottom - below_half_height -
                     half_stroke_width),
                    surround_bb.width - stroke_width,
                    below_half_height)
            case Gutter.Position.LEFT:
                desc_box = inkex.Rectangle.new(
                    surround_bb.left + half_stroke_width,
                    (surround_bb.top + vertical_half_height +
                     half_stroke_width),
                    surround_bb.width * text_ratio - half_stroke_width,
                    vertical_half_height)
            case Gutter.Position.RIGHT:
                desc_box = inkex.Rectangle.new(
                    surround_bb.right - (surround_bb.width * text_ratio),
                    (surround_bb.top + vertical_half_height +
                     half_stroke_width),
                    surround_bb.width * text_ratio - half_stroke_width,
                    vertical_half_height)

        desc_box.style.set_color(inkex.Color('none'), 'stroke')
        desc_box.style.set_color(inkex.Color('none'), 'fill')
        self.add(desc_box)
        desc_box.label = "description shape"

        desc = inkex.TextElement()
        desc.text = self.description
        desc.style['font-size'] = INKSCAPE_SVG.viewport_to_unit("8pt")
        desc.style['text-anchor'] = "start"
        desc.style['line-height'] = "1"
        desc.style['shape-inside'] = desc_box.get_id(as_url=2)
        self.add(desc)
        desc.label = "description text"

    def draw_connector(self, duplicate: Optional[Self] = None) -> None:
        """connect the surrounding rect and the old rect with a path"""
        path = inkex.PathElement()
        path.style['stroke-width'] = INKSCAPE_SVG.viewport_to_unit("1mm")
        path.style.set_color(self.color, 'stroke')
        # Make it a connector
        path.set("inkscape:connector-type", "polyline")
        path.set("inkscape:connector-curvature", 0)
        # Connections need a url id like '#id'
        path.set("inkscape:connection-start", self.rectangle.get_id(as_url=1))
        if duplicate is None:
            path.set("inkscape:connection-end", self.surround.get_id(as_url=1))
            self.add(path)
        else:
            path.set("inkscape:connection-end",
                     duplicate.surround.get_id(as_url=1))
            duplicate.add(path)

        path.label = "connector"

    def update_rectangle_style(self) -> None:
        """Give the user drawn rectangle a matching color and stroke style"""
        self.rectangle.style['stroke-width'] = (
            INKSCAPE_SVG.viewport_to_unit("1mm"))
        self.rectangle.style.set_color(self.color, 'stroke')
        self.rectangle.style.set_color(inkex.Color("none"), 'fill')
        if self.reverse:
            # TODO dashes are unsatisfactory
            self.rectangle.style['stroke-dasharray'] = '2,1'
            self.rectangle.style['stroke-dashoffset'] = 0

        # Originally I grouped the user drawn rectangles with everything,
        # but that makes it harder to re-position the annotation, so don't
        # Just label it to make the association clear
        self.rectangle.label = "chip_location " + self.name


def annotate_board(selection_items: Gtk.ListStore,
                   chip_items: Gtk.ListStore) -> None:
    """Set up the gutters and iterate through the selections
    drawing annotations"""
    board_image = find_board_image()
    gutter_a, gutter_b = None, None
    if YAML_CONFIG['gutter'] == 'horizontal':
        gutter_a = Gutter(Gutter.Position.ABOVE, board_image)
        gutter_b = Gutter(Gutter.Position.BELOW, board_image)
    elif YAML_CONFIG['gutter'] == 'vertical':
        gutter_a = Gutter(Gutter.Position.LEFT, board_image)
        gutter_b = Gutter(Gutter.Position.RIGHT, board_image)
    else:
        raise ValueError("Bad 'gutter' type in YAML config")

    colors = AnnotateColors()

    completed: List[Annotation] = []
    for selection in selection_items:
        # Create annotation
        annotation: Optional[Annotation] = None
        chip_index = 0
        while chip_items.get_item(chip_index) is not None:
            chip_item = chip_items.get_item(chip_index)
            if (chip_item.name == selection_items.get_value(selection.iter,
                                                            3)):
                annotation = Annotation(
                    rectangle=INKSCAPE_SVG.getElementById(
                        selection_items.get_value(selection.iter, 2)),
                    name=chip_item.name,
                    description=chip_item.description,
                    image_path=chip_item.image_path,
                    gdkpixbuf=chip_item.image,
                    color=colors.next(),
                    reverse=selection_items.get_value(selection.iter, 4))

            chip_index += 1

        if Annotation is None:
            raise TypeError("Failed to create Annotation for"
                            f"{selection_items.get_value(selection.iter, 2)}")
        assert annotation is not None
        # TODO still not happy with this method
        # Some ideas
        # - config option to select a distribution method
        # - just pick a midpoint dividing line
        # - Work from both ends, and position as close as possible while
        #       avoiding overlap (automatic gaps)
        #       gutters will need a start and end offset
        # - let the user distribute the groups afterwards,
        #       could kick rects out of the group to make it easier

        # Probably easiest to store up the annotations, then pick a method
        # collection.deque can do operations from either side

        duplicate = [anno for anno in completed
                     if anno.name == annotation.name]
        if duplicate:
            # Already drew an annotation for this chip
            # connect to it instead of drawing again
            annotation.draw_existing(duplicate[0])
        # Annotations go into the next position on gutter A or B
        # based on which is more empty, and which is closer
        else:
            if (gutter_a.index - gutter_b.index) > 1:
                annotation.draw(gutter_b)
            elif (gutter_b.index - gutter_a.index) > 1:
                annotation.draw(gutter_a)
            else:
                closest = closest_gutter(annotation, gutter_a, gutter_b)
                annotation.draw(closest)

        completed.append(annotation)


def find_board_image() -> inkex.Image:
    '''Return the board image in the main SVG'''
    # Try the one with id 'board'
    board_image = INKSCAPE_SVG.getElementById('board')
    if board_image is not None and board_image.tag_name == 'image':
        return board_image

    # Find the biggest image
    size = 0.0
    for image in INKSCAPE_SVG.xpath("//svg:image"):
        image_size = (float(image.get('width')) *
                      float(image.get('height')))
        if image_size > size:
            size = image_size
            board_image = image

    if board_image is not None:
        return board_image

    raise RuntimeError(
        "Could not find a board image in the SVG."
        "Check there is at least one image."
        "The board image can be designated by assigning it id 'board'.")


class AnnotateColors:
    """Color palettes for giving each annotation a unique color"""
    # These are SVG named colors
    # I removed things near whites, greys, blacks, browns, and beiges
    # default was rearranged to try to produce non-consecutive shades
    # dark, light, and medium are just alphabetical.
    default = ['maroon', 'goldenrod', 'blue', 'coral', 'orchid', 'limegreen',
               'indigo', 'red', 'gold', 'royalblue', 'salmon', 'plum',
               'yellowgreen', 'midnightblue', 'firebrick', 'yellow',
               'steelblue', 'orange', 'blueviolet', 'green', 'purple',
               'orangered', 'chartreuse', 'cornflowerblue', 'indianred',
               'violet', 'magenta', 'seagreen', 'crimson', 'greenyellow',
               'cyan', 'deeppink', 'slateblue', 'hotpink', 'springgreen',
               'tomato', 'lawngreen', 'dodgerblue', 'pink', 'lime',
               'deepskyblue', 'olivedrab', 'cadetblue', 'navy', 'darkorange',
               'turquoise', 'teal', 'skyblue', 'forestgreen']
    dark = ['darkblue', 'darkcyan', 'darkgoldenrod', 'darkgreen',
            'darkmagenta', 'darkorange', 'darkorchid', 'darkred', 'darksalmon',
            'darkseagreen', 'darkslateblue', 'darkturquoise', 'darkviolet']
    light = ['lightblue', 'lightcoral', 'lightgreen', 'lightpink',
             'lightsalmon', 'lightseagreen', 'lightskyblue', 'lightsteelblue',
             'palegreen', 'paleturquoise', 'palevioletred']
    medium = ['mediumaquamarine', 'mediumblue', 'mediumorchid', 'mediumpurple',
              'mediumseagreen', 'mediumslateblue', 'mediumspringgreen',
              'mediumturquoise', 'mediumvioletred']

    def __init__(self) -> None:
        """Read the palette configuration
        and set up generator for returning colors"""
        AnnotateColors.validate_colors(YAML_CONFIG)

        if 'palette' in YAML_CONFIG:
            palette = YAML_CONFIG['palette']
        else:
            palette = 'default'

        match palette:
            case 'default':
                self.iterator = self.color_gen(self.default)
            case 'light':
                self.iterator = self.color_gen(self.light)
            case 'dark':
                self.iterator = self.color_gen(self.dark)
            case 'medium':
                self.iterator = self.color_gen(self.medium)
            case 'all':
                self.iterator = self.color_gen(self.default + self.dark +
                                               self.light + self.medium)
            # NOTE: random still repeats after all the colors have been used
            case 'all_random':
                full_list = self.default + self.dark + self.light + self.medium
                random.shuffle(full_list)
                self.iterator = self.color_gen(full_list)
            case 'custom' | 'custom_random':
                colors = YAML_CONFIG['colors']
                for color in colors:
                    inkex.Color(color)

                if palette == 'custom_random':
                    random.shuffle(colors)

                self.iterator = self.color_gen(colors)

    def next(self) -> str:
        """Get the next color to be used"""
        return next(self.iterator)

    def color_gen(self,
                  colors: List[str]) -> Generator[str, None, None]:
        """Generator that returns infinite colors (sequence repeats)"""
        while True:
            yield from colors

    @staticmethod
    def validate_colors(config: Dict[str, Any]) -> None:
        """Check for valid color config"""
        if 'palette' in config:
            palette = config['palette']
        else:
            return  # no palette is a valid config

        match palette:
            case ('default' | 'light' | 'dark' | 'medium' | 'all' |
                  'all_random'):
                pass
            case 'custom' | 'custom_random':
                if 'colors' in config:
                    colors = config['colors']
                    for color in colors:
                        try:
                            inkex.Color(color)
                        except inkex.colors.ColorError as exc:
                            inkex.utils.errormsg(
                                f"Invalid color in 'custom' palette: {color}")
                            raise inkex.utils.AbortExtension from exc
                else:
                    inkex.utils.errormsg(
                        "No color list found for 'custom' palette")
                    raise inkex.utils.AbortExtension
            case _:
                inkex.utils.errormsg(
                    f"Unknown palette in YAML config: {palette}")
                raise inkex.utils.AbortExtension


def closest_gutter(annotation: Annotation, gutter_a: Gutter,
                   gutter_b: Gutter) -> Gutter:
    """Return the closest gutter to the annotation user-drawn rectangle
    according to the next empty space"""
    rect = annotation.rectangle
    rect_transform = inkex.Transform(rect.get('transform'))
    rect_corners = list(
        rect_transform.apply_to_point(x) for x in
        [(rect.left, rect.top), (rect.right, rect.top),
         (rect.left, rect.bottom), (rect.right, rect.bottom)])

    gutter_a_corners = gutter_a.get_approximate_corners()
    gutter_b_corners = gutter_b.get_approximate_corners()

    rect_to_a0 = sorted([math.dist(rect_corners[x], gutter_a_corners[0])
                         for x in [0, 1, 2, 3]])
    rect_to_a1 = sorted([math.dist(rect_corners[x], gutter_a_corners[1])
                         for x in [0, 1, 2, 3]])

    rect_to_b0 = sorted([math.dist(rect_corners[x], gutter_b_corners[0])
                         for x in [0, 1, 2, 3]])
    rect_to_b1 = sorted([math.dist(rect_corners[x], gutter_b_corners[1])
                         for x in [0, 1, 2, 3]])

    if (rect_to_a0[0] + rect_to_a1[0]) <= (rect_to_b0[0] + rect_to_b1[0]):
        return gutter_a

    return gutter_b


def chip_context_image(rect: inkex.Rectangle,
                       selections: Gtk.ListStore) -> GdkPixbuf.Pixbuf:
    """Create an image of the context around a user-drawn rectangle"""
    # TODO this is a bit expensive (rendering the whole svg)
    # currently doing so to scale things
    # but we could just pick some fraction of the svg size (same size contexts)
    # or multiple of the rectangle size (results in different size contexts)
    image = svg_without_selections_as_pixbuf(INKSCAPE_SVG, selections, rect)
    render_width = image.get_width()
    render_height = image.get_height()
    svg_width = INKSCAPE_SVG.viewbox_width
    svg_height = INKSCAPE_SVG.viewbox_height
    # average, because I'm not getting consistent scale factors
    # TODO sort out what the render makes vs what inkscape says
    scale_factor = (render_width/svg_width + render_height/svg_height)/2

    # Rectangle dimensions
    size = 360
    rect_bb = rect.bounding_box()
    context_x = max(0, rect_bb.center_x * scale_factor - ((size)/2))
    context_y = max(0, rect_bb.center_y * scale_factor - ((size)/2))

    # Don't go past the render boundary
    context_width = size
    if (context_x + size) > render_width:
        context_x = render_width - size

    context_height = size
    if (context_y + size) > render_height:
        context_y = render_height - size

    # NOTE: images may include the inkscape page area which may be transparent
    #       Gtk will render it transparent.
    #       It looks odd, but provides context that we're beyond the image edge
    return image.new_subpixbuf(
        context_x, context_y, context_width, context_height)


def rect_icon_image(rect: inkex.Rectangle,
                    base_image: GdkPixbuf.Pixbuf) -> GdkPixbuf.Pixbuf:
    """Return an icon image of the rectangle"""
    render_width = base_image.get_width()
    render_height = base_image.get_height()
    svg_width = INKSCAPE_SVG.viewbox_width
    svg_height = INKSCAPE_SVG.viewbox_height

    scale_factor = (render_width/svg_width + render_height/svg_height)/2

    rect_bb = rect.bounding_box()
    new_width = 2 * round(rect_bb.width * scale_factor * 1.4 / 2)
    new_height = 2 * round(rect_bb.height * scale_factor * 1.4 / 2)
    # make it square
    if new_width > new_height:
        new_height = new_width
    else:
        new_width = new_height
    new_x = rect_bb.center_x * scale_factor - (new_width / 2)
    new_y = rect_bb.center_y * scale_factor - (new_height / 2)

    crop_image = base_image.new_subpixbuf(new_x, new_y, new_width, new_height)
    return crop_image.scale_simple(64, 64, GdkPixbuf.InterpType.BILINEAR)


def svg_without_selections_as_pixbuf(
    svg: inkex.SvgDocumentElement, selections: Gtk.ListStore,
        keep_rect: inkex.Rectangle = None) -> GdkPixbuf.Pixbuf:
    """Return a pixbuf render of the svg
    with keep_rect visible as a red unfilled rectangle,
    and all other selections removed"""
    # copy SVG, remove selections, except keep_rect
    temp_svg = copy.deepcopy(svg)
    for rect in selections:
        if rect is keep_rect:
            # NOTE: rect is still the inkscape copy, don't modify it
            new_rect = temp_svg.getElementById(rect.get("id"))
            new_rect.style['fill'] = 'none'
            new_rect.style['stroke'] = 'red'
        else:
            new_rect = temp_svg.getElementById(rect.get("id"))
            new_rect.getparent().remove(new_rect)

    # Render it as a pixbuf
    stream = Gio.MemoryInputStream.new_from_bytes(
        GLib.Bytes.new(temp_svg.tostring()))
    return GdkPixbuf.Pixbuf.new_from_stream(stream, None)


class BoardAnnotateImage(inkex.Rectangle):
    'A simple image, just enough for positioning, and embedding file contents'
    tag_name = 'image'

    def embed_image(self, file_path: str) -> None:
        """base64 encode the image and place it in an svg element"""
        # Borrowed from Inkscape 1.5 inkex.elements._image
        # Copyright (c) 2020 Martin Owens
        with open(file_path, "rb") as handle:
            file_type = BoardAnnotateImage.get_image_type(
                file_path, handle.read(10))
            handle.seek(0)
            if file_type:
                self.set(
                    "xlink:href",
                    f"data:{file_type};"
                    "base64,"
                    f"{base64.encodebytes(handle.read()).decode('ascii')}")
            else:
                raise ValueError(
                    f"{file_path} is not of type image/png, image/jpeg, "
                    "image/bmp, image/gif, image/tiff, or image/x-icon")

    @staticmethod
    def get_image_type(path: str, header: bytes) -> Optional[str]:
        """Basic magic header checker, returns mime type"""
        # Borrowed from inkscape extension image_embed.py
        # Copyright (c) 2005,2007 Aaron Spike
        for head, mime in (
            (b"\x89PNG", "image/png"),
            (b"\xff\xd8", "image/jpeg"),
            (b"BM", "image/bmp"),
            (b"GIF87a", "image/gif"),
            (b"GIF89a", "image/gif"),
            (b"MM\x00\x2a", "image/tiff"),
            (b"II\x2a\x00", "image/tiff"),
        ):
            if header.startswith(head):
                return mime

        # ico files lack any magic... therefore we check the filename instead
        for ext, mime in (
            # official IANA registered MIME is 'image/vnd.microsoft.icon' tho
            (".ico", "image/x-icon"),
            (".svg", "image/svg+xml"),
        ):
            if path.endswith(ext):
                return mime
        return None


def _debug_print(*args: List[str]) -> None:
    'Implement print in terms of inkex.utils.debug.'
    inkex.utils.debug(' '.join([str(a) for a in args]))


if __name__ == '__main__':
    BoardAnnotateExtension().run()
