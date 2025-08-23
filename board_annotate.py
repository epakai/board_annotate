#! /usr/bin/env python

import os
import copy
import yaml
import inkex
from inkex.gui import GtkApp, Window
from gi.repository import Gtk, GdkPixbuf, Gio, GLib

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
            info_label.set_text("There are " + str(item_remaining_count) +
                                " images left to assign.")
            info_label.show()


class SelectionWindow(Window):
    name = "board_annotate"

    def __init__(self, widget, *args, **kwargs):
        super().__init__(widget, *args, **kwargs)

        # Chips defined in user provide yaml
        chip_items = self.widget('chip_items')
        for chip in yaml_config['chips']:
            chip_items.append([chip['name'], chip['description'], None])

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
                                            selection_items)

        self.window.show_all()
        self.window.connect("destroy", Gtk.main_quit)

    def on_close_clicked(self, button):
        Gtk.main_quit()

    def on_apply_clicked(self, button, selection_items):
        item_iter = selection_items.get_iter_first()
        while item_iter:
            _debug_print(selection_items.get_value(item_iter, 1),
                         selection_items.get_value(item_iter, 2))
            # make a new layer
            #   layers are just groups with layer attributes
            #   see https://github.com/dmitry-t/inkscape-export-layers/
            #   I didn't see an API for them, look again
            # pick a color
            # move the rect to new layer, style it
            # find left/upper-most free space above the board image
            #   PROBLEM: we don't know where the board image is
            #            is there a way to find empty space in an SVG?
            # insert the image (optional)
            #   has to be loaded from yaml path and chip photo variable
            # write the name
            # write the description
            # draw a rect around them
            # connect the rect and the old rect
            item_iter = selection_items.iter_next(item_iter)


class SelectionApp(GtkApp):
    glade_dir = os.path.join(os.path.dirname(__file__))
    app_name = "inkscape_board_annotate"
    windows = [SelectionWindow]


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
    width, height, x, y = [float(x) for x in
                           [rect.get("width"), rect.get("height"),
                            rect.get("x"), rect.get("y")]]
    size = 400
    context_x = max(0, x * scale_factor - ((size - (width * scale_factor))/2))
    context_y = max(0, y * scale_factor - ((size - (height * scale_factor))/2))

    # Don't go past the render boundary
    context_width = size
    if (context_x + size) > render_width:
        context_x = render_width - size

    context_height = size
    if (context_y + size) > render_height:
        context_y = render_height - size

    # NOTE: images may include the inkscape page area which may be transparent
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


def _debug_print(*args):
    'Implement print in terms of inkex.utils.debug.'
    inkex.utils.debug(' '.join([str(a) for a in args]))


def sort_selection(selection, orientation):
    'Sort selection rectangles left to right or top to bottom'
    if orientation == "landscape":
        return sorted(selection, key=lambda e: float(e.get("x")))
    elif orientation == "portrait":
        return sorted(selection, key=lambda e: float(e.get("y")))


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
            with open(yaml_file, 'r') as file:
                yaml_config = yaml.safe_load(file)
        sorted_selection = sort_selection(inkscape_svg.selection,
                                          yaml_config['orientation'])
        SelectionApp(start_loop=True,
                     selection=sorted_selection)


if __name__ == '__main__':
    BoardAnnotateExtension().run()
