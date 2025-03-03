import os
from datetime import datetime

import pcbnew

from .common import EcadParser, Component
from .kicad_extra import find_latest_schematic_data, parse_schematic_data
from .svgpath import create_path
from ..core import ibom
from ..core.config import Config
from ..core.fontparser import FontParser


class PcbnewParser(EcadParser):

    def __init__(self, file_name, config, logger, board=None):
        super(PcbnewParser, self).__init__(file_name, config, logger)
        self.board = board
        if self.board is None:
            self.board = pcbnew.LoadBoard(self.file_name)  # type: pcbnew.BOARD
        if hasattr(self.board, 'GetModules'):
            self.footprints = list(self.board.GetModules())
        else:
            self.footprints = list(self.board.GetFootprints())
        self.font_parser = FontParser()
        self.extra_data_func = parse_schematic_data

    def latest_extra_data(self, extra_dirs=None):
        base_name = os.path.splitext(os.path.basename(self.file_name))[0]
        extra_dirs.append(self.board.GetPlotOptions().GetOutputDirectory())
        file_dir_name = os.path.dirname(self.file_name)
        directories = [
            file_dir_name,
        ]
        for dir in extra_dirs:
            if not os.path.isabs(dir):
                dir = os.path.join(file_dir_name, dir)
            if os.path.exists(dir):
                directories.append(dir)
        return find_latest_schematic_data(base_name, directories)

    @staticmethod
    def normalize(point):
        return [point[0] * 1e-6, point[1] * 1e-6]

    def parse_shape(self, d):
        # type: (pcbnew.PCB_SHAPE) -> dict or None
        shape = {
            pcbnew.S_SEGMENT: "segment",
            pcbnew.S_CIRCLE: "circle",
            pcbnew.S_ARC: "arc",
            pcbnew.S_POLYGON: "polygon",
            pcbnew.S_CURVE: "curve",
            pcbnew.S_RECT: "rect",
        }.get(d.GetShape(), "")
        if shape == "":
            self.logger.info("Unsupported shape %s, skipping", d.GetShape())
            return None
        start = self.normalize(d.GetStart())
        end = self.normalize(d.GetEnd())
        if shape in ["segment", "rect"]:
            return {
                "type": shape,
                "start": start,
                "end": end,
                "width": d.GetWidth() * 1e-6
            }
        if shape == "circle":
            return {
                "type": shape,
                "start": start,
                "radius": d.GetRadius() * 1e-6,
                "width": d.GetWidth() * 1e-6
            }
        if shape == "arc":
            a1 = round(d.GetArcAngleStart() * 0.1, 2)
            a2 = round((d.GetArcAngleStart() + d.GetAngle()) * 0.1, 2)
            if d.GetAngle() < 0:
                (a1, a2) = (a2, a1)
            return {
                "type": shape,
                "start": start,
                "radius": d.GetRadius() * 1e-6,
                "startangle": a1,
                "endangle": a2,
                "width": d.GetWidth() * 1e-6
            }
        if shape == "polygon":
            if hasattr(d, "GetPolyShape"):
                polygons = self.parse_poly_set(d.GetPolyShape())
            else:
                self.logger.info(
                    "Polygons not supported for KiCad 4, skipping")
                return None
            angle = 0
            if hasattr(d, 'GetParentModule'):
                parent_footprint = d.GetParentModule()
            else:
                parent_footprint = d.GetParentFootprint()
            if parent_footprint is not None:
                angle = parent_footprint.GetOrientation() * 0.1,
            return {
                "type": shape,
                "pos": start,
                "angle": angle,
                "polygons": polygons
            }
        if shape == "curve":
            return {
                "type": shape,
                "start": start,
                "cpa": self.normalize(d.GetBezControl1()),
                "cpb": self.normalize(d.GetBezControl2()),
                "end": end,
                "width": d.GetWidth() * 1e-6
            }

    def parse_poly_set(self, polygon_set):
        result = []
        for polygon_index in range(polygon_set.OutlineCount()):
            outline = polygon_set.Outline(polygon_index)
            if not hasattr(outline, "PointCount"):
                self.logger.warn("No PointCount method on outline object. "
                                 "Unpatched kicad version?")
                return result
            parsed_outline = []
            for point_index in range(outline.PointCount()):
                point = outline.CPoint(point_index)
                parsed_outline.append(self.normalize([point.x, point.y]))
            result.append(parsed_outline)

        return result

    def parse_text(self, d):
        pos = self.normalize(d.GetPosition())
        if not d.IsVisible():
            return None
        if hasattr(d, "GetTextThickness"):
            thickness = d.GetTextThickness() * 1e-6
        else:
            thickness = d.GetThickness() * 1e-6
        if hasattr(d, 'TransformToSegmentList'):
            segments = [self.normalize(p) for p in d.TransformToSegmentList()]
            lines = []
            for i in range(0, len(segments), 2):
                if i == 0 or segments[i-1] != segments[i]:
                    lines.append([segments[i]])
                lines[-1].append(segments[i+1])
            return {
                "thickness": thickness,
                "svgpath": create_path(lines)
            }
        if d.GetClass() == "MTEXT":
            angle = d.GetDrawRotation() * 0.1
        else:
            if hasattr(d, "GetTextAngle"):
                angle = d.GetTextAngle() * 0.1
            else:
                angle = d.GetOrientation() * 0.1
        if hasattr(d, "GetTextHeight"):
            height = d.GetTextHeight() * 1e-6
            width = d.GetTextWidth() * 1e-6
        else:
            height = d.GetHeight() * 1e-6
            width = d.GetWidth() * 1e-6
        if hasattr(d, "GetShownText"):
            text = d.GetShownText()
        else:
            text = d.GetText()
        self.font_parser.parse_font_for_string(text)
        attributes = []
        if d.IsMirrored():
            attributes.append("mirrored")
        if d.IsItalic():
            attributes.append("italic")
        if d.IsBold():
            attributes.append("bold")

        return {
            "pos": pos,
            "text": text,
            "height": height,
            "width": width,
            "justify": [d.GetHorizJustify(), d.GetVertJustify()],
            "thickness": thickness,
            "attr": attributes,
            "angle": angle
        }

    def parse_drawing(self, d):
        if d.GetClass() in ["DRAWSEGMENT", "MGRAPHIC", "PCB_SHAPE"]:
            return self.parse_shape(d)
        elif d.GetClass() in ["PTEXT", "MTEXT"]:
            return self.parse_text(d)
        else:
            self.logger.info("Unsupported drawing class %s, skipping",
                             d.GetClass())
            return None

    def parse_edges(self, pcb):
        edges = []
        drawings = list(pcb.GetDrawings())
        bbox = None
        for f in self.footprints:
            for g in f.GraphicalItems():
                drawings.append(g)
        for d in drawings:
            if d.GetLayer() == pcbnew.Edge_Cuts:
                parsed_drawing = self.parse_drawing(d)
                if parsed_drawing:
                    edges.append(parsed_drawing)
                    if bbox is None:
                        bbox = d.GetBoundingBox()
                    else:
                        bbox.Merge(d.GetBoundingBox())
        if bbox:
            bbox.Normalize()
        return edges, bbox

    def parse_drawings_on_layers(self, drawings, f_layer, b_layer):
        front = []
        back = []

        for d in drawings:
            if d[1].GetLayer() not in [f_layer, b_layer]:
                continue
            drawing = self.parse_drawing(d[1])
            if not drawing:
                continue
            if d[0] in ["ref", "val"]:
                drawing[d[0]] = 1
            if d[1].GetLayer() == f_layer:
                front.append(drawing)
            else:
                back.append(drawing)

        return {
            "F": front,
            "B": back
        }

    def get_all_drawings(self):
        drawings = [(d.GetClass(), d) for d in list(self.board.GetDrawings())]
        for f in self.footprints:
            drawings.append(("ref", f.Reference()))
            drawings.append(("val", f.Value()))
            for d in f.GraphicalItems():
                drawings.append((d.GetClass(), d))
        return drawings

    def parse_pad(self, pad):
        # type: (pcbnew.PAD) -> dict or None
        layers_set = list(pad.GetLayerSet().Seq())
        layers = []
        if pcbnew.F_Cu in layers_set:
            layers.append("F")
        if pcbnew.B_Cu in layers_set:
            layers.append("B")
        pos = self.normalize(pad.GetPosition())
        size = self.normalize(pad.GetSize())
        angle = pad.GetOrientation() * -0.1
        shape_lookup = {
            pcbnew.PAD_SHAPE_RECT: "rect",
            pcbnew.PAD_SHAPE_OVAL: "oval",
            pcbnew.PAD_SHAPE_CIRCLE: "circle",
        }
        if hasattr(pcbnew, "PAD_SHAPE_ROUNDRECT"):
            shape_lookup[pcbnew.PAD_SHAPE_ROUNDRECT] = "roundrect"
        if hasattr(pcbnew, "PAD_SHAPE_CUSTOM"):
            shape_lookup[pcbnew.PAD_SHAPE_CUSTOM] = "custom"
        if hasattr(pcbnew, "PAD_SHAPE_CHAMFERED_RECT"):
            shape_lookup[pcbnew.PAD_SHAPE_CHAMFERED_RECT] = "chamfrect"
        shape = shape_lookup.get(pad.GetShape(), "")
        if shape == "":
            self.logger.info("Unsupported pad shape %s, skipping.",
                             pad.GetShape())
            return None
        pad_dict = {
            "layers": layers,
            "pos": pos,
            "size": size,
            "angle": angle,
            "shape": shape
        }
        if shape == "custom":
            polygon_set = pad.GetCustomShapeAsPolygon()
            if polygon_set.HasHoles():
                self.logger.warn('Detected holes in custom pad polygons')
            if polygon_set.IsSelfIntersecting():
                self.logger.warn(
                    'Detected self intersecting polygons in custom pad')
            pad_dict["polygons"] = self.parse_poly_set(polygon_set)
        if shape in ["roundrect", "chamfrect"]:
            pad_dict["radius"] = pad.GetRoundRectCornerRadius() * 1e-6
        if shape == "chamfrect":
            pad_dict["chamfpos"] = pad.GetChamferPositions()
            pad_dict["chamfratio"] = pad.GetChamferRectRatio()
        if hasattr(pcbnew, 'PAD_ATTRIB_PTH'):
            through_hole_attributes = [pcbnew.PAD_ATTRIB_PTH,
                                       pcbnew.PAD_ATTRIB_NPTH]
        else:
            through_hole_attributes = [pcbnew.PAD_ATTRIB_STANDARD,
                                       pcbnew.PAD_ATTRIB_HOLE_NOT_PLATED]
        if pad.GetAttribute() in through_hole_attributes:
            pad_dict["type"] = "th"
            pad_dict["drillshape"] = {
                pcbnew.PAD_DRILL_SHAPE_CIRCLE: "circle",
                pcbnew.PAD_DRILL_SHAPE_OBLONG: "oblong"
            }.get(pad.GetDrillShape())
            pad_dict["drillsize"] = self.normalize(pad.GetDrillSize())
        else:
            pad_dict["type"] = "smd"
        if hasattr(pad, "GetOffset"):
            pad_dict["offset"] = self.normalize(pad.GetOffset())
        if self.config.include_nets:
            pad_dict["net"] = pad.GetNetname()

        return pad_dict

    def parse_footprints(self):
        # type: () -> list
        footprints = []
        for f in self.footprints:  # type: pcbnew.FOOTPRINT
            ref = f.GetReference()

            # bounding box
            if hasattr(pcbnew, 'MODULE'):
                f_copy = pcbnew.MODULE(f)
            else:
                f_copy = pcbnew.FOOTPRINT(f)
            f_copy.SetOrientation(0)
            f_copy.SetPosition(pcbnew.wxPoint(0, 0))
            if hasattr(f_copy, 'GetFootprintRect'):
                footprint_rect = f_copy.GetFootprintRect()
            else:
                footprint_rect = f_copy.GetBoundingBox(False, False)
            bbox = {
                "pos": self.normalize(f.GetPosition()),
                "relpos": self.normalize(footprint_rect.GetPosition()),
                "size": self.normalize(footprint_rect.GetSize()),
                "angle": f.GetOrientation() * 0.1,
            }

            # graphical drawings
            drawings = []
            for d in f.GraphicalItems():
                # we only care about copper ones, silkscreen is taken care of
                if d.GetLayer() not in [pcbnew.F_Cu, pcbnew.B_Cu]:
                    continue
                drawing = self.parse_drawing(d)
                if not drawing:
                    continue
                drawings.append({
                    "layer": "F" if d.GetLayer() == pcbnew.F_Cu else "B",
                    "drawing": drawing,
                })

            # footprint pads
            pads = []
            for p in f.Pads():
                pad_dict = self.parse_pad(p)
                if pad_dict is not None:
                    pads.append((p.GetPadName(), pad_dict))

            if pads:
                # Try to guess first pin name.
                pads = sorted(pads, key=lambda el: el[0])
                pin1_pads = [p for p in pads if p[0] in
                             ['1', 'A', 'A1', 'P1', 'PAD1']]
                if pin1_pads:
                    pin1_pad_name = pin1_pads[0][0]
                else:
                    # No pads have common first pin name,
                    # pick lexicographically smallest.
                    pin1_pad_name = pads[0][0]
                for pad_name, pad_dict in pads:
                    if pad_name == pin1_pad_name:
                        pad_dict['pin1'] = 1

            pads = [p[1] for p in pads]

            # add footprint
            footprints.append({
                "ref": ref,
                "bbox": bbox,
                "pads": pads,
                "drawings": drawings,
                "layer": {
                    pcbnew.F_Cu: "F",
                    pcbnew.B_Cu: "B"
                }.get(f.GetLayer())
            })

        return footprints

    def parse_tracks(self, tracks):
        result = {pcbnew.F_Cu: [], pcbnew.B_Cu: []}
        for track in tracks:
            if track.GetClass() == "VIA":
                track_dict = {
                    "start": self.normalize(track.GetStart()),
                    "end": self.normalize(track.GetEnd()),
                    "width": track.GetWidth() * 1e-6,
                    "net": track.GetNetname(),
                }
                for layer in [pcbnew.F_Cu, pcbnew.B_Cu]:
                    if track.IsOnLayer(layer):
                        result[layer].append(track_dict)
            else:
                if track.GetLayer() in [pcbnew.F_Cu, pcbnew.B_Cu]:
                    if track.GetClass() == "ARC":
                        a1 = round(track.GetArcAngleStart() * 0.1, 2)
                        a2 = round((track.GetArcAngleStart() +
                                    track.GetAngle()) * 0.1, 2)
                        if track.GetAngle() < 0:
                            (a1, a2) = (a2, a1)
                        track_dict = {
                            "center": self.normalize(track.GetCenter()),
                            "startangle": a1,
                            "endangle": a2,
                            "radius": track.GetRadius() * 1e-6,
                            "width": track.GetWidth() * 1e-6,
                        }
                    else:
                        track_dict = {
                            "start": self.normalize(track.GetStart()),
                            "end": self.normalize(track.GetEnd()),
                            "width": track.GetWidth() * 1e-6,
                        }
                    if self.config.include_nets:
                        track_dict["net"] = track.GetNetname()
                    result[track.GetLayer()].append(track_dict)

        return {
            'F': result.get(pcbnew.F_Cu),
            'B': result.get(pcbnew.B_Cu)
        }

    def parse_zones(self, zones):
        result = {pcbnew.F_Cu: [], pcbnew.B_Cu: []}
        for zone in zones:  # type: pcbnew.ZONE
            if (not zone.IsFilled() or
                    hasattr(zone, 'GetIsKeepout') and zone.GetIsKeepout() or
                    hasattr(zone, 'GetIsRuleArea') and zone.GetIsRuleArea()):
                continue
            layers = [layer for layer in list(zone.GetLayerSet().Seq())
                      if layer in [pcbnew.F_Cu, pcbnew.B_Cu]]
            for layer in layers:
                try:
                    # kicad 5.1 and earlier
                    poly_set = zone.GetFilledPolysList()
                except TypeError:
                    poly_set = zone.GetFilledPolysList(layer)
                width = zone.GetMinThickness() * 1e-6
                if (hasattr(zone, 'GetFilledPolysUseThickness') and
                        not zone.GetFilledPolysUseThickness()):
                    width = 0
                zone_dict = {
                    "polygons": self.parse_poly_set(poly_set),
                    "width": width,
                }
                if self.config.include_nets:
                    zone_dict["net"] = zone.GetNetname()
                result[layer].append(zone_dict)

        return {
            'F': result.get(pcbnew.F_Cu),
            'B': result.get(pcbnew.B_Cu)
        }

    @staticmethod
    def parse_netlist(net_info):
        # type: (pcbnew.NETINFO_LIST) -> list
        nets = net_info.NetsByName().asdict().keys()
        nets = [str(s) for s in nets]
        nets.sort()
        return nets

    @staticmethod
    def footprint_to_component(footprint, extra_fields):
        try:
            footprint_name = str(footprint.GetFPID().GetFootprintName())
        except AttributeError:
            footprint_name = str(footprint.GetFPID().GetLibItemName())

        attr = 'Normal'
        if hasattr(pcbnew, 'FP_EXCLUDE_FROM_BOM'):
            if footprint.GetAttributes() & pcbnew.FP_EXCLUDE_FROM_BOM:
                attr = 'Virtual'
        elif hasattr(pcbnew, 'MOD_VIRTUAL'):
            if footprint.GetAttributes() == pcbnew.MOD_VIRTUAL:
                attr = 'Virtual'
        layer = {
            pcbnew.F_Cu: 'F',
            pcbnew.B_Cu: 'B',
        }.get(footprint.GetLayer())

        return Component(footprint.GetReference(),
                         footprint.GetValue(),
                         footprint_name,
                         layer,
                         attr,
                         extra_fields)

    def parse(self):
        from ..errors import ParsingException

        # Get extra field data from netlist
        need_extra_fields = (self.config.extra_fields or
                             self.config.board_variant_whitelist or
                             self.config.board_variant_blacklist or
                             self.config.dnp_field)

        if not self.config.netlist_file and need_extra_fields:
            self.logger.warn('Ignoring extra fields related config parameters '
                             'since no netlist/xml file was specified.')
            need_extra_fields = False

        extra_field_data = None
        if (self.config.netlist_file and
                os.path.isfile(self.config.netlist_file)):
            extra_field_data = self.extra_data_func(
                self.config.netlist_file, self.config.normalize_field_case)

        if extra_field_data is None and need_extra_fields:
            raise ParsingException(
                'Failed parsing %s' % self.config.netlist_file)

        extra_field_data = extra_field_data[1] if extra_field_data else None

        title_block = self.board.GetTitleBlock()
        title = title_block.GetTitle()
        revision = title_block.GetRevision()
        company = title_block.GetCompany()
        if (hasattr(self.board, "GetProject") and
                hasattr(pcbnew, "ExpandTextVars")):
            project = self.board.GetProject()
            title = pcbnew.ExpandTextVars(title, project)
            revision = pcbnew.ExpandTextVars(revision, project)
            company = pcbnew.ExpandTextVars(company, project)

        file_date = title_block.GetDate()
        if not file_date:
            file_mtime = os.path.getmtime(self.file_name)
            file_date = datetime.fromtimestamp(file_mtime).strftime(
                    '%Y-%m-%d %H:%M:%S')
        pcb_file_name = os.path.basename(self.file_name)
        if not title:
            # remove .kicad_pcb extension
            title = os.path.splitext(pcb_file_name)[0]
        edges, bbox = self.parse_edges(self.board)
        if bbox is None:
            self.logger.error('Please draw pcb outline on the edges '
                              'layer on sheet or any footprint before '
                              'generating BOM.')
            return None, None
        bbox = {
            "minx": bbox.GetPosition().x * 1e-6,
            "miny": bbox.GetPosition().y * 1e-6,
            "maxx": bbox.GetRight() * 1e-6,
            "maxy": bbox.GetBottom() * 1e-6,
        }

        drawings = self.get_all_drawings()

        pcbdata = {
            "edges_bbox": bbox,
            "edges": edges,
            "drawings": {
                "silkscreen": self.parse_drawings_on_layers(
                        drawings, pcbnew.F_SilkS, pcbnew.B_SilkS),
                "fabrication": self.parse_drawings_on_layers(
                        drawings, pcbnew.F_Fab, pcbnew.B_Fab),
            },
            "footprints": self.parse_footprints(),
            "metadata": {
                "title": title,
                "revision": revision,
                "company": company,
                "date": file_date,
            },
            "bom": {},
            "font_data": self.font_parser.get_parsed_font()
        }
        if self.config.include_tracks:
            pcbdata["tracks"] = self.parse_tracks(self.board.GetTracks())
            if hasattr(self.board, "Zones"):
                pcbdata["zones"] = self.parse_zones(self.board.Zones())
            else:
                self.logger.info("Zones not supported for KiCad 4, skipping")
                pcbdata["zones"] = {'F': [], 'B': []}
        if self.config.include_nets and hasattr(self.board, "GetNetInfo"):
            pcbdata["nets"] = self.parse_netlist(self.board.GetNetInfo())

        warning_shown = False
        if extra_field_data and need_extra_fields:
            e = []
            for f in self.footprints:
                e.append(extra_field_data.get(f.GetReference(), {}))
                if f.GetReference() not in extra_field_data:
                    # Some components are on pcb but not in schematic data.
                    # Show a warning about possibly outdated netlist/xml file.
                    self.logger.warn(
                        'Component %s is missing from schematic data.'
                        % f.GetReference())
                    warning_shown = True
        else:
            e = [{}] * len(self.footprints)

        if warning_shown:
            self.logger.warn('Netlist/xml file is likely out of date.')

        components = [self.footprint_to_component(f, ee)
                      for (f, ee) in zip(self.footprints, e)]

        return pcbdata, components


class InteractiveHtmlBomPlugin(pcbnew.ActionPlugin, object):

    def __init__(self):
        super(InteractiveHtmlBomPlugin, self).__init__()
        self.name = "Generate Interactive HTML BOM"
        self.category = "Read PCB"
        self.pcbnew_icon_support = hasattr(self, "show_toolbar_button")
        self.show_toolbar_button = True
        icon_dir = os.path.dirname(os.path.dirname(__file__))
        self.icon_file_name = os.path.join(icon_dir, 'icon.png')
        self.description = "Generate interactive HTML page with BOM " \
                           "table and pcb drawing."

    def defaults(self):
        pass

    def Run(self):
        from ..version import version
        from ..errors import ParsingException
        config = Config(version)
        board = pcbnew.GetBoard()
        pcb_file_name = board.GetFileName()

        logger = ibom.Logger()
        if not pcb_file_name:
            logger.error('Please save the board file before generating BOM.')
            return

        parser = PcbnewParser(pcb_file_name, config, logger, board)
        try:
            ibom.run_with_dialog(parser, config, logger)
        except ParsingException as e:
            logger.error(str(e))
