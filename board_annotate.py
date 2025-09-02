#! /usr/bin/env python

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
import yaml
import base64
import math
import inkex
import random
from inkex.gui import GtkApp, Window
from gi.repository import Gtk, GdkPixbuf, Gio, GLib
from enum import Enum

# Globals
inkscape_svg = None
yaml_file = None
yaml_config = None


class ChipSelection(Gtk.CellRendererCombo):
    def __init__(self, model, selection_items, apply_button, info_label):
        super().__init__()
        self.set_property('editable', True)
        self.set_property('model', model)
        self.set_property('text-column', 0)
        self.set_property('has-entry', False)
        self.connect('edited', self.on_combo_changed,
                     selection_items, apply_button, info_label)

    def on_combo_changed(self, widget, path, text, selection_items,
                         apply_button, info_label):
        selection_items[path][2] = text
        item_iter = selection_items.get_iter_first()
        item_remaining_count = selection_items.iter_n_children()

        while item_iter:
            if selection_items.get_value(item_iter, 2) == "Select a chip":
                apply_button.set_sensitive(False)
            else:
                item_remaining_count -= 1

            item_iter = selection_items.iter_next(item_iter)

        if item_remaining_count == 0:
            info_label.set_text("All images are assigned")
            apply_button.set_sensitive(True)
        else:
            info_label.set_text(str(item_remaining_count) +
                                " images left to assign.")
            info_label.show()


class SelectionWindow(Window):
    name = "board_annotate"

    def __init__(self, widget, *args, **kwargs):
        super().__init__(widget, *args, **kwargs)

        # Chips defined in user provide yaml
        chip_items = self.widget('chip_items')
        for chip in yaml_config['chips']:
            chip_image_path = chip['chip_photo']
            if not os.path.isabs(chip_image_path) and chip_image_path:
                chip_image_path = os.path.join(
                    os.path.dirname(yaml_file), chip['chip_photo'])
            # NOTE chip_items last column is GdkPixbuf for the chip image
            #      maybe a better UI would load the image and display it
            #      Right now it is only loaded to get the image dimensions
            chip_items.append(
                [chip['name'], chip['description'], chip_image_path,
                 None if chip_image_path == "" else
                 GdkPixbuf.Pixbuf.new_from_file(chip_image_path)])

        # User selected rectangles to be match with a chip
        selection_items = self.widget('selection_items')
        for rect in self.gapp.kwargs['selection']:
            context_image = chip_context_image(rect,
                                               self.gapp.kwargs['selection'])
            selection_items.append([context_image, rect.get("id"),
                                    "Select a chip"])

        # Column for chip selection
        apply_button = self.widget('apply_button')
        info_label = self.widget('info_label')
        chip_combo_renderer = ChipSelection(chip_items, selection_items,
                                            apply_button, info_label)
        chip_selection_column = Gtk.TreeViewColumn(
            "Chip (click twice)", chip_combo_renderer, text=2)
        selections_tree_view = self.widget('selections_tree_view')
        selections_tree_view.append_column(chip_selection_column)

        self.widget('close_button').connect('clicked', self.on_close_clicked)
        self.widget('apply_button').connect('clicked', self.on_apply_clicked,
                                            selection_items, chip_items)

        self.window.show_all()
        self.window.connect("destroy", Gtk.main_quit)

    def on_close_clicked(self, button):
        Gtk.main_quit()

    def on_apply_clicked(self, button, selection_items, chip_items):
        AnnotateBoard(selection_items, chip_items)
        Gtk.main_quit()


class SelectionApp(GtkApp):
    glade_dir = os.path.join(os.path.dirname(__file__))
    app_name = "inkscape_board_annotate"
    windows = [SelectionWindow]


