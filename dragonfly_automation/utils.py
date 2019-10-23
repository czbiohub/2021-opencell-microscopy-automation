import os
import re
import sys
import json
import skimage
import datetime
import numpy as np

from scipy import interpolate


def timestamp():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def to_uint8(im):

    dtype = 'uint8'
    max_value = 255
    im = im.copy().astype(float)

    percentile = 1
    minn, maxx = np.percentile(im, (percentile, 100 - percentile))
    if minn==maxx:
        return (im * 0).astype(dtype)

    im = im - minn
    im[im < minn] = 0
    im = im/(maxx - minn)
    im[im > 1] = 1
    im = (im * max_value).astype(dtype)
    return im


def interpolate_focusdrive_positions_from_corners(
    position_list_filepath, 
    region_shape, 
    num_positions_per_well,
    corner_positions):
    '''
    This method refactors the 'StageTiltDragonfly.py' script

    Parameters
    ----------
    position_list_filepath: str
        Local path to a JSON list of positions; these positions are assumed 
        to have been generated by the HCS Site Generator plugin
    region_shape: tuple of (num_rows, num_columns)
        The size/shape of the plate 'region' to be imaged
    num_positions_per_well : int
        The number of positions per well
    corner_positions: tuple of tuples
        The user-measured z-positions (FocusDrive device positions) 
        of the corners of the plate region, as a tuple of the form
        (
            (top_left, top_right), 
            (bottom_left, bottom_right),
        )

    Returns
    -------
    filepath : the path to the interpolated position list
    position_list : the position list itself

    '''
    num_rows, num_cols = region_shape

    # linearly interpolate the z-positions
    rows = [0, 0, num_rows, num_rows]
    cols = [0, num_cols, 0, num_cols]
    z = np.array(corner_positions).flatten()
    interpolator = interpolate.interp2d(rows, cols, z, kind='linear')

    with open(position_list_filepath) as file:
        position_list = json.load(file)

    # loop over each well in the region
    for row_ind in range(num_rows):
        for col_ind in range(num_cols):

            # Here we account for the snake-like order of the positions
            # in the position_list generated by the HCS Site Generator plugin
            # This list begins with the positions in the top-left-most well,
            # then traverses the top-most row of wells from right to left, 
            # then traverses the second row from left to right, and so on
            if row_ind % 2 == 0:
                physical_col_ind = (num_cols - 1) - col_ind
            else:
                physical_col_ind = col_ind
            
            # the interpolated z-position of the current well
            interpolated_position = interpolator(row_ind, physical_col_ind)[0]

            # the config entry for the 'FocusDrive' device (this is the motorized z-stage)
            focus_drive_config = {
                'X': interpolated_position,
                'Y': 0,
                'Z': 0,
                'AXES': 1,
                'DEVICE': 'FocusDrive',
            }

            # copy the FocusDrive config into the position_list
            # at each position of the current well
            for pos_ind in range(num_positions_per_well):
                ind = num_positions_per_well * (row_ind * num_cols + col_ind) + pos_ind
                position_list['POSITIONS'][ind]['DEVICES'].append(focus_drive_config)
    
    # save the new position_list
    ext = position_list_filepath.split('.')[-1]
    new_filepath = re.sub('.%s$' % ext, '_INTERPOLATED.%s' % ext, position_list_filepath)
    with open(new_filepath, 'w') as file:
        json.dump(position_list, file)

    return new_filepath, position_list



def well_id_to_position(well_id):
    '''
    'A1' to (0, 0), 'H12' to (7, 11), etc
    '''
    pattern = r'^([A-H])([0-9]{1,2})$'
    result = re.findall(pattern, well_id)
    row, col = result[0]
    row_ind = list('ABCDEFGH').index(row)
    col_ind = int(col) - 1
    return row_ind, col_ind


def parse_hcs_site_label(label):
    '''
    Parse an HCS site label
    ** copied from PipelinePlateProgram **
    '''
    pattern = r'^([A-H][0-9]{1,2})-Site_([0-9]+)$'
    result = re.findall(pattern, label)
    well_id, site_num = result[0]
    site_num = int(site_num)
    return well_id, site_num


def interpolate_focusdrive_positions_from_all(
    position_list_filepath, 
    measured_focusdrive_positions, 
    top_left_well_id,
    bottom_right_well_id):
    '''

    measured_focusdrive_positions = {
        'B9': 7600,
        'B5': 7500,
        'B2': 7600,
        ...
    }

    '''

    # create an array of numeric (x,y,z) positions from the well_ids
    measured_positions = np.array([
        (*well_id_to_position(well_id), pos) 
            for well_id, pos in measured_focusdrive_positions.items()])

    interpolator = interpolate.interp2d(
        measured_positions[:, 0], 
        measured_positions[:, 1], 
        measured_positions[:, 2], 
        kind='linear')

    with open(position_list_filepath) as file:
        position_list = json.load(file)

    for ind, pos in enumerate(position_list['POSITIONS']):
        
        well_id, site_num = parse_hcs_site_label(pos['LABEL'])
        x, y = well_id_to_position(well_id)

        # the interpolated z-position of the current well
        interpolated_position = interpolator(x, y)[0]

        # the config entry for the 'FocusDrive' device (this is the motorized z-stage)
        focus_drive_config = {
            'X': interpolated_position,
            'Y': 0,
            'Z': 0,
            'AXES': 1,
            'DEVICE': 'FocusDrive',
        }

        position_list['POSITIONS'][ind]['DEVICES'].append(focus_drive_config)
    
    # save the new position_list
    ext = position_list_filepath.split('.')[-1]
    new_filepath = re.sub('.%s$' % ext, '_INTERPOLATED-FROM-ALL.%s' % ext, position_list_filepath)
    with open(new_filepath, 'w') as file:
        json.dump(position_list, file)

    return new_filepath, position_list



def rename_tiff_stacks(positions_filepath, tiff_dirpath, logger=None):
    '''
    Rename TIFF stacks acquired by a scripted acquisition that 
    1) used stage positions generated by the HCS Site Generator plugin for MicroManager
    2) saved TIFF stacks using a datastore object returned by `createMultipageTIFFDatastore`

    In other words, this method refactors and generalizes 
    the 'FileRename_HalfPlates_MM2.py' script from Nathan.


    ASSUMPTIONS
    -----------


    PARAMETERS
    ----------


    '''

    pass
