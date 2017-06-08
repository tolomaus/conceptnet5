import pathlib
import math
import json
import os
import numpy as np
import pandas as pd
from operator import itemgetter

from conceptnet5.vectors.formats import load_hdf, save_hdf
from conceptnet5.nodes import get_uri_language

TAU = 2 * math.pi
TILE_LANGUAGES = ['mul', 'en', 'fr', 'de', 'es', 'pt', 'it', 'ru', 'ja', 'nl']
DEGREE_SCALE = {
    'mul': 1,
    'en': 1,
    'fr': 1,
    'es': 5,
    'de': 3,
    'ja': 8,
    'pt': 8,
    'it': 8,
    'nl': 12,
    'ru': 16,
    'zh': 1,
}


def get_concept_degrees(filename):
    """
    Load a file containing concept URIs and their degrees in ConceptNet.
    We use this as the measure of the importance of a concept.
    """
    concept_degrees = {}
    with open(filename) as infile:
        for line in infile:
            line = line.strip()
            if line:
                numstr, uri = line.split(' ', 1)
                count = int(numstr)
                if count == 1:
                    break
                concept_degrees[uri] = count

    # Hack to suppress a term that's overrepresented and confusing
    # (its vector makes it mostly mean an article of clothing, but it appears
    # as the dialectal context of many Wiktionary words)
    concept_degrees['/c/en/jersey'] /= 10
    return concept_degrees


def compute_tsne(input_filename, degree_filename, output_filename):
    """
    Use Barnes-Hut t-SNE to build a 2-dimensional projection of the
    similarities in ConceptNet. (This takes several hours.)
    """
    from tsne import bh_sne
    concept_degrees = get_concept_degrees(degree_filename)
    frame = load_hdf(input_filename)

    vocab = [
        term for term in frame.index
        if concept_degrees.get(term, 0) >= 10
        and get_uri_language(term) in TILE_LANGUAGES
    ]
    v_frame = frame.loc[vocab].astype(np.float64)
    tsne_coords = bh_sne(v_frame.values, perplexity=30.)
    tsne_frame = pd.DataFrame(tsne_coords, index=v_frame.index)
    save_hdf(tsne_frame, output_filename)


def _language_and_text(uri):
    """
    Given a ConceptNet URI, extract its language code and its text as separate
    components.
    """
    _, _c, lang, word = uri.split('/', 3)
    word = word.replace('_', ' ')
    return lang, word


def global_coordinate(coord):
    """
    Convert a t-SNE coordinate (which, empirically, goes from about -25 to 25
    on each axis) to a global coordinate for our Leaflet map (whose coordinates
    go from about -100 to 100, and are definitely enclosed in the box whose
    coordinates go from -128 to 128).
    """
    return coord * 4


def raster_coordinate(coord):
    """
    Convert a map coordinate (with X and Y values between -128 and 128) to
    a location in a 4096 x 4096 grid of pixels.
    """
    return (global_coordinate(coord) + 128) * 16


def map_to_tile_coordinate(coord, z):
    """
    Convert a map coordinate (with X and Y values between -128 and 128) to a
    Leaflet tile's local coordinate system, at zoom level `z`.

    The size of a tile at zoom level `z` is 2 ** (8 - z) in the map's global
    coordinate system. For example, a tile at zoom level 0 is 256 units on
    each side, and therefore the tile at zoom level 0, row 0, column 0 (0/0/0)
    contains the entire map within it.

    A tile at zoom level 4 is 16 units on each side, so there is a 16x16 grid
    of tiles at that zoom level that form the whole map, with rows and columns
    numbered in the range [-8, 7].

    Each tile contains a local coordinate system in which (0, 0) is the top
    left corner of the tile and (256, 256) is the bottom right corner. The
    frontend will generate an SVG in this coordinate system that acts like a
    256x256 image.

    This function returns a 4-tuple containing the tile's column, the tile's
    row, and the x and y position within that tile.
    """
    x, y = global_coordinate(coord)
    tile_size = 2 ** (8 - z)
    tile_x = int(math.floor(x / tile_size))
    tile_y = int(math.floor(y / tile_size))
    offset_x = x - (tile_x * tile_size)
    offset_y = y - (tile_y * tile_size)
    local_x = offset_x / tile_size * 256
    local_y = offset_y / tile_size * 256
    return (tile_x, tile_y, local_x, local_y)