class Gutter:
    index = 0
    offset = 0.0

    class Position(Enum):
        ABOVE = 1
        BELOW = 2
        LEFT = 3
        RIGHT = 4

    def __init__(self, position, board_image):
        self.position = position
        self.image_ratio = (yaml_config['image_ratio']
                            if 'image_ratio' in yaml_config
                            else 0.6)
        match position:
            case self.Position.ABOVE:
                self.gutter_size = board_image.top
            case self.Position.BELOW:
                self.gutter_size = (inkscape_svg.viewbox_height -
                                    board_image.bottom)
            case self.Position.LEFT:
                self.gutter_size = board_image.left
            case self.Position.RIGHT:
                self.gutter_size = (inkscape_svg.viewbox_width -
                                    board_image.right)

        stroke_width = inkscape_svg.viewport_to_unit("1mm")
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

    def get_approximate_corners(self):
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

    def get_position_size(self, width, height):
        # Work around annotations without images
        if width == 0:
            width = self.image_display_size
        if height == 0:
            height = self.image_display_size

        stroke_width = inkscape_svg.viewport_to_unit("1mm")
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
                    (inkscape_svg.viewbox_height -
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
                    (inkscape_svg.viewbox_width -
                     self.main_image_edge - stroke_width),
                    (self.image_display_size * (height/width) + stroke_width))

    def get_image_position_size(self, width, height):
        ''' return tuple of x, y, width, height
        where the image should be placed'''
        stroke_width = inkscape_svg.viewport_to_unit("1mm")
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

    def increment(self, width, height):
        self.index += 1
        stroke_width = inkscape_svg.viewport_to_unit("1mm")
        match self.position:
            case self.Position.ABOVE | self.Position.BELOW:
                self.offset += width + stroke_width
            case self.Position.LEFT | self.Position.RIGHT:
                self.offset += height + stroke_width


