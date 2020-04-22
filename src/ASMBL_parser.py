import sys
import os
import re
from math import (
    inf,
    ceil,
    floor,
)
import src.utils as utils


class AdditiveGcodeLayer:
    """ Stores a complete layer of gcode produced in Simplify3d """

    def __init__(self, gcode, name=None, layer_height=None):
        self.gcode = gcode
        self.name = name
        self.layer_height = layer_height

        self.remove_park_gcode()

        if name is None:
            self.name = self.get_name(self.gcode)

        if layer_height is None:
            self.layer_height = self.get_layer_height(self.gcode)

    def get_name(self, gcode):
        return gcode.split(',')[0][2:]

    def get_layer_height(self, gcode):
        height = None
        lines = gcode.split('\n')

        # Check for Simplify3D end of file code
        if lines[0] == '; layer end':
            height = inf

        else:
            line_heights = []
            for line in lines:
                if line == '':
                    continue
                if line[0] == ';':
                    continue
                line_segments = line.split('Z')
                if len(line_segments) > 1:
                    line_height = float(line_segments[1].split(' ')[0])
                    line_heights.append(line_height)

            height = min(line_heights)

        return height

    def comment_all_gcode(self):
        commented_gcode = ''
        lines = self.gcode.split('\n')
        for line in lines:
            if line != '':
                if line[0] != ';':
                    line = '; ' + line
                commented_gcode += line + '\n'
        self.gcode = commented_gcode

    def remove_park_gcode(self):
        # Fusion adds some dirty end gcode
        # Kill it with fire until they let us control the end gcode with the post processor
        self.gcode = self.gcode.split('; move to park position')[0]


class CamGcodeLine:
    """ Stores a single line of fusion360 CAM gcode. """

    def __init__(self, gcode, offset, line_type):
        self.gcode = utils.offset_gcode(gcode, offset)
        self.layer_height = self.get_layer_height(self.gcode)
        self.type = line_type

    def get_layer_height(self, gcode):
        """Return the layer height of single line of gcode."""
        return float(gcode.split('Z')[1].split(' ')[0])


class CamGcodeSegment:
    """ Stores gcode lines for a sequence of a specific movement type """

    def __init__(self, segment_type=None, lines=[]):
        self.type = segment_type
        self.lines = lines
        self.planar = None
        self.height = None

        if self.type == 'cutting':
            self.set_z_height()

    def get_min_z_height(self):
        op_height = min(
            [line.layer_height for line in self.lines])
        return op_height

    def get_max_z_height(self):
        # Filter out retracts, only care about max Z height of cutting ops
        op_height = max(
            [line.layer_height for line in self.lines])
        return op_height

    def set_z_height(self, threshold=0.05):
        max_height = self.get_max_z_height()
        min_height = self.get_min_z_height()

        if round(max_height - min_height, 6) > threshold:
            self.height = max_height
            self.planar = False

        else:
            self.height = min_height
            self.planar = True


class CamGcodeLayer:
    """ Stores all the CAM operations in a specific layer. """

    def __init__(self, operations, name=None, strategy=None, tool=None, height=None):
        self.height = height
        self.operations = operations
        self.name = name
        self.strategy = strategy
        self.tool = tool
        self.planar = None

        if self.operations:
            self.gcode = self.parse_gcode(self.operations)

        self.layer_height = None  # height to print to before running the operation

    def parse_gcode(self, operations):
        """ Combines the gcode lines from all the operations into a single string """
        gcode = ''

        for op in operations:
            gcode += op.gcode + '\n'

        return gcode

    def get_min_z_height(self):
        op_height = min(
            [line.layer_height for line in self.operations if line.type == 'cutting'])
        return op_height

    def get_max_z_height(self):
        # Filter out retracts, only care about max Z height of cutting ops
        op_height = max(
            [line.layer_height for line in self.operations if line.type == 'cutting'])
        return op_height

    def get_retract_height(self):
        retract_height = max(
            [line.layer_height for line in self.operations])
        return retract_height

    def set_z_height(self, threshold=0.05):
        max_height = self.get_max_z_height()
        min_height = self.get_min_z_height()

        if round(max_height - min_height, 6) > threshold:
            self.height = max_height
            self.planar = False

        else:
            self.height = min_height
            self.planar = True


class NonPlanarOperation():
    def __init__(self, operation):
        self.name = operation[0].name
        self.tool = operation[0].tool
        self.cam_layers = operation
        self.set_z_height()

    def set_z_height(self):
        self.height = -inf
        for layer in self.cam_layers:
            if layer.height > self.height:
                self.height = layer.height


