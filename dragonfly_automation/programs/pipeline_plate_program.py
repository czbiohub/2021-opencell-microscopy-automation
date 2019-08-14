
import os
import numpy as np
from skimage import filters

from dragonfly_automation import operations
from dragonfly_automation import global_settings
from dragonfly_automation.gateway import gateway_utils
from dragonfly_automation.programs import pipeline_plate_settings as settings


class PipelinePlateProgram(object):


    def __init__(self, data_dirpath=None, env='dev'):

        self.env = env
        self.data_dirpath = data_dirpath

        # create the py4j objects
        self.gate, self.mm_studio, self.mm_core = gateway_utils.get_gate(env=env)

        # copied from Nathan's script - likely unnecessary
        self.mm_core.setExposure(settings.DEFAULT_EXPOSURE_TIME)

        if env=='prod':
            self.datastore = self._initialize_datastore()
        
        if env=='dev':

            # no mock yet for the datastore object
            self.datastore = None

            # reduce the number of z-steps to make debugging easier
            settings.ZSTACK_STEP_SIZE = 6.0


    def _initialize_datastore(self):

        if self.data_dirpath is None:
            raise ValueError('A data directory must be provided')

        os.makedirs(self.data_dirpath, exist_ok=True)

        # these arguments for createMultipageTIFFDatastore are copied from Nathan's script
        should_generate_separate_metadata = True
        should_split_positions = True

        datastore = self.mm_studio.data().createMultipageTIFFDatastore(
            self.data_dirpath, should_generate_separate_metadata, should_split_positions)

        self.mm_studio.displays().createDisplay(datastore)
        return datastore


    def setup(self):
        '''
        Generic microscope setup
        set the autofocus mode and run the `mm_core.assignImageSynchro` calls

        '''

        # change autofocus mode to AFC
        af_manager = self.mm_studio.getAutofocusManager()
        af_manager.setAutofocusMethodByName("Adaptive Focus Control")

        # these `assignImageSynchro` calls are copied directly from Nathan's script
        # TODO: check with Bryant if these are necessary
        self.mm_core.assignImageSynchro(global_settings.PIEZO_STAGE)
        self.mm_core.assignImageSynchro(global_settings.XY_STAGE)
        self.mm_core.assignImageSynchro(self.mm_core.getShutterDevice())
        self.mm_core.assignImageSynchro(self.mm_core.getCameraDevice())

        # turn on auto shutter mode 
        # (this means that the shutter automatically opens and closes when an image is acquired)
        self.mm_core.setAutoShutter(True)


    def cleanup(self):
        '''
        Commands that should be run after the acquisition is complete
        (that is, at the very end of self.run)
        '''

        if self.datastore:
            self.datastore.freeze()


    @staticmethod
    def _is_first_position_in_new_well(position_label):
        '''
        This is the logic Nathan used to determine 
        whether a position is the first position in a new well

        Note that the position_label is assumed to have been generated by 
        the 96-well-plate position plugin for MicroManager
        '''
        flag = ('Site_0' in position_label) or ('Pos_000_000' in position_label)
        return flag


    def confluency_test(self):
        return True


    def run(self):
        '''
        The main acquisition workflow
        
        The outermost loop is over all of the positions loaded into MicroManager
        (that is, all positions returned by `mm_studio.getPositionList()`)
        
        *** We assume that these positions were generated by the 96-well-plate platemap/position plugin ***

        In particular, we assume that the list of positions corresponds 
        to some number of FOVs in some number of distinct wells,
        and that all of the FOVs in each well appear together.

        At each position, the following steps are performed:

            1) reset the piezo z-stage to zero
            2) move to the new position (this moves the xy-stage and the FocusDrive z-stage)
            3) check if the new position is the first FOV of a new well
               (if it is, we will need to run the autoexposure routine)
            4) check if we already have enough FOVs for the current well
               (if we do, we'll skip the position)
            5) autofocus using the 405 ('DAPI') laser
            6) run the confluency test (and skip the position if it fails)
            7) run the autoexposure routine using the 488 ('GFP') laser
            8) acquire the z-stack in 405 and 488 and 'put' the stacks in self.datastore

        '''


        position_list = self.mm_studio.getPositionList()
        for position_index in range(position_list.getNumberOfPositions()):
            print('------------------------- Position %d -------------------------' % position_index)

            # reset the piezo stage
            self.mm_core.setPosition(global_settings.PIEZO_STAGE, 0.0)

            # move to the next position
            position = position_list.getPosition(position_index)
            position.goToPosition(position, self.mm_core)

            # check if the position is the first one in a new well
            new_well_flag = self._is_first_position_in_new_well(position.getLabel())
            if new_well_flag:
                self.num_fovs_acquired_in_current_well = 0

            # if we have already acquired enough FOVs from the current well
            if self.num_fovs_acquired_in_current_well >= settings.MAX_NUM_FOV_PER_WELL:
                continue

            # autofocus
            operations.autofocus(
                self.mm_studio, 
                self.mm_core, 
                channel_name=settings.CHANNEL_405['name'],
                laser_name=settings.CHANNEL_405['laser_name'],
                laser_power=settings.CHANNEL_405['laser_power'],
                camera_gain=settings.DEFAULT_CAMERA_GAIN,
                exposure_time=settings.DEFAULT_EXPOSURE_TIME)
    

            # confluency check
            if not self.confluency_test():
                continue
    

            # auto-exposure (only if this is the first FOV of a new well)
            if new_well_flag:
                exposure_time, laser_power, status = operations.autoexposure(settings.CHANNEL_488)
                settings.CHANNEL_488['calculated_laser_power'] = laser_power
                settings.CHANNEL_488['calculated_exposure_time'] = exposure_time
            
            # acquire the stacks using the calculated laser power and exposure time
            operations.acquire_stack(self.datastore, settings.CHANNEL_405)
            operations.acquire_stack(self.datastore, settings.CHANNEL_488)


        self.cleanup()