class Annotation(inkex.Layer):
    ''' Annotation as an inkscape layer '''
    def __init__(self, rectangle, name, description, image_path, gdkpixbuf,
                 color):
        self.rectangle = rectangle
        self.name = name
        self.description = description
        self.image_path = image_path
        self.gdkpixbuf = gdkpixbuf
        self.color = inkex.Color(color)

        if self.gdkpixbuf is not None:
            self.image_width = gdkpixbuf.get_width()
            self.image_height = gdkpixbuf.get_height()
        else:
            self.image_width, self.image_height = 0.0, 0.0

        super().__init__()

        self.set("inkscape:label", self.name)
        self.set("inkscape:highlight-color", self.color)

    def draw_existing(self, duplicate):
        self.color = duplicate.color
        self.set("inkscape:label", self.name)
        self.set("inkscape:highlight-color", self.color)

        self.draw_connector(duplicate.gutter, duplicate)
        self.update_rectangle_style(duplicate)
        # gutter remains in the same state, no increment

    def draw(self, gutter):
        # save gutter in case a duplicate needs it
        self.gutter = gutter

        inkscape_svg.add(self)  # Add the Annotation layer

        self.draw_image(gutter)
        self.draw_surround(gutter)
        self.draw_text(gutter)
        self.draw_connector(gutter)
        self.update_rectangle_style()

        # Increment the gutter after everything is drawn
        bb = self.surround.bounding_box()
        gutter.increment(bb.width, bb.height)

    def draw_image(self, gutter):
        if self.image_path and self.gdkpixbuf:
            position_size = gutter.get_image_position_size(self.image_width,
                                                           self.image_height)
            # TODO inkscape 1.5 adds inkex.Image
            #      After that you can remove BA_Image and get_image_type
            self.svg_image = BA_Image.new(*position_size)
            self.svg_image.embed_image(self.image_path)
            self.add(self.svg_image)
            self.svg_image.label = "chip image"

    def draw_surround(self, gutter):
        position_size = gutter.get_position_size(self.image_width,
                                                 self.image_height)
        stroke_width = inkscape_svg.viewport_to_unit("1mm")
        self.surround = inkex.Rectangle.new(*position_size)
        self.surround.style['stroke-width'] = stroke_width
        self.surround.style.set_color(self.color, 'stroke')
        self.surround.style.set_color(inkex.Color('none'), 'fill')
        # Prevent connectors being drawn through adjacent annotations
        # TODO would like to avoid them passing through the gutter at all
        self.surround.set("inkscape:connector-avoid", 'true')
        self.add(self.surround)
        self.surround.label = "surround"

    def draw_text(self, gutter):
        # TODO try to fit title_box to the title, then give rest to desc_box
        # TODO better fonts
        # TODO better way to set font size based on box dimension
        surround_bb = self.surround.bounding_box()
        title_box = None
        text_ratio = 1 - gutter.image_ratio
        stroke_width = inkscape_svg.viewport_to_unit("1mm")
        half_stroke_width = 0.5 * stroke_width
        above_half_height = 0.5 * (self.svg_image.top -
                                   (surround_bb.top +
                                    half_stroke_width))
        below_half_height = 0.5 * ((surround_bb.bottom -
                                    half_stroke_width) -
                                   self.svg_image.bottom)
        vertical_half_height = 0.5 * (surround_bb.height - stroke_width)
        match gutter.position:
            case gutter.Position.ABOVE:
                title_box_xywh = (
                    surround_bb.left + half_stroke_width,
                    surround_bb.top + half_stroke_width,
                    surround_bb.width - stroke_width,
                    above_half_height)
            case gutter.Position.BELOW:
                title_box_xywh = (
                    surround_bb.left + half_stroke_width,
                    self.svg_image.bottom,
                    surround_bb.width - stroke_width,
                    below_half_height)
            case gutter.Position.LEFT:
                title_box_xywh = (
                    surround_bb.left + half_stroke_width,
                    surround_bb.top + half_stroke_width,
                    surround_bb.width * text_ratio - half_stroke_width,
                    vertical_half_height)
            case gutter.Position.RIGHT:
                title_box_xywh = (
                    self.svg_image.right,
                    surround_bb.top + half_stroke_width,
                    surround_bb.width * text_ratio - half_stroke_width,
                    vertical_half_height)

        # slightly inset the title box
        title_box = inkex.Rectangle.new(*title_box_xywh)
        title_box.style.set_color(inkex.Color('none'), 'stroke')
        title_box.style.set_color(inkex.Color('none'), 'fill')

        title = inkex.TextElement()
        title_text = inkex.Tspan()
        title.append(title_text)
        title_text.text = self.name
        title.style['font-size'] = inkscape_svg.viewport_to_unit("10pt")
        title.style['text-anchor'] = "middle"
        self.add(title_box)
        title_box.label = "title shape"
        title.style['shape-inside'] = title_box.get_id(as_url=2)
        self.add(title)
        title.label = "title text"

        desc_box = None
        match gutter.position:
            case gutter.Position.ABOVE:
                desc_box_xywh = (
                    surround_bb.left + half_stroke_width,
                    (surround_bb.top + above_half_height +
                     half_stroke_width),
                    surround_bb.width - stroke_width,
                    above_half_height)
            case gutter.Position.BELOW:
                desc_box_xywh = (
                    surround_bb.left + half_stroke_width,
                    (surround_bb.bottom - below_half_height -
                     half_stroke_width),
                    surround_bb.width - stroke_width,
                    below_half_height)
            case gutter.Position.LEFT:
                desc_box_xywh = (
                    surround_bb.left + half_stroke_width,
                    (surround_bb.top + vertical_half_height +
                     half_stroke_width),
                    surround_bb.width * text_ratio - half_stroke_width,
                    vertical_half_height)
            case gutter.Position.RIGHT:
                desc_box_xywh = (
                    surround_bb.right - (surround_bb.width * text_ratio),
                    (surround_bb.top + vertical_half_height +
                     half_stroke_width),
                    surround_bb.width * text_ratio - half_stroke_width,
                    vertical_half_height)

        # slightly inset the description box
        desc_box = inkex.Rectangle.new(*desc_box_xywh)
        desc_box.style.set_color(inkex.Color('none'), 'stroke')
        desc_box.style.set_color(inkex.Color('none'), 'fill')

        desc = inkex.TextElement()
        desc_text = inkex.Tspan()
        desc.append(desc_text)
        desc_text.text = self.description
        desc.style['font-size'] = inkscape_svg.viewport_to_unit("8pt")
        desc.style['line-height'] = "1"
        desc.style['text-anchor'] = "start"
        self.add(desc_box)
        desc_box.label = "description shape"
        desc.style['shape-inside'] = desc_box.get_id(as_url=2)
        self.add(desc)
        desc.label = "description text"

    def draw_connector(self, gutter, duplicate=None):
        ''' connect the surrounding rect and the old rect with a path '''
        path = inkex.PathElement()
        path.style['stroke-width'] = inkscape_svg.viewport_to_unit("1mm")
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

    def update_rectangle_style(self, duplicate=None):
        self.rectangle.style['stroke-width'] = (
            inkscape_svg.viewport_to_unit("1mm"))
        self.rectangle.style.set_color(self.color, 'stroke')
        self.rectangle.style.set_color(inkex.Color("none"), 'fill')

        # Originally I grouped the user drawn rectangles with everything,
        # but that makes it harder to re-position the annotation, so don't
        # Just label it to make the association clear
        self.rectangle.label = "chip_location " + self.name