class Parser:
    """ Main parsing class. """

    def __init__(self, config, progress=None):
        self.config = config
        self.progress = progress    # progress bar for Fusion add-in
        self.offset = (config['Printer']['bed_centre_x'],
                       config['Printer']['bed_centre_y'],

                       config['PrintSettings']['raft_height'] - config['CamSettings']['layer_dropdown']
                       )

        self.last_additive_tool = None
        self.last_subtractive_tool = None

        self.main()

    def main(self):
        progress = self.progress

        if progress:
            progress.message = 'Opening files'
            progress.progressValue += 1
        self.open_files(self.config)

        # Fusion 360 currently only exports absolute extrusion gcode, this needs to be converted
        # This method will not convert gcode if it is already relative
        if progress:
            progress.message = 'Converting additive gcode to relative positioning'
            progress.progressValue += 1
        self.gcode_add = utils.convert_relative(self.gcode_add)

        if progress:
            progress.message = 'Spliting additive gcode layers'
            progress.progressValue += 1
        self.gcode_add_layers = self.split_additive_layers(self.gcode_add)

        if progress:
            progress.message = 'Spliting subtractive gcode layers'
            progress.progressValue += 1
        operations = self.split_cam_operations(self.gcode_sub)

        if progress:
            progress.message = 'Ordering subtractive gcode layers'
            progress.progressValue += 1
        self.cam_operations = self.order_cam_operations_by_layer(operations)

        if progress:
            progress.message = 'Merging gcode layers'
            progress.progressValue += 1
        self.merged_gcode = self.merge_gcode_layers(self.gcode_add_layers, self.cam_operations)

        if progress:
            progress.message = 'Creating gcode script'
            progress.progressValue += 1
        self.create_gcode_script(self.merged_gcode)

    def open_files(self, config):
        gcode_add_file = open(config['InputFiles']['additive_gcode'], 'r')
        self.gcode_add = gcode_add_file.read()

        gcode_sub_file = open(config['InputFiles']['subtractive_gcode'], 'r')
        self.gcode_sub = gcode_sub_file.read()

    def split_additive_layers(self, gcode_add):
        """ Takes Simplify3D gcode and splits in by layer """
        tmp_list = re.split('(; layer)', gcode_add)

        gcode_add_layers = []
        initialise_layer = AdditiveGcodeLayer(
            tmp_list.pop(0),
            name="initialise",
            layer_height=0,
        )    # slicer settings & initialise
        self.set_last_additive_tool(initialise_layer)
        # initialise_layer.comment_all_gcode()
        gcode_add_layers.append(initialise_layer)

        for i in range(ceil(len(tmp_list)/2)):

            layer = tmp_list[2*i] + tmp_list[2*i+1]
            name = layer.split(',')[0][2:]

            if 2*i + 1 == len(tmp_list) - 1:
                gcode_add_layers.append(AdditiveGcodeLayer(
                    layer, 'end', inf))
                continue

            gcode_add_layers.append(AdditiveGcodeLayer(layer))

        return gcode_add_layers

    def assign_cam_line_type(self, unlabelled_lines):
        """ extract type information from string, returns a list of CamGcodeLine's """
        lines = []
        line_type = None
        for line in unlabelled_lines:
            if line.startswith('(type: '):
                line_type = line[7:].strip(')')
            else:
                lines.append(CamGcodeLine(line, self.offset, line_type))

        return lines

    def group_cam_lines(self, lines):
        """
        Group consequetive lines of the same type into segments
        Returns a list of segments
        """
        segments = []
        segment_lines = [lines[0]]
        for i, line in enumerate(lines[1:]):
            if line.type != lines[i].type:
                segments.append(CamGcodeSegment(lines[i].type, segment_lines))
                segment_lines = [line]
            else:
                segment_lines.append(line)

        segments.append(CamGcodeSegment(lines[-1].type, segment_lines))

        return segments

    def group_cam_segments(self, segments):
        segment_groups = []
        segment_group
        cutting_height = None
        for i, segment in enumerate(segments):
            if segment.type == 'cutting' and segment.height == cutting_height:
                

    def split_cam_operations(self, gcode_sub):
        """ Takes fusion360 CAM gcode and splits the operations by execution height """
        tmp_operation_list = gcode_sub.split('\n\n')

        operations = []

        for i, operation in enumerate(tmp_operation_list):
            unlabelled_lines = operation.split('\n')
            name = unlabelled_lines.pop(0)
            strategy = unlabelled_lines.pop(0)[11:].strip(')')
            tool = unlabelled_lines.pop(0)
            unlabelled_lines = [line for line in unlabelled_lines if line != '']

            operations.append([])

            lines = self.assign_cam_line_type(unlabelled_lines)
            segments = self.group_cam_lines(lines)

            line_heights = [line.layer_height for line in lines]
            local_peaks = utils.find_maxima(line_heights)

            if len(local_peaks) > 0:
                op_lines = lines[0: local_peaks[0]+1]
                operations[i].append(CamGcodeLayer(op_lines, name, strategy, tool))

                for index, peak in enumerate(local_peaks[:-1]):
                    op_lines = lines[local_peaks[index]: local_peaks[index+1]+1]
                    operations[i].append(CamGcodeLayer(op_lines, name, strategy, tool))

                op_lines = lines[local_peaks[-1]:]
                operations[i].append(CamGcodeLayer(op_lines, name, strategy, tool))
            else:
                operations[i].append(CamGcodeLayer(lines, name, strategy, tool))

        return operations

    def order_cam_operations_by_layer(self, operations):
        """ Takes a list of cam operations and calculates the layer that they should be executed """
        unordered_ops = []
        for operation in operations:
            if operation:
                for op_instance in operation:
                    # Finds the min and max z height of the where the tool is "cutting" (ie a Feedrate is specified)
                    # If operation name starts with 'NP_' then operation is classified as 'Non-Planar'
                    op_instance.set_z_height()

        for operation in operations:
            non_planar_layers = [op_instance for op_instance in operation if not op_instance.planar]
            planar_layers = [op_instance for op_instance in operation if op_instance.planar]

            for op_instance in planar_layers:
                unordered_ops.append(op_instance)

            if non_planar_layers:
                non_planar_op = NonPlanarOperation(non_planar_layers)
                unordered_ops.append(non_planar_op)

        ordered_operations = sorted(unordered_ops, key=lambda x: x.height)
        layer_overlap = self.config['CamSettings']['layer_overlap']

        for i, op_instance in enumerate(ordered_operations):
            later_ops = [op for op in ordered_operations if op.height > op_instance.height]
            if len(later_ops) > 0:
                next_op_height = min([op.height for op in later_ops])
                later_additive = [layer for layer in self.gcode_add_layers[:-1] if layer.layer_height > next_op_height]

                if layer_overlap == 0:
                    op_instance.layer_height = next_op_height

                elif len(later_additive) == 0:
                    op_instance.layer_height = next_op_height

                elif len(later_additive) >= layer_overlap:
                    op_instance.layer_height = later_additive[layer_overlap - 1].layer_height

                else:
                    op_instance.layer_height = later_additive[-1].layer_height

            else:  # no later ops
                later_additive = [layer for layer in self.gcode_add_layers[:-1]
                                  if layer.layer_height > op_instance.height]

                if len(later_additive) == 0:   # no further printing
                    # add 10 since it is unlikely that the printed layer height will exceed 10 mm
                    # but still want to place cutting after a print at the same height
                    op_instance.layer_height = op_instance.height + 10

                elif len(later_additive) >= layer_overlap:
                    op_instance.layer_height = later_additive[layer_overlap - 1].layer_height

                else:
                    op_instance.layer_height = later_additive[-1].layer_height

            if op_instance.layer_height == inf:
                raise ValueError("CAM op height can't be 'inf'")

        # Expand out non planar layers
        expanded_ordered_operations = []
        for layer in ordered_operations:
            if type(layer) == NonPlanarOperation:
                for non_planar_layer in layer.cam_layers:
                    non_planar_layer.layer_height = layer.layer_height
                    expanded_ordered_operations.append(non_planar_layer)
            else:
                expanded_ordered_operations.append(layer)

        return expanded_ordered_operations

    def merge_gcode_layers(self, gcode_add, cam_operations):
        """ Takes the individual CAM instructions and merges them into the additive file from Simplify3D """
        merged_gcode = gcode_add + cam_operations
        merged_gcode.sort(key=lambda x: x.layer_height)

        return merged_gcode

    def create_gcode_script(self, gcode):
        self.merged_gcode_script = '; ASMBL gcode created by https://github.com/AndyEveritt/ASMBL\n'
        prev_layer = gcode[0]
        for layer in gcode:
            self.set_last_additive_tool(prev_layer)
            self.tool_change(layer, prev_layer)
            prev_layer = layer
            self.merged_gcode_script += layer.gcode

    def set_last_additive_tool(self, layer):
        if isinstance(layer, AdditiveGcodeLayer):
            process_list = layer.gcode.split('\nT')
            if len(process_list) > 1:
                self.last_additive_tool = 'T' + process_list[-1].split('\n')[0]

    def tool_change(self, layer, prev_layer):
        if type(layer) == AdditiveGcodeLayer:
            if layer.name == 'initialise' or prev_layer.name == 'initialise':
                return  # no need to add a tool change
            first_gcode = layer.gcode.split('\n')[1]
            if first_gcode[0] is not 'T':
                self.merged_gcode_script += self.last_additive_tool + '\n'
        elif type(layer) == CamGcodeLayer:
            self.merged_gcode_script += layer.tool + '\n'

    def create_output_file(self, gcode, folder_path="output/", relative_path=True):
        """ Saves the file to the output folder """
        file_path = folder_path + self.config['OutputSettings']['filename'] + ".gcode"

        file_path = os.path.expanduser(file_path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        with open(file_path, "w") as f:
            f.write(gcode)

        f.close()

        try:
            os.startfile(file_path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    gcode_add_file = open("gcode/cyclodial_gear/additive.gcode", "r")
    gcode_add = gcode_add_file.read()

    gcode_sub_file = open("gcode/cyclodial_gear/cam.nc", "r")
    gcode_sub = gcode_sub_file.read()

    parser = Parser(gcode_add, gcode_sub)
    parser.create_output_file(parser.merged_gcode_script)
    pass