def render_tsne(tsne_filename, degree_filename, json_out_path, png_out_path,
                render_png=True, depth=8):
    """
    Produces the data that a Web frontend will used to show a t-SNE
    visualization of ConceptNet:

    - A directory hierarchy containing files named {z}/{x}/{y}.json, where
      each file contains the nodes that land in tile (x, y) at zoom level z
      in Leaflet
    - A .png file containing a faint silhouette of the visualization, providing
      shape to the visualization when zoomed out
    """
    import cairocffi as cairo
    tsne_frame = load_hdf(tsne_filename)
    json_out_path = pathlib.Path(json_out_path)
    concept_degrees = get_concept_degrees(degree_filename)

    tiles = {}
    for tile_z in range(depth):
        bound = 1 << tile_z
        for tile_x in range(-bound, bound):
            for tile_y in range(-bound, bound):
                for lang in TILE_LANGUAGES:
                    tiles[lang, tile_z, tile_x, tile_y] = []

    occlusion = {}
    for lang in TILE_LANGUAGES:
        occlusion[lang] = np.zeros((depth, 4096, 4096), np.bool)

    nodes = []
    for i, uri in enumerate(tsne_frame.index):
        coord = tsne_frame.iloc[i, :2]
        lang, label = _language_and_text(uri)
        deg = min(10000, concept_degrees.get(uri, 0)) * DEGREE_SCALE[lang]
        nodes.append((deg, coord, lang, label, uri))

    nodes.sort(key=itemgetter(0), reverse=True)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 4096, 4096)
    ctx = cairo.Context(surface)
    ctx.set_source_rgba(1, 1, 1, 1)
    ctx.paint()

    for deg, coord, lang, label, uri in nodes:
        for z in range(depth):
            tile_size = 2 ** (8 - z)
            if deg >= tile_size:  # no reason these should be comparable, just a convenient cutoff
                tile_x, tile_y, local_x, local_y = map_to_tile_coordinate(coord, z)
                # These numbers just come from a lot of experimentation
                text_size = deg ** .5 * 0.75 * (2 ** (z / 2 - 3)) + 6
                text_size = min(24, text_size)
                for tile_lang in [lang, 'mul']:
                    raster_text_size = text_size * tile_size / 16

                    use_label = False
                    if text_size > 2:
                        cx, cy = raster_coordinate(coord)
                        occlude_top = int(max(0, cy))
                        occlude_left = int(max(0, cx))
                        occlude_bottom = int(math.ceil(occlude_top + raster_text_size * 1.25))
                        occlude_right = int(math.ceil(occlude_left + raster_text_size * len(label)))
                        region = occlusion[tile_lang][z, occlude_top:occlude_bottom, occlude_left:occlude_right]
                        if not region.any():
                            region[:, :] = True
                            use_label = True
                            if z == 3:
                                print(occlude_top, occlude_left, occlude_bottom, occlude_right, lang, label)

                    tile = tiles[tile_lang, z, tile_x, tile_y]
                    point_data = {
                        'x': round(local_x, 2),
                        'y': round(local_y, 2),
                        'gx': round(coord[0], 2),
                        'gy': round(coord[1], 2),
                        'lang': lang,
                        'label': label,
                        'uri': uri,
                        's': round(text_size, 2)
                    }
                    if not use_label:
                        del point_data['label']
                    tile.append(point_data)

        if render_png:
            ctx.new_path()
            ctx.set_source_rgba(.75, .75, .8, 1)
            cx, cy = raster_coordinate(coord)
            ctx.arc(cx, cy, min(deg, 1000) ** .25, 0, TAU)
            ctx.fill()

    if render_png:
        surface.write_to_png(png_out_path)

    print('Writing JSON')
    for key, val in tiles.items():
        language, tile_z, tile_x, tile_y = key
        out_path = json_out_path / language / str(tile_z) / str(tile_x) / ("%s.json" % tile_y)
        os.makedirs(str(out_path.parent), exist_ok=True)
        with open(str(out_path), 'w', encoding='utf-8') as out:
            json.dump(val, out, ensure_ascii=False)