def AnnotateBoard(selection_items, chip_items):
    # Board image is the one with id 'board', or the biggest one
    board_image = inkscape_svg.getElementById('board')
    if board_image is None:
        size = 0
        for image in inkscape_svg.xpath("//svg:image"):
            image_size = (float(image.get('width')) *
                          float(image.get('height')))
            if image_size > size:
                size = image_size
                board_image = image

    gutter_a, gutter_b = None, None
    if yaml_config['gutter'] == 'horizontal':
        gutter_a = Gutter(Gutter.Position.ABOVE, board_image)
        gutter_b = Gutter(Gutter.Position.BELOW, board_image)
    elif yaml_config['gutter'] == 'vertical':
        gutter_a = Gutter(Gutter.Position.LEFT, board_image)
        gutter_b = Gutter(Gutter.Position.RIGHT, board_image)

    colors = AnnotateColors()

    iter_selection = selection_items.get_iter_first()
    completed = []
    while iter_selection:
        # Create annotation
        chip_name = selection_items.get_value(iter_selection, 2)
        iter_chip = chip_items.get_iter_first()
        while chip_items.get_value(iter_chip, 0) != chip_name:
            iter_chip = chip_items.iter_next(iter_chip)
        annotation = Annotation(
            rectangle=inkscape_svg.getElementById(
                selection_items.get_value(iter_selection, 1)),
            name=chip_name,
            description=chip_items.get_value(iter_chip, 1),
            image_path=chip_items.get_value(iter_chip, 2),
            gdkpixbuf=chip_items.get_value(iter_chip, 3),
            color=colors.next())

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

        duplicate = [anno for anno in completed if anno.name == chip_name]
        if duplicate:
            # Already drew an annotation for this chip
            # connect to it instead of drawing again
            annotation.draw_existing(duplicate[0])
        # Annotations go into the next position on gutter A or B
        # based on which is more empty, and which is closer
        elif (gutter_a.index - gutter_b.index) > 1:
            annotation.draw(gutter_b)
        elif (gutter_b.index - gutter_a.index) > 1:
            annotation.draw(gutter_a)
        else:
            closest = closest_gutter(annotation, gutter_a, gutter_b)
            annotation.draw(closest)

        completed.append(annotation)

        iter_selection = selection_items.iter_next(iter_selection)


class AnnotateColors:
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

    def __init__(self):
        if 'palette' in yaml_config:
            palette = yaml_config['palette']
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
            case 'random':
                full_list = self.default + self.dark + self.light + self.medium
                random.shuffle(full_list)
                self.iterator = self.color_gen(full_list)
            case 'custom':
                if 'colors' in yaml_config:
                    colors = yaml_config['colors']
                    for color in colors:
                        try:
                            inkex.Color(color)
                        except inkex.colors.ColorError:
                            inkex.utils.errormsg(
                                f"Invalid color in 'custom' palette: {color}")
                            raise inkex.utils.AbortExtension

                    self.iterator = self.color_gen(colors)
                else:
                    inkex.utils.errormsg(
                        "No color list found for 'custom' palette")
                    raise inkex.utils.AbortExtension
            case _:
                inkex.utils.errormsg(
                    f"Unknown palette in YAML config: {palette}")
                raise inkex.utils.AbortExtension

    def next(self):
        return next(self.iterator)

    def color_gen(self, colors):
        while True:
            yield from colors


def closest_gutter(annotation, gutter_a, gutter_b):
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
    else:
        return gutter_b


def chip_context_image(rect, selections):
    image = svg_without_selections_as_pixbuf(inkscape_svg, selections, rect)
    render_width = image.get_width()
    render_height = image.get_height()
    svg_width = inkscape_svg.viewbox_width
    svg_height = inkscape_svg.viewbox_height
    # average, because I'm not getting consistent scale factors
    # TODO sort out what the render makes vs what inkscape says
    scale_factor = (render_width/svg_width + render_height/svg_height)/2

    # Rectangle dimensions
    size = 400
    bb = rect.bounding_box()
    context_x = max(0, bb.center_x * scale_factor - ((size)/2))
    context_y = max(0, bb.center_y * scale_factor - ((size)/2))

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


def svg_without_selections_as_pixbuf(svg, selections, keep_rect=None):
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


class BA_Image(inkex.Rectangle):
    'A simple image, just enough for positioning, and embedding file contents'
    tag_name = 'image'

    def embed_image(self, file_path):
        # Borrowed from Inkscape 1.5 inkex.elements._image
        # Copyright (c) 2020 Martin Owens
        with open(file_path, "rb") as handle:
            file_type = get_image_type(file_path, handle.read(10))
            handle.seek(0)
            if file_type:
                self.set(
                    "xlink:href",
                    "data:{};base64,{}".format(
                        file_type,
                        base64.encodebytes(
                            handle.read()).decode("ascii")
                    ),
                )
            else:
                raise ValueError(
                    f"{file_path} is not of type image/png, image/jpeg, "
                    "image/bmp, image/gif, image/tiff, or image/x-icon")


def get_image_type(path, header):
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


def _debug_print(*args):
    'Implement print in terms of inkex.utils.debug.'
    inkex.utils.debug(' '.join([str(a) for a in args]))


def sort_check_selection(selection, gutter):
    '''Sort selection rectangles left to right or top to bottom
    also checks for invalid selections, and a valid gutter setting'''
    for item in inkscape_svg.selection:
        if str(item) != 'rect':
            item_id = item.get_id()
            inkex.utils.errormsg(
                f"Invalid items selected\n"
                f"Found '{str(item)}':'{item_id}' in selection\n"
                f"Board annotate only works on rectangles")
            raise inkex.utils.AbortExtension
    if gutter == "horizontal":
        return sorted(selection,
                      key=lambda e: float(e.bounding_box().center_x))
    elif gutter == "vertical":
        return sorted(selection,
                      key=lambda e: float(e.bounding_box().center_y))
    else:
        raise ValueError("YAML config gutter is not "
                         "'horizontal' or 'vertical' ")


class BoardAnnotateExtension(inkex.EffectExtension):
    'Annotate a circuit board with images'
    def __init__(self, svg=None):
        super().__init__()

    def add_arguments(self, pars):
        pars.add_argument('--yaml-file', type=str,
                          help='Board YAML configuration')
        pars.add_argument('--tab', dest='tab',
                          help='The selected UI tab when Apply was pressed')

    def effect(self):
        global yaml_file
        global yaml_config
        global inkscape_svg

        yaml_file = self.options.yaml_file
        yaml_config = None
        inkscape_svg = self.svg

        if (yaml_file is not None and not os.path.isdir(yaml_file)):
            # see https://gitlab.com/inkscape/inkscape/-/issues/2822
            # for why isdir check is used
            with open(yaml_file, 'r') as file:
                yaml_config = yaml.safe_load(file)

        # validate color settings in the yaml config
        # (so user gets warned before trying to match chips)
        temp = AnnotateColors()
        temp.next()  # so linter doesn't warn about unused variable
        del temp
        try:
            sorted_selection = sort_check_selection(
                inkscape_svg.selection, yaml_config['gutter'])
        except ValueError as ve:
            inkex.utils.errormsg(ve)
            raise inkex.utils.AbortExtension
        # TODO probably could do some more early checks

        SelectionApp(start_loop=True,
                     selection=sorted_selection)


if __name__ == '__main__':
    BoardAnnotateExtension().run()
