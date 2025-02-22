# -*- coding: utf-8 -*-

"""
Much like DOASWorker but designed for iFit rather than DOAS retrieval using Esse's code for iFit retrieval.
This could be advantageous since we don't know exactly what the clear-sky spectrum looks like? Although this isn't
critical for getting the gradient of AA vs ppm.m in camera calibration.
This work may also allow light dilution correction following Varnam 2021
"""

import time
import queue
import numpy as np
import datetime
import threading
from itertools import compress
from tkinter import filedialog
from astropy.convolution import convolve
from scipy.interpolate import griddata
import matplotlib.pyplot as plt
from matplotlib.pyplot import GridSpec
import sys
import os
import pandas as pd
from shapely.geometry import Point, Polygon     # For Varnam light dilution
from shapely.strtree  import STRtree
# Make it possible to import iFit by updating path
# dir_path = os.path.dirname(os.path.realpath(__file__))
# dir_path = os.path.split(dir_path)[0]
# sys.path.append(os.path.join(dir_path, 'ifit'))

from pycam.directory_watcher import create_dir_watcher
from pycam.setupclasses import SpecSpecs, FileLocator
from pycam.io_py import load_spectrum, spec_txt_2_npy
from pycam.iFit.ifit.parameters import Parameters
from pycam.iFit.ifit.spectral_analysis import Analyser
from pycam.ifit_ld.ifit_mod.synthetic_suite import Analyser_ld
from pycam.ifit_ld import lookup
from pydoas.analysis import DoasResults

try:
    from scipy.constants import N_A
except BaseException:
    N_A = 6.022140857e+23


class IFitWorker:
    """
    Class to control DOAS processing
    General order of play for processing:
    Initiate class,
    get_ref_spectrum()
    set_fit_window()
    shift_spectrum()

    :param q_doas: queue.Queue   Queue where final processed dictionary is placed (should be a PyplisWorker.q_doas)
    """
    def __init__(self, routine=2, species={'SO2': {'path': '', 'value': 0}}, spec_specs=SpecSpecs(), spec_dir='C:\\',
                 dark_dir=None, q_doas=queue.Queue(), frs_path='./ifit/Ref/sao2010.txt'):
        self.routine = routine          # Defines routine to be used, either (1) Polynomial or (2) Digital Filtering

        self.spec_specs = spec_specs    # Spectrometer specifications

        # ======================================================================================================================
        # Initial Definitions
        # ======================================================================================================================
        self.ppmm_conversion = 2.7e15   # convert absorption cross-section in cm2/molecule to ppm.m (MAY NEED TO CHANGE THIS TO A DICTIONARY AS THE CONVERSION MAY DIFFER FOR EACH SPECIES?)
        self._conversion_factor = 2.663 * 1e-6  # Conversion for ppm.m into Kg m-2
        MOL_MASS_SO2 = 64.0638  # g/mol
        self.ppmm_conv = (self._conversion_factor * N_A * 1000) / (
                100 ** 2 * MOL_MASS_SO2)  # Conversion for ppm.m to molecules cm-2

        self.shift = 0                  # Shift of spectrum in number of pixels
        self.shift_tol = 0              # Shift tolerance (will process data at multiple shifts defined by tolerance)
        self.stretch = 0                # Stretch of spectrum
        self.stretch_tol = 0            # As shift_tol but for stretch
        self.stretch_adjuster = 0.0001  # Factor to scale stretch (needed if different spectrometers have different pixel resolutions otherwise the stretch applied may be in too large or too small stages)
        self.stretch_resample = 100     # Number of points to resample the spectrum by during stretching
        self._start_stray_pix = None    # Pixel space stray light window definitions
        self._end_stray_pix = None
        self._start_stray_wave = 293    # Wavelength space stray light window definitions
        self._end_stray_wave = 296
        self._start_fit_pix = None  # Pixel space fitting window definitions
        self._end_fit_pix = None
        self._start_fit_wave_init = 300  # Wavelength space fitting window definitions       Set big range to start Analyser
        self._end_fit_wave_init = 340
        self._start_fit_wave = 308       # Update fit window to more reasonable starting size (initial setting was to create a big grid
        self._end_fit_wave = 318
        self.start_fit_wave_2 = 312      # Second fit window (used in light dilution correction)
        self.end_fit_wave_2 = 322
        self.fit_window = None      # Fitting window, determined by set_fit_window()
        self.fit_window_ref = None  # Placeholder for shifted fitting window for the reference spectrum
        self.wave_fit = True        # If True, wavelength parameters are used to define fitting window

        self.wavelengths = None         # Placeholder for wavelengths attribute which contains all wavelengths of spectra
        self.wavelengths_cut = None     # Wavelengths in fit window
        self._dark_spec = None           # Dark spectrum
        self.dark_dict = {}             # Dictionary holding all dark spectra loaded in
        self._clear_spec_raw = None     # Clear (fraunhofer) spectrum - not dark corrected
        self._plume_spec_raw = None     # In-plume spectrum (main one which is used for calculation of SO2
        self.clear_spec_corr = None     # Clear (fraunhofer) spectrum - typically dark corrected and stray light corrected
        self.plume_spec_corr = None     # In-plume spectrum (main one which is used for calculation of SO2
        self.spec_time = None           # Time of currently loaded plume_spec
        self.ref_spec = dict()          # Create empty dictionary for holding reference spectra
        self.ref_spec_interp = dict()   # Empty dictionary to hold reference spectra after sampling to spectrometer wavelengths
        self.ref_spec_conv = dict()     # Empty dictionary to hold reference spectra after convolving with ILS
        self.ref_spec_cut = dict()      # Ref spectrum cut to fit window
        self.ref_spec_ppmm = dict()   # Convolved ref spectrum scaled by ppmm_conversion factor
        self.ref_spec_filter = dict()   # Filtered reference spectrum
        self.ref_spec_fit = dict()      # Ref spectrum scaled by ppmm (for plotting)
        self.ref_spec_types = ['SO2', 'O3', 'Ring'] # List of reference spectra types accepted/expected
        self.ref_spec_used = list(species.keys())   # Reference spectra we actually want to use at this time (similar to ref_spec_types - perhaps one is obsolete (or should be!)
        self.abs_spec = None
        self.abs_spec_cut = None
        self.abs_spec_filt = None
        self.abs_spec_species = dict()  # Dictionary of absorbances isolated for individual species
        self.ILS_wavelengths = None     # Wavelengths for ILS
        self._ILS = None                 # Instrument line shape (will be a numpy array)
        self.ils_path = None
        self.processed_data = False     # Bool to define if object has processed DOAS yet - will become true once process_doas() is run

        self.poly_order = 2  # Order of polynomial used to fit residual

        self.start_ca = -2000  # Starting column amount for iterations
        self.end_ca = 20000  # Ending column amount for iterations
        self.vals_ca = np.arange(self.start_ca, self.end_ca+1)  # Array of column amounts to be iterated over
        self.vals_ca_cut_idxs = np.arange(0, len(self.vals_ca), 100)
        self.vals_ca_cut = self.vals_ca[self.vals_ca_cut_idxs]
        self.mse_vals_cut = np.zeros(len(self.vals_ca_cut))
        self.mse_vals = np.zeros(len(self.vals_ca))  # Array to hold mse values

        self.std_err = None
        self.fit_errs = dict()
        self.column_density = dict()

        self.filetypes = dict(defaultextension='.png', filetypes=[('PNG', '*.png')])

        # ----------------------------------------------------------------------------------
        # We need to make sure that all images are dark subtracted before final processing
        # Also make sure that we don't dark subtract more than once!
        self.ref_convolved = False  # Bool defining if reference spe has been convolved - speeds up DOAS processing
        self.new_spectra = True
        self.dark_corrected_clear = False
        self.dark_corrected_plume = False
        self.stray_corrected_clear = False    # Bool defining if all necessary spectra have been stray light corrected
        self.stray_corrected_plume = False    # Bool defining if all necessary spectra have been stray light corrected

        self.have_dark = False  # Used to define if a dark image is loaded.
        self.cal_dark_corr = False  # Tells us if the calibration image has been dark subtracted
        self.clear_dark_corr = False  # Tells us if the clear image has been dark subtracted
        self.plume_dark_corr = False  # Tells us if the plume image has been dark subtracted
        # ==============================================================================================================

        # Processing loop attributes
        self.process_thread = None      # Thread for running processing loop
        self.processing_in_thread = False   # Flags whether the object is processing in a thread or in the main thread - therefore deciding whether plots should be updated herein or through pyplisworker
        self.q_spec = queue.Queue()     # Queue where spectra files are placed, for processing herein
        self.q_doas = q_doas
        self.q_stop = queue.Queue()     # Queue for stopping processing of sequence
        self.watcher = None
        self.watching_dir = None
        self.watching = False
        self.STOP_FLAG = 'exit'
        self.plot_iter = True           # Plots time series plot iteratively if true

        self._dark_dir = None
        self.dark_dir = dark_dir        # Directory where dark images are stored
        self.spec_dir = spec_dir        # Directory where plume spectra are stored
        self.spec_dict = {}             # Dictionary containing all spectrum files from current spec_dir

        # Figures
        self.fig_spec = None            # pycam.doas.SpectraPlot object
        self.fig_doas = None            # pycam.doas.DOASPlot object
        self.fig_series = None          # pycam.doas.CDSeries object

        # Results object
        self.results = DoasResults([], index=[], fit_errs=[], species_id='SO2')
        self.results.ldfs = []
        self.save_date_fmt = '%Y-%m-%dT%H%M%S'
        self.save_freq = [0]

        # ==============================================================================================================
        # iFit setup
        # ==============================================================================================================
        self.frs_path = frs_path
        self.ref_spec_used = list(species.keys())
        self.species_info = species

        # Create parameter dictionary
        self.params = Parameters()

        # Add the gases
        for spec in species:
            # Load reference spectrum (this adds to self.params too
            self.load_ref_spec(species[spec]['path'], spec, value=species[spec]['value'], update=False)

        # Add background polynomial parameters
        self.params.add('bg_poly0', value=0.0, vary=True)
        self.params.add('bg_poly1', value=0.0, vary=True)
        self.params.add('bg_poly2', value=0.0, vary=True)
        self.params.add('bg_poly3', value=1.0, vary=True)

        # Add intensity offset parameters
        self.params.add('offset0', value=0.0, vary=True)

        # Add wavelength shift parameters
        self.params.add('shift0', value=0.0, vary=True)
        self.params.add('shift1', value=0.1, vary=True)

        # Setup ifit analyser (we will stray correct ourselves
        self.analyser = Analyser(params=self.params,
                                 fit_window=[self._start_fit_wave_init, self._end_fit_wave_init],
                                 frs_path=frs_path,
                                 stray_flag=False,      # We stray correct prior to passing spectrum to Analyser
                                 dark_flag=False)       # We dark correct prior to passing the spectrum to Analyser

        # LD attributes
        self.corr_light_dilution = True
        self.applied_ld_correction = False      # True if spectrum was corrected for light dilution during processing
        self.recal_ld_mins = 0                  # Recalibration time for light dilution correction (means don't have to calculate it for every spectrum)
        self.spec_time_last_ld = None           # Time of spectrum for last LDF lookup - used with recal_ld_mins to decide whether to lookup LDf or use previous LDF
        self.ifit_so2_0, self.ifit_err_0 = None, None
        self.ifit_so2_1, self.ifit_err_1 = None, None
        self.ifit_so2_0_path, self.ifit_so2_0_path = 'None', 'None'
        self.grid_max_ppmm = 5000
        self.grid_increment_ppmm = 20
        self.so2_grid_ppmm = np.arange(0, self.grid_max_ppmm, self.grid_increment_ppmm)
        self.so2_grid = np.multiply(self.so2_grid_ppmm, 2.652e+15)    # Convert SO2 in ppmm to molecules/cm2
        self.ldf_grid = np.arange(0, 1.00, 0.005)
        self.analyser0 = None
        self.analyser1 = None
        self.fit = None
        self.fit_0 = None
        self.fit_1 = None
        self.fit_0_uncorr = None
        self.fit_1_uncorr = None
        self.ldf_best = np.nan      # Best estimate of light dilution factor

    def reset_self(self, reset_dark=True):
        """Some resetting of object, before processing occurs"""
        # Reset dark dictionary
        if reset_dark:
            self.dark_dict = {}

        # Reset results object
        self.reset_doas_results()

        # Reset last LDF correction
        self.spec_time_last_ld = None

        # Clear stop queue so old requests aren't caught
        with self.q_stop.mutex:
            self.q_stop.queue.clear()

        # Clear images queue
        with self.q_spec.mutex:
            self.q_spec.queue.clear()

    @property
    def start_stray_wave(self):
        return self._start_stray_wave

    @start_stray_wave.setter
    def start_stray_wave(self, value):
        self._start_stray_wave = value

        # Set pixel value too, if wavelengths attribute is present
        if self.wavelengths is not None:
            self._start_stray_pix = np.argmin(np.absolute(self.wavelengths - value))

    @property
    def end_stray_wave(self):
        return self._end_stray_wave

    @end_stray_wave.setter
    def end_stray_wave(self, value):
        self._end_stray_wave = value

        # Set pixel value too, if wavelengths attribute is present
        if self.wavelengths is not None:
            self._end_stray_pix = np.argmin(np.absolute(self.wavelengths - value))

    @property
    def start_fit_wave(self):
        return self._start_fit_wave

    @start_fit_wave.setter
    def start_fit_wave(self, value):
        self._start_fit_wave = value
        # self.analyser.fit_window = [self.start_fit_wave, self.end_fit_wave]
        # self.update_analyser()

        # Set pixel value too, if wavelengths attribute is present
        if self.wavelengths is not None:
            self._start_fit_pix = np.argmin(np.absolute(self.wavelengths - value))

    @property
    def end_fit_wave(self):
        return self._end_fit_wave

    @end_fit_wave.setter
    def end_fit_wave(self, value):
        self._end_fit_wave = value
        # self.analyser.fit_window = [self.start_fit_wave, self.end_fit_wave]
        # self.update_analyser()

        # Set pixel value too, if wavelengths attribute is present
        if self.wavelengths is not None:
            self._end_fit_pix = np.argmin(np.absolute(self.wavelengths - value))

    # --------------------------------------------
    # Set spectra attributes so that whenever they are updated we flag that they have not been dark or stray corrected
    # If we have a new dark spectrum too we need to assume it is to be used for correcting spectra, so all corrected
    # spectra, both dark and stray, become invalid
    @property
    def dark_spec(self):
        return self._dark_spec

    @dark_spec.setter
    def dark_spec(self, value):
        self._dark_spec = value

        # If we have a new dark image all dark and stray corrections become invalid
        self.dark_corrected_clear = False
        self.dark_corrected_plume = False
        self.stray_corrected_clear = False
        self.stray_corrected_plume = False

    @property
    def dark_dir(self):
        return self._dark_dir

    @dark_dir.setter
    def dark_dir(self, value):
        """If dark_dir is changed we need to reset the dark_dict which holds preloaded dark specs"""
        self.dark_dict = {}
        self._dark_dir = value
        print('Dark spectra directory set: {}'.format(self.dark_dir))

    @property
    def clear_spec_raw(self):
        return self._clear_spec_raw

    @clear_spec_raw.setter
    def clear_spec_raw(self, value):
        self._clear_spec_raw = value
        self.dark_corrected_clear = False
        self.stray_corrected_clear = False

    @property
    def plume_spec_raw(self):
        return self._plume_spec_raw

    @plume_spec_raw.setter
    def plume_spec_raw(self, value):
        self._plume_spec_raw = value
        self.dark_corrected_plume = False
        self.stray_corrected_plume = False

    @property
    def ILS(self):
        """ILS array"""
        return self._ILS

    @ILS.setter
    def ILS(self, value):
        self._ILS = value

        # If new ILS is generated, then must flag that ref spectrum is no longer convolved with up-to-date ILS
        self.ref_convolved = False

    # -------------------------------------------

    def get_spec_time(self, filename):
        """
        Gets time from filename and converts it to datetime object
        :param filename:
        :return spec_time:
        """
        # Make sure filename only contains file and not larger pathname
        filename = filename.split('\\')[-1].split('/')[-1]

        # Extract time string from filename
        time_str = filename.split('_')[self.spec_specs.file_date_loc]

        # Turn time string into datetime object
        spec_time = datetime.datetime.strptime(time_str, self.spec_specs.file_datestr)

        return spec_time

    def get_spec_type(self, filename):
        """
        Gets type from filename
        :param filename:
        :return spec_type:
        """
        # Make sure filename only contains file and not larger pathname, and remove file extension
        filename = filename.split('\\')[-1].split('/')[-1].split('.')[0]

        # Extract time string from filename
        spec_type = filename.split('_')[self.spec_specs.file_type_loc]

        return spec_type

    def dark_corr_spectra(self):
        """Subtract dark spectrum from spectra"""
        if not self.dark_corrected_clear and self.clear_spec_raw is not None:
            self.clear_spec_corr = self.clear_spec_raw - self.dark_spec
            self.clear_spec_corr[self.clear_spec_corr < 0] = 0
            self.dark_corrected_clear = True

        if not self.dark_corrected_plume:
            self.plume_spec_corr = self.plume_spec_raw - self.dark_spec
            self.plume_spec_corr[self.plume_spec_corr < 0] = 0
            self.dark_corrected_plume = True

    def stray_corr_spectra(self):
        """Correct spectra for stray light - spectra are assumed to be dark-corrected prior to running this function"""
        # Set the range of stray pixels
        stray_range = np.arange(self._start_stray_pix, self._end_stray_pix + 1)

        if not self.stray_corrected_clear and self.clear_spec_corr is not None:
            self.clear_spec_corr = self.clear_spec_corr - np.mean(self.clear_spec_corr[stray_range])
            self.clear_spec_corr[self.clear_spec_corr < 0] = 0
            self.stray_corrected_clear = True

        # Correct plume spectra (assumed to already be dark subtracted)
        if not self.stray_corrected_plume:
            self.plume_spec_corr = self.plume_spec_corr - np.mean(self.plume_spec_corr[stray_range])
            self.plume_spec_corr[self.plume_spec_corr < 0] = 0
            self.stray_corrected_plume = True

    def load_ref_spec(self, pathname, species, value=None, update=True):
        """
        Load raw reference spectrum
        :param update:  bool    If False the analyser isn't updated (just use False when initiating object)
        """
        self.ref_spec[species] = np.loadtxt(pathname)

        # If no value is provided:
        # If we already have info on that species' starting value then we use that. Otherwise we set it to 0
        if value is None:
            if species in self.species_info:
                value = self.species_info[species]['value']
            else:
                value = 0

        # Add spectrum to params for ifit
        self.params.add(species, value=value, vary=True, xpath=pathname)
        if update:
            self.update_analyser()

        # Remove un-needed data above 400 nm, which may slow down processing
        idxs = np.where(self.ref_spec[species][:, 0] > 400)
        if len(idxs[0]) > 0:
            self.ref_spec[species] = self.ref_spec[species][:np.min(idxs), :]

        # Assume we have loaded a new spectrum, so set this to False - ILS has not been convolved yet
        self.ref_convolved = False

    def get_ref_spectrum(self):
        """Load in reference spectrum"""
        self.wavelengths = None  # Placeholder for wavelengths attribute which contains all wavelengths of spectra
        #
        # --------------------------------

    def conv_ref_spec(self):
        """
        Convolves reference spectrum with instument line shape (ILS)
        after first interpolating to spectrometer wavelengths
        """
        if self.wavelengths is None:
            print('No wavelength data to perform convolution')
            return

        # Need an odd sized array for convolution, so if even we omit the last pixel
        if self.ILS.size % 2 == 0:
            self.ILS = self.ILS[:-1]

        # Loop through all reference species we have loaded and resample their data to the spectrometers wavelengths
        for f in self.ref_spec_used:
            self.ref_spec_interp[f] = np.interp(self.wavelengths, self.ref_spec[f][:, 0], self.ref_spec[f][:, 1])
            self.ref_spec_conv[f] = convolve(self.ref_spec_interp[f], self.ILS)
            self.ref_spec_ppmm[f] = self.ref_spec_conv[f] * self.ppmm_conversion

        # Update bool as we have now performed this process
        self.ref_convolved = True

    def load_calibration_spectrum(self, pathname):
        """Load Calibation image for spectrometer"""
        pass

    def stretch_spectrum(self, ref_key):
        """Stretch/squeeze reference spectrum to improve fit"""
        if self.stretch == 0:
            # If no stretch, extract reference spectrum using fit window and return
            return self.ref_spec_filter[ref_key][self.fit_window_ref]
        else:
            stretch_inc = (self.stretch * self.stretch_adjuster) / self.stretch_resample  # Stretch increment for resampled spectrum

            if self.stretch < 0:
                # Generate the fit window for extraction
                # Must be larger than fit window as a squeeze requires pulling in data outside of the fit window
                if self.fit_window_ref[-1] < (len(self.wavelengths) - 50):
                    extract_window = np.arange(self.fit_window_ref[0], self.fit_window_ref[-1] + 50)
                else:
                    extract_window = np.arange(self.fit_window_ref[0], len(self.wavelengths))
            else:
                extract_window = self.fit_window_ref

            wavelengths = self.wavelengths[extract_window]
            values = self.ref_spec_filter[ref_key][extract_window]

            # Generate new arrays with 'stretch_resample' more data points
            wavelengths_resampled = np.linspace(wavelengths[0], wavelengths[-1], len(wavelengths)*self.stretch_resample)
            values_resample = np.interp(wavelengths_resampled, wavelengths, values)

            num_pts = len(wavelengths_resampled)
            wavelengths_stretch = np.zeros(num_pts)

            # Stretch wavelengths
            for i in range(num_pts):
                wavelengths_stretch[i] = wavelengths_resampled[i] + (i * stretch_inc)

            values_stretch = np.interp(self.wavelengths[self.fit_window_ref], wavelengths_stretch, values_resample)

            return values_stretch

    def load_spec(self):
        """Load spectrum"""
        pass

    def load_dark_spec(self, dark_spec_path):
        """Loads dark spectrum"""
        filename, ext = os.path.splitext(dark_spec_path)
        if ext == '.npy':
            wavelengths, spectrum = np.load(dark_spec_path)
        elif ext == '.txt':
            wavelengths, spectrum = np.load(dark_spec_path)
        else:
            print('Unrecognised file type for loading clear spectrum')
            return
        self.wavelengths = wavelengths
        self.dark_spec = spectrum

        # Add dark spectrum to current dark_list
        filename = os.path.split(filename)[-1].split('.')[0]
        ss_str = filename.split('_')[self.spec_specs.file_ss_loc]
        ss = int(ss_str.replace(self.spec_specs.file_ss.replace('{}', ''), ''))

        # Update dark dictionary
        if ss in self.dark_dict.keys():
            print('New dark spectrum for integration time {} overwriting previous in dictionary'.format(ss))
        self.dark_dict[ss] = self.dark_spec

    def load_dark_coadd(self, dark_spec_path):
        """Load drk images -> co-add to generate single dark image"""
        pass

    def load_clear_spec(self, clear_spec_path):
        """Loads clear spectrum"""
        filename, ext = os.path.splitext(clear_spec_path)
        if ext == '.npy':
            wavelengths, spectrum = np.load(clear_spec_path)
        elif ext == '.txt':
            wavelengths, spectrum = np.load(clear_spec_path)
        else:
            print('Unrecognised file type for loading clear spectrum')
            return
        self.wavelengths = wavelengths
        self.clear_spec_raw = spectrum

    def load_dir(self, spec_dir=None, prompt=True, plot=True):
        """Load spectrum directory
        :param spec_dir str     If provided this is the path to the spectra directory. prompt is ignored if spec_dir is
                                specified.
        :param: prompt  bool    If true, a dialogue box is opened to request directory load. Else, self.spec_dir is used
        :param: plot    bool    If true, the first spectra are plotted in the GUI"""

        if spec_dir is not None:
            self.spec_dir = spec_dir
        elif prompt:
            spec_dir = filedialog.askdirectory(title='Select spectrum sequence directory', initialdir=self.spec_dir)

            if len(spec_dir) > 0 and os.path.exists(spec_dir):
                self.spec_dir = spec_dir
            else:
                raise ValueError('Spectrum directory not recognised: {}'.format(spec_dir))
        else:
            if self.spec_dir is None:
                raise ValueError('Spectrum directory not recognised: {}'.format(self.spec_dir))

        # Update first_spec flag TODO possibly not used in DOASWorker, check
        self.first_spec = True

        # Get list of all files in directory
        self.spec_dict = self.get_spec_list()

        # Set current spectra to first in lists
        if len(self.spec_dict['clear']) > 0:
            self.wavelengths, self.clear_spec_raw = load_spectrum(os.path.join(self.spec_dir,
                                                                               self.spec_dict['clear'][0]))
        if len(self.spec_dict['plume']) > 0:
            self.wavelengths, self.plume_spec_raw = load_spectrum(os.path.join(self.spec_dir,
                                                                               self.spec_dict['plume'][0]))
            self.spec_time = self.get_spec_time(self.spec_dict['plume'][0])
        if len(self.spec_dict['dark']) > 0:
            ss_id = self.spec_specs.file_ss.replace('{}', '')
            ss = self.spec_dict['plume'][0].split('_')[self.spec_specs.file_ss_loc].replace(ss_id, '')

            # Try to find dark spectrum in current directory - if we can't, we revert to set dark directory
            self.dark_spec = self.find_dark_spectrum(self.spec_dir, ss)
            if self.dark_spec is None:
                self.dark_spec = self.find_dark_spectrum(self.dark_dir, ss)
                if self.dark_spec is None:
                    print('No dark spectrum could be found in the current spectrum directory or current dark directory')

        # Update plots if requested
        if plot:
            self.fig_spec.update_clear()
            self.fig_spec.update_dark()
            self.fig_spec.update_plume()

    def get_spec_list(self):
        """
        Gets list of spectra files from current spec_dir
        Assumes all .npy files in the directory are spectra...
        :returns: sd    dict    Dictionary containing all filenames
        """
        # Setup empty dictionary sd
        sd = {}

        # Get all files into associated list/dictionary entry
        sd['all'] = [f for f in os.listdir(self.spec_dir) if self.spec_specs.file_ext in f]
        sd['all'].sort()
        sd['plume'] = [f for f in sd['all']
                       if self.spec_specs.file_type['meas'] + self.spec_specs.file_ext in f]
        sd['plume'].sort()
        sd['clear'] = [f for f in sd['all']
                       if self.spec_specs.file_type['clear'] + self.spec_specs.file_ext in f]
        sd['clear'].sort()
        sd['dark'] = [f for f in sd['all']
                      if self.spec_specs.file_type['dark'] + self.spec_specs.file_ext in f]
        sd['dark'].sort()
        return sd

    def find_dark_spectrum(self, spec_dir, ss):
        """
        Searches for suitable dark spectrum in designated directory by finding one with the same shutter speed as
        passed to function.
        :return: dark_spec
        """
        # Ensure ss is an integer
        ss = int(ss)

        # Fast dictionary look up for preloaded dark spectra
        if ss in self.dark_dict.keys():
            dark_spec = self.dark_dict[ss]
            return dark_spec

        # List all dark images in directory
        dark_list = [f for f in os.listdir(spec_dir)
                     if self.spec_specs.file_type['dark'] in f and self.spec_specs.file_ext in f]

        # Extract ss from each image and round to 2 significant figures
        ss_str = self.spec_specs.file_ss.replace('{}', '')
        ss_list = [int(f.split('_')[self.spec_specs.file_ss_loc].replace(ss_str, '')) for f in dark_list]

        ss_idx = [i for i, x in enumerate(ss_list) if x == ss]
        ss_spectra = [dark_list[i] for i in ss_idx]

        if len(ss_spectra) < 1:
            return None

        # If we have images, we loop through them to create a coadded image
        dark_full = np.zeros([self.spec_specs.pix_num, len(ss_spectra)])
        for i, ss_spectrum in enumerate(ss_spectra):
            # Load image. Coadd.
            wavelengths, dark_full[:, i] = load_spectrum(os.path.join(spec_dir, ss_spectrum))

        # Coadd images to creat single image
        dark_spec = np.mean(dark_full, axis=1)

        # Update lookup dictionary for fast retrieval of dark image later
        self.dark_dict[ss] = dark_spec

        return dark_spec

    def save_dark(self, filename):
        """Save dark spectrum"""
        if self.wavelengths is None or self.dark_spec is None:
            raise ValueError('One or both attributes are NoneType. Cannot save.')
        if len(self.wavelengths) != len(self.dark_spec):
            raise ValueError('Arrays are not the same length. Cannot save.')

        np.savetxt(filename, np.transpose([self.wavelengths, self.dark_spec]),
                   header='Dark spectrum\nWavelength [nm]\tIntensity [DN]')

    def save_clear_raw(self, filename):
        """Save clear spectrum"""
        if self.wavelengths is None or self.clear_spec_raw is None:
            raise ValueError('One or both attributes are NoneType. Cannot save.')
        if len(self.wavelengths) != len(self.clear_spec_raw):
            raise ValueError('Arrays are not the same length. Cannot save.')

        np.savetxt(filename, np.transpose([self.wavelengths, self.clear_spec_raw]),
                   header='Raw clear (Fraunhofer) spectrum\n'
                          '-Not dark-corrected\nWavelength [nm]\tIntensity [DN]')

    def save_plume_raw(self, filename):
        if self.wavelengths is None or self.plume_spec_raw is None:
            raise ValueError('One or both attributes are NoneType. Cannot save.')
        if len(self.wavelengths) != len(self.plume_spec_raw):
            raise ValueError('Arrays are not the same length. Cannot save.')

        np.savetxt(filename, np.transpose([self.wavelengths, self.plume_spec_raw]),
                   header='Raw in-plume spectrum\n'
                          '-Not dark-corrected\nWavelength [nm]\tIntensity [DN]')

    def process_doas(self):
        """Handles the order of DOAS processing"""
        # Check we have all of the correct spectra to perform processing
        if self.plume_spec_raw is None or self.wavelengths is None:
            raise SpectraError('Require plume spectra for iFit processing')

        if self.ref_spec_types[0] not in self.ref_spec.keys():
            raise SpectraError('No SO2 reference spectrum present for processing')

        # If the stray region hasn't been converted to pixel space we just set it to itself to implement pixel mapping
        if self._start_stray_pix is None or self._end_stray_pix is None:
            self.start_stray_wave = self.start_stray_wave
            self.end_stray_wave = self.end_stray_wave

        if not self.dark_corrected_plume:
            if self.dark_spec is None:
                print('Warning! No dark spectrum present, processing without dark subtraction')

                # Set raw spectra to the corrected spectra, ignoring that they have not been dark corrected
                self.plume_spec_corr = self.plume_spec_raw
            else:
                self.dark_corr_spectra()

        # Correct spectra for stray light
        if not self.stray_corrected_plume:
            self.stray_corr_spectra()

        # Run processing
        #print('IFit worker: Running fit')
        fit_0 = self.analyser.fit_spectrum([self.wavelengths, self.plume_spec_corr], calc_od=self.ref_spec_used,
                                           fit_window=[self.start_fit_wave, self.end_fit_wave])
        #print('IFit worker: Finished fit')
        self.applied_ld_correction = False

        # Update wavelengths
        self.wavelengths_cut = fit_0.grid

        # If we have light dilution correction we run it here
        if self.corr_light_dilution:
            print('Performing iFit light dilution correction...')
            if False in [isinstance(x, np.ndarray) for x in
                         [self.ifit_so2_0, self.ifit_err_1, self.ifit_so2_1, self.ifit_err_1]]:
                print('{} SO2 grids not found for light dilution correction. '
                      'Use load_ld_lookup() or light_dilution_cure_generator()'.format(str(self.__class__).split()[-1]))
                self.ldf_best = np.nan
            else:
                # Perform full hunt for LDF if the last LDF lookup was beyond the specified time for recal or we don't
                # have a previous ldf. If last point was NaN we also try a recalibration as NaN may mean the previous
                # spectrum didn't have enough information to calculate LDF (e.g. it may have been free of SO2)
                if self.spec_time_last_ld is None or np.isnan(self.ldf_best) or \
                        self.spec_time - self.spec_time_last_ld >= datetime.timedelta(minutes=self.recal_ld_mins):

                    # Process second fit window
                    fit_1 = self.analyser.fit_spectrum([self.wavelengths, self.plume_spec_corr],
                                                       calc_od=self.ref_spec_used,
                                                       fit_window=[self.start_fit_wave_2, self.end_fit_wave_2])
                    self.fit_0_uncorr = fit_0   # Save uncorrected fits as attributes
                    self.fit_1_uncorr = fit_1
                    df_lookup, df_refit, fit_0, fit_1 = self.ld_lookup(so2_dat=(fit_0.params['SO2'].fit_val,
                                                                                fit_1.params['SO2'].fit_val),
                                                                       so2_dat_err=(fit_0.params['SO2'].fit_err,
                                                                                    fit_1.params['SO2'].fit_err),
                                                                       wavelengths=self.wavelengths,
                                                                       spectra=self.plume_spec_corr,
                                                                       spec_time=self.spec_time)
                    print('Fit val uncorrected: {}'.format(fit_0.params['SO2'].fit_val))
                    print('Fit val corrected: {}'.format(df_lookup['SO2'][0]))
                    # Update SO2 value for the best SO2 value we have
                    if not np.isnan(df_lookup['SO2'][0]):
                        fit_0.params['SO2'].fit_val = df_lookup['SO2'][0]

                    self.fit_0 = fit_0  # Save corrected fits
                    self.fit_1 = fit_1
                    self.applied_ld_correction = True
                    self.spec_time_last_ld = self.spec_time     # Set this spec time to last ld calculation spec time

                else:
                    # TODO I don't think this alone works - the ldf doesn't seem to change the fit result
                    # TODO instead i will still need to run the LD lookup to find so2_best which is the column density value we
                    # TODO are interested in.
                    fit_0, fit_1 = self.ldf_refit(self.wavelengths, self.plume_spec_corr, self.ldf_best)

        else:
            self.ldf_best = np.nan

        # Unpack results (either from light dilution corrected or not)
        for species in self.ref_spec_used:
            self.column_density[species] = fit_0.params[species].fit_val
            self.abs_spec_species[species] = fit_0.meas_od[species]
            self.ref_spec_fit[species] = fit_0.synth_od[species]
            self.fit_errs[species] = fit_0.params[species].fit_err
        self.abs_spec_species['Total'] = fit_0.spec
        self.ref_spec_fit['Total'] = fit_0.fit
        self.abs_spec_species['residual'] = fit_0.resid
        self.std_err = fit_0.params['SO2'].fit_err   # RMSE from residual

        # Set flag defining that data has been fully processed
        self.processed_data = True
        self.fit = fit_0     # Save fit in attribute

    def process_dir(self, spec_dir=None, plot=False):
        """
        Processes the current directory, or the directory specified by spec_dir
        :param spec_dir:
        :return:
        """
        if spec_dir is not None:
            self.load_dir(spec_dir=spec_dir, plot=plot)

        cds = []
        times = []

        # Loop through directory plume images processing them
        for i in range(len(self.spec_dict['plume'])):
            print('Processing spectrum {} of {}'.format(i+1, len(self.spec_dict['plume'])))

            self.wavelengths, self.plume_spec_raw = load_spectrum(os.path.join(self.spec_dir,
                                                                               self.spec_dict['plume'][i]))
            self.spec_time = self.get_spec_time(self.spec_dict['plume'][i])

            # Get dark spectrum
            ss_id = self.spec_specs.file_ss.replace('{}', '')
            ss = self.spec_dict['plume'][i].split('_')[self.spec_specs.file_ss_loc].replace(ss_id, '')
            self.dark_spec = self.find_dark_spectrum(self.dark_dir, ss)

            # Process spectrum
            self.process_doas()

            # Update results
            times.append(self.spec_time)
            cds.append(self.column_density['SO2'])

        for cd in cds:
            print('CDs (ppmm): {:.0f}'.format(np.array(cd) / self.ppmm_conv))

        # Save results
        self.save_results(save_all=True)

    def reset_doas_results(self):
        """Makes empty doas results object"""
        self.results = DoasResults([], index=[], fit_errs=[], species_id='SO2')
        self.results.ldfs = []

    def add_doas_results(self, doas_dict):
        """
        Add column densities to DoasResults object, which should be already created
        Also controls removal of old doas points, if we wish to
        :param doas_dict:   dict       Containing at least keys 'column_density' and 'time'
        """
        cd = doas_dict['column_density']['SO2']
        try:
            cd_err = doas_dict['std_err']
        except KeyError:
            cd_err = np.nan

        # Faster append method - seems to work
        self.results.loc[doas_dict['time']] = cd
        if isinstance(self.results.fit_errs, list):
            self.results.fit_errs.append(cd_err)
        elif isinstance(self.results.fit_errs, np.ndarray):
            self.results.fit_errs = np.append(self.results.fit_errs, cd_err)
        else:
            print('ERROR! Unrecognised datatype for ifit fit errors')

        # Light dilution
        try:
            ldf = doas_dict['LDF']
        except KeyError:
            ldf = np.nan

        # If there is no ldf attribute, fill it with nans and then set the most recent value to LDF
        if not hasattr(self.results, 'ldfs'):
            self.results.ldfs = [np.nan] * len(self.results.fit_errs)
            self.results.ldfs[-1] = ldf
        elif isinstance(self.results.ldfs, list):
            self.results.ldfs.append(ldf)
        elif isinstance(self.results.ldfs, np.ndarray):
            self.results.ldfs = np.append(self.results.ldfs, ldf)

    def rem_doas_results(self, time_obj, inplace=False):
        """
        Remove DOAS result values from time series
        :param time_obj:  datetime.datetime     Any value equal to or earlier than this time will be removed
        :param inplace:     bool
                            If False, the new cut array is returned. If True, the self.results array is overwritten
                            to contain the new cut data
        :return:
        """
        indices = [x for x in self.results.index if x < time_obj]
        fit_err_idxs = self.results.index >= time_obj
        fit_errs = np.array(list(compress(self.results.fit_errs, fit_err_idxs)))
        ldfs = np.array(list(compress(self.results.ldfs, fit_err_idxs)))
        if inplace:
            self.results.drop(indices, inplace=True)
            self.results.fit_errs = fit_errs
            self.results.ldfs = ldfs
            results = None
        else:
            results = DoasResults(self.results.drop(indices, inplace=False))
            results.fit_errs = fit_errs
            results.ldfs = ldfs

        return results

    def make_doas_results(self, times, column_densities, stds=None, ldfs=None, species='SO2'):
        """
        Makes pydoas DOASResults object from timeseries
        :param times:   arraylike           Datetimes of column densities
        :param column_densities: arraylike  Column densities
        :param stds:    arraylike           Standard errors in the column density values
        :param ldfs:    array-like          Light dilution factors
        :param species: str                 Gas species
        """
        # If we have a low value we can assume it is in ppm.m (arbitrary deifnition but it should hold for almost every
        # case). So then we need to convert to molecules/cm
        doas_results = DoasResults(column_densities, index=times, fit_errs=stds, species_id=species)

        # Created ldfs attribute which holds light dilution factors
        if ldfs is not None:
            doas_results.ldfs = ldfs
        else:
            doas_results.ldfs = [np.nan] * len(stds)
        return doas_results

    def save_results(self, pathname=None, start_time=None, end_time=None, save_all=False):
        """Saves doas results"""
        if len(self.results) < 1:
            print('No DOAS results to save')
            return

        # Extract data from specific index if we are given a start time
        if start_time is not None:
            idx_start = np.argmin(np.abs(self.results.index - start_time))
        else:
            if save_all:
                idx_start = 0
            else:
                # Go back to the last hour (as data will already be saved up to there)
                current_time = self.results.index[-1]
                if current_time.minute == 0 and current_time.second == 0:
                    new_hour = current_time.hour - 1
                    start_time == current_time.replace(hour=new_hour)
                else:
                    start_time = current_time.replace(minute=0, second=0)
                idx_start = np.argmin(np.abs(self.results.index - start_time))

        if end_time is not None:
            idx_end = np.argmin(np.abs(self.results.index - end_time)) + 1
        else:
            idx_end = None      # -1 doesn't work as it cuts the last item out - use None to go right to end of list

        # Generate pathname
        if pathname is None:
            start_time_str = datetime.datetime.strftime(self.results.index[0], self.save_date_fmt)
            end_time_str = datetime.datetime.strftime(self.results.index[-1], self.save_date_fmt).split('T')[-1]
            filename = 'doas_results_{}_{}.csv'.format(start_time_str, end_time_str)

            subdir = 'Processed_{}_spec'
            i = 1
            while os.path.exists(os.path.join(self.spec_dir, subdir.format(i))):
                i += 1
            pathname = os.path.join(self.spec_dir, subdir.format(i))
            os.mkdir(pathname)
            pathname = os.path.join(pathname, filename)

        # Create full database to save
        frame = {'Time': pd.Series(self.results.index[idx_start:idx_end]),
                 'Column density': pd.Series(self.results.values[idx_start:idx_end]),
                 'CD error': pd.Series(self.results.fit_errs[idx_start:idx_end]),
                 'LDF': pd.Series(self.results.ldfs[idx_start:idx_end])}
        df = pd.DataFrame(frame)

        # self.results[idx_start:idx_end].to_csv(pathname)
        df.to_csv(pathname)
        print('DOAS results saved: {}'.format(pathname))
        pathname = pathname.replace('.csv', '.txt')
        with open(pathname, 'w') as f:
            f.write('DOAS processing parameters\n')
            f.write('Stray range={}:{}\n'.format(self.start_stray_wave, self.end_stray_wave))
            f.write('Fit window={}:{}\n'.format(self.start_fit_wave, self.end_fit_wave))
            f.write('Light dilution correction={}\n'.format(self.corr_light_dilution))
            f.write('Light dilution recal time [mins]={}\n'.format(self.recal_ld_mins))

    def load_results(self, filename=None, plot=True):
        """
        Loads DOAS results from csv file
        :param filename:    str     Full path to file to be loaded. If None, a prompt is given to select file
        :param plot:        bool    If True, attempt to update time series plot of results
        """
        if filename is None:
            filename = filedialog.askopenfilename(title='Select DOAS results file',
                                                  initialdir=self.spec_dir, filetypes=(('csv', '*.csv'),
                                                                                       ('All files', '*.*')))
            if not filename:
                return

        # Load results
        df = pd.read_csv(filename)

        # Reset results
        self.reset_self()

        # Unpack results into DoasResults object
        res = df['Column density'].astype(float).squeeze()
        try:
            res.index = [datetime.datetime.strptime(x, '%Y-%m-%d %H:%M:%S') for x in df['Time']]
        except Exception:
            res.index = [datetime.datetime.strptime(x, '%d/%m/%Y %H:%M:%S') for x in df['Time']]
        self.results = DoasResults(res, species_id='SO2')
        self.results.fit_errs = df['CD error'].astype(float)
        self.results.ldfs = df['LDF'].astype(float)

        # Plot results if requested
        if plot:
            if self.fig_series is not None:
                self.fig_series.update_plot()

    def start_processing_threadless(self, spec_dir=None):
        """
        Process spectra already in a directory, without entering a thread - this means that the _process_loop
        function can update the plots wihtout going through the main thread in PyplisWorker
        """
        if spec_dir is not None:
            self.spec_dir = spec_dir

        # Flag that we are running processing outside of thread
        self.processing_in_thread = False

        # Reset self
        self.reset_self()

        # Get all files
        spec_files = [f for f in os.listdir(self.spec_dir) if '.npy' in f]

        # Sort files alphabetically (which will sort them by time due to file format)
        spec_files.sort()

        # Extract clear spectra if they exist. If not, the first file is assumed to be the clear spectrum
        clear_spec = [f for f in spec_files if self.spec_specs.file_type['clear'] + '.npy' in f]
        plume_spec = [f for f in spec_files if self.spec_specs.file_type['meas'] + '.npy' in f]

        # Loop through all files and add them to queue
        for file in clear_spec:
            self.q_spec.put(os.path.join(self.spec_dir, file))
        for file in plume_spec:
            self.q_spec.put(os.path.join(self.spec_dir, file))

        # Add the exit flag at the end, to ensure that the process_loop doesn't get stuck waiting on the queue forever
        self.q_spec.put(self.STOP_FLAG)

        # Begin processing
        self._process_loop(continuous_save=False)

    def start_processing_thread(self):
        """Public access thread starter for _processing"""
        # Reset self
        self.reset_self()

        self.processing_in_thread = True
        self.process_thread = threading.Thread(target=self._process_loop, args=())
        self.process_thread.daemon = True
        self.process_thread.start()

    def _process_loop(self, continuous_save=True):
        """
        Main process loop for doas
        :param continuous_save: bool    If True, data is saved every hour, otherwise all data is saved at the end of
            processing. Continuous save is set to False when post-processing DOAS in a non-continuous manner, just
            processing a single directory. May always want continuous save though, so think about if I want this...
        :return:
        """
        print('IFit worker: Entering new processing loop')
        # Setup which we don't need to repeat once in the loop (optimising the code a little)
        ss_str = self.spec_specs.file_ss.replace('{}', '')

        while True:
            # See if we are wanting an early exit
            try:
                ans = self.q_stop.get(block=False)
                if ans == self.STOP_FLAG:
                    break
            except queue.Empty:
                pass

            # Blocking wait for new file
            pathname = self.q_spec.get(block=True)
            print('IFit worker processing thread: got new file: {}'.format(pathname))

            # Close thread if requested with 'exit' command
            if pathname == self.STOP_FLAG:
                # Update plot at end if we haven't been updating it as we go - this should speed up processing as plotting takes a long time
                if self.fig_series is not None and not self.plot_iter:
                    self.fig_series.update_plot()
                if continuous_save:
                    self.save_results(end_time=self.spec_time)
                else:
                    self.save_results(save_all=True)
                break

            spec_type = self.get_spec_type(pathname)

            if spec_type == self.spec_specs.file_type['meas'] or spec_type == self.spec_specs.file_type['test'] or \
                    self.spec_specs.file_type['cal'] in spec_type:
                #print('IFitWorker: processing spectrum: {}'.format(pathname))
                # Extract filename and create datetime object of spectrum time
                working_dir, filename = os.path.split(pathname)
                self.spec_dir = working_dir     # Update working directory to where most recent file has come from
                self.spec_time = self.get_spec_time(filename)

                # Extract shutter speed
                ss_full_str = filename.split('_')[self.spec_specs.file_ss_loc]
                ss = int(ss_full_str.replace(ss_str, ''))

                # Find dark spectrum with same shutter speed
                self.dark_spec = self.find_dark_spectrum(self.dark_dir, ss)

                # Load spectrum
                self.wavelengths, self.plume_spec_raw = load_spectrum(pathname)

                # Update spectra as soon as we have acquired them (don't wait to process as this may take time)
                if self.fig_spec is not None:
                    self.fig_spec.update_dark()
                    self.fig_spec.update_plume()

                time_1 = time.time()
                self.process_doas()
                print('IFit Worker: Time taken to process {}: {}'.format(pathname, time.time() - time_1))

                # Gather all relevant information and spectra and pass it to PyplisWorker
                processed_dict = {'processed': True,             # Flag whether this is a complete, processed dictionary
                                  'time': self.spec_time,
                                  'filename': pathname,             # Filename of processed spectrum
                                  'dark': self.dark_spec,           # Dark spectrum used (raw)
                                  'clear': self.clear_spec_raw,     # Clear spectrum used (raw)
                                  'plume': self.plume_spec_raw,     # Plume spectrum used (raw)
                                  'abs': self.abs_spec_species,     # Absorption spectrum
                                  'ref': self.ref_spec_fit,         # Reference spectra (scaled)
                                  'std_err': self.std_err,          # Standard error of fit
                                  'LDF': self.ldf_best,              # Light dilution factor
                                  'column_density': self.column_density}    # Column density

                # Update results object
                self.add_doas_results(processed_dict)

                # Pass data dictionary to PyplisWorker queue (might not need to do this if I hold all the data here
                if self.processing_in_thread:
                    self.q_doas.put(processed_dict)

                # Update doas plot
                if self.fig_doas is not None:
                    self.fig_doas.update_plot()
                if self.fig_series is not None and self.plot_iter:
                    self.fig_series.update_plot()

                # Save all results if we are on the 0 or 30th minute of the hour
                if continuous_save and self.spec_time.second == 0 and self.spec_time.minute in self.save_freq:
                    self.save_results(end_time=self.spec_time)

                #print('IFit worker: Processed file: {}'.format(filename))

            elif spec_type == self.spec_specs.file_type['dark']:
                print('IFitWorker: got dark spectrum: {}'.format(pathname))
                # If dark spectrum, we load it into
                self.load_dark_spec(pathname)
                self.fig_spec.update_dark()
            elif spec_type == self.spec_specs.file_type['clear']:
                print('IFitWorker: got dark spectrum: {}'.format(pathname))
                # If dark spectrum, we load it into
                self.load_clear_spec(pathname)
                self.fig_spec.update_clear()
            else:
                print('IFitWorker: spectrum type not recognised: {}'.format(pathname))

    def stop_sequence_processing(self):
        """Stops processing a sequence"""
        self.q_stop.put(self.STOP_FLAG)

    def start_watching(self, directory, recursive=True):
        """
        Setup directory watcher for images - note this is not for watching spectra - use DOASWorker for that
        Also starts a processing thread, so that the images which arrive can be processed
        """
        if self.watching:
            print('IFit worker: Already watching for spectra: {}'.format(self.watching_dir))
            print('IFit worker: Please stop watcher before attempting to start new watch. '
                  'This isssue may be caused by having manual acquisitions running alongside continuous watching')
            return
        self.watcher = create_dir_watcher(directory, recursive, self.directory_watch_handler)
        self.watcher.start()
        self.watching_dir = directory
        self.watching = True
        print('IFit worker: Watching {} for new spectra'.format(self.watching_dir[-30:]))

        # Start the processing thread
        self.start_processing_thread()

    def stop_watching(self):
        """Stop directory watcher and end processing thread"""
        if self.watching:
            if self.watcher is not None:
                self.watcher.stop()
                print('Stopped watching {} for new images'.format(self.watching_dir[-30:]))
                self.watching = False

                # Stop processing thread when we stop watching the directory
                self.q_spec.put(self.STOP_FLAG)
            else:
                print('No directory watcher to stop')
        else:
            print('IFit worker: Watching was already stopped')

    def directory_watch_handler(self, pathname, t):
        """Handles new spectra passed from watcher"""
        _, ext = os.path.splitext(pathname)
        if ext != self.spec_specs.file_ext:
            return

        # Wait until lockfile is removed
        pathname_lock = pathname.replace(ext, '.lock')
        while os.path.exists(pathname_lock):
            pass

        print('IFit worker: New file found {}'.format(pathname))
        # Pass path to queue
        self.q_spec.put(pathname)

    # =================================================
    # IFIT functions
    # =================================================
    def update_analyser(self):
        """
        Updates ifit analyser
        This should be called everytime the fit window is changed.
        Instrument is stray-light corrected prior to analyser to ignore this flag now
        NOTE: I cannot just adjust the fit_window attribute as the model is generated
        """
        # TODO perhaps requst ben adds an update fit window func to update the analyser without creating a new object
        if self.ils_path is not None:
            self.analyser = Analyser(params=self.params,
                                     fit_window=[self._start_fit_wave_init, self._end_fit_wave_init],
                                     frs_path=self.frs_path,
                                     stray_flag=False,      # We stray correct prior to passing spectrum to Analyser
                                     dark_flag=False,
                                     ils_type='File',
                                     ils_path=self.ils_path)
        else:
            self.analyser = Analyser(params=self.params,
                                     fit_window=[self._start_fit_wave_init, self._end_fit_wave_init],
                                     frs_path=self.frs_path,
                                     stray_flag=False,      # We stray correct prior to passing spectrum to Analyser
                                     dark_flag=False)

    def load_ils(self, ils_path):
        """Load ils from path"""
        self.analyser = Analyser(params=self.params,
                                 fit_window=[self._start_fit_wave_init, self._end_fit_wave_init],
                                 frs_path=self.frs_path,
                                 stray_flag=False,      # We stray correct prior to passing spectrum to Analyser
                                 dark_flag=False,
                                 ils_type='File',
                                 ils_path=ils_path)

        self.ils_path = ils_path

        # Load ILS
        self.ILS_wavelengths, self.ILS = np.loadtxt(ils_path, unpack=True)

        # Force updating of ld analysers to use new ILS
        self.update_ld_analysers(force_both=True)
        print('Updating ILS. Light dilution analysers will be updated. This could cause issues if lookup grids'
              'were generated with a different ILS. Please ensure ILS represents the lookup grids currently in use')

    def update_ils(self):
        """Updates ILS in Analyser object for iFit (code taken from __init__ of Analyser, this should work..."""
        grid_ils = np.arange(self.ILS_wavelengths[0], self.ILS_wavelengths[-1], self.analyser.model_spacing)
        ils = griddata(self.ILS_wavelengths, self.ILS, grid_ils, 'cubic')
        self.analyser.ils = ils / np.sum(ils)
        self.analyser.generate_ils = False

    def update_ld_analysers(self, force_both=False):
        """
        Updates the 2 fit window analysers based on current it window definitions. This could be invoked by a button to
        prevent it being run every time the fit window is change, saving time - preventing lag in the GUI. Should only
        need to update this every time the light dilution curve generator is run, so could do it then
        :return:
        """
        # Only create new objects if the old ones aren't the as expected
        if force_both or self.analyser0 is None or self.start_fit_wave != self.analyser0.fit_window[0] \
                or self.end_fit_wave != self.analyser0.fit_window[1]:
            self.analyser0 = Analyser(params=self.params, fit_window=[self.start_fit_wave, self.end_fit_wave],
                                 frs_path=self.frs_path, stray_flag=False, dark_flag=False, ils_type='File',
                                 ils_path=self.ils_path)

        if force_both or self.analyser1 is None or self.start_fit_wave_2 != self.analyser1.fit_window[0] \
                or self.end_fit_wave_2 != self.analyser1.fit_window[1]:
            self.analyser1 = Analyser(params=self.params, fit_window=[self.start_fit_wave_2, self.end_fit_wave_2],
                                 frs_path=self.frs_path, stray_flag=False, dark_flag=False, ils_type='File',
                                 ils_path=self.ils_path)

    def update_grid(self, max_ppmm, increment=20):
        """Updates ppmm resolution for light dilution lookup grid"""
        self.grid_max_ppmm = max_ppmm
        self.grid_increment_ppmm = increment
        new_so2_grid_ppmm = np.arange(0, max_ppmm, increment)
        if not np.array_equal(self.so2_grid_ppmm, new_so2_grid_ppmm):
            print('Changing SO2 grid for light dilution correction. If preloading grids, ensure both match this grid.')
        self.so2_grid_ppmm = new_so2_grid_ppmm
        np.multiply(self.so2_grid_ppmm, 2.652e+15)

    def light_diluiton_curve_generator(self, wavelengths, spec, spec_date=datetime.datetime.now(), is_corr=True):
        """
        Generates light dilution curves from the clear spectrum it is passed. Taken from Varnam et al. (2020)
        Code in ld_curve_generator.py on light_dilution branch of ifit.
        Lookup tables are saved to a file based on fit_window and spec_date. NOTE this will overwrite any file generated
        from the same clear spectrum with the same fit window - it will not notify the user if it is overwriting a file
        :param wavelengths:     np.array    Array of wavelengths
        :param spec:            np.array    Clear spectrum intensity. Intensities should already be corrected for
                                            stray-light and dark current if is_corr is True
        :param spec_date:       datetime    Clear spectrum acquisition time - used for saving lookup table
        :param is_corr:         bool        Flags of clear spectrum is corrected for stray light and dark current.
                                            If False, the correction is applied here
        """
        if not is_corr:
            self.clear_spec_raw = spec

            # Ensure we have updated the pixel space of for stray light window - simply setting property runs update
            self.start_stray_wave = self.start_stray_wave
            self.end_stray_wave = self.end_stray_wave

            # Correct clear spectrum and then reassign it to spec
            self.dark_corr_spectra()
            self.stray_corr_spectra()
            spec = self.clear_spec_corr

        self.update_ld_analysers()      # Update light dilution analysers if the fit windows have been changed

        # Create generator that encompasses full fit window
        pad = 1
        analyser_prm = Analyser(params=self.params,
                                fit_window=[self.start_fit_wave - pad, self.end_fit_wave_2 + pad],
                                frs_path=self.frs_path,
                                stray_flag=False,      # We stray correct prior to passing spectrum to Analyser
                                dark_flag=False,
                                ils_type='File',
                                ils_path=self.ils_path)
        fit_prm = analyser_prm.fit_spectrum([wavelengths, spec], calc_od=['SO2', 'Ring', 'O3'])

        # ==============================================================================================================
        # Create suit of synthetic spectra
        # ==============================================================================================================
        analyser_ld = Analyser_ld(params=self.params,
                                  fit_window=[self.start_fit_wave - pad, self.end_fit_wave_2 + pad],
                                  frs_path=self.frs_path,
                                  stray_flag=False,      # We stray correct prior to passing spectrum to Analyser
                                  dark_flag=False,
                                  ils_type='File',
                                  ils_path=self.ils_path)
        analyser_ld.params.update_values(fit_prm.params.popt_list())
        analyser_ld.params.add('LDF', value=0.0, vary=True)
        analyser_ld.interp_method = 'cubic'

        # Use shape of grid and so2 value to produce a single array
        shape = (len(self.so2_grid), len(fit_prm.spec), len(self.ldf_grid))
        spectra_suite = np.zeros(shape)

        # Create synthetic spectra by updating parameters
        print('Creating synthetic spectra with forward model')
        for i, so2 in enumerate(self.so2_grid):
            for j, ldf in enumerate(self.ldf_grid):

                # Update parameters of synthetic spectra to generate
                analyser_ld.params['SO2'].set(value=so2)
                analyser_ld.params['LDF'].set(value=ldf)

                # Extract parameter list
                fit_params = analyser_ld.params.fittedvalueslist()

                # Create synthetic spectrum
                spectra_suite[i, ..., j] = analyser_ld.fwd_model(fit_prm.grid, *fit_params)

        # =============================================================================
        # Analyse spectra in first waveband
        # =============================================================================
        print('Analyse synthetic spectra in waveband 1')
        print('---------------------------------------')

        # Create arrays to store answers
        ifit_so2_0 = np.zeros((shape[0], shape[2]))
        ifit_err_0 = np.zeros((shape[0], shape[2]))

        # Loop through each synthetic spectrum
        for i, so2 in enumerate(self.so2_grid):
            print('Fitting SO2 value: {}'.format('SO2'))
            for j, ldf in enumerate(self.ldf_grid):

                #Extract syntheteic spectrum for suite of spectra
                spectrum = [fit_prm.grid, spectra_suite[i, ..., j]]

                # Analyse spectrum
                fit = self.analyser0.fit_spectrum(spectrum, calc_od=['SO2', 'Ring', 'O3'])

                # Store SO2 fit parameters in array
                ifit_so2_0[i, j] = fit.params['SO2'].fit_val
                ifit_err_0[i, j] = fit.params['SO2'].fit_err

        #Create new ifit_so2 with units in ppm.m
        ifit_so2_ppmm0 = np.divide(ifit_so2_0, 2.652e15)
        ifit_err_ppmm0 = np.divide(ifit_err_0, 2.652e15)

        # =============================================================================
        # Analyse spectra in second waveband
        # =============================================================================
        print('Analyse synthetic spectra in waveband 2')
        print('---------------------------------------')

        # Create arrays to store answers
        ifit_so2_1 = np.zeros((shape[0],shape[2]))
        ifit_err_1 = np.zeros((shape[0],shape[2]))

        # Loop through each synthetic spectrum
        for i, so2 in enumerate(self.so2_grid):
            print('Fitting SO2 value: {}'.format('SO2'))
            for j, ldf in enumerate(self.ldf_grid):

                #Extract syntheteic spectrum for suite of spectra
                spectrum = [fit_prm.grid, spectra_suite[i, ..., j]]

                fit = self.analyser1.fit_spectrum(spectrum, calc_od=['SO2', 'Ring', 'O3'])

                # Store SO2 fit parameters in array
                ifit_so2_1[i, j] = fit.params['SO2'].fit_val
                ifit_err_1[i, j] = fit.params['SO2'].fit_err

        #Create new ifit_so2 with units in ppm.m
        ifit_so2_ppmm1 = np.divide(ifit_so2_1, 2.652e15)
        ifit_err_ppmm1 = np.divide(ifit_err_1, 2.652e15)

        # Store lookup tables as attributes
        self.ifit_so2_0, self.ifit_err_0 = ifit_so2_0, ifit_err_0
        self.ifit_so2_1, self.ifit_err_1 = ifit_so2_1, ifit_err_1

        # --------------------
        # Save lookup tables
        # --------------------
        # Define filenames
        date_str = spec_date.strftime(self.spec_specs.file_datestr)
        filename_0 = '{}_ld_lookup_{}-{}_0-{}-{}ppmm.npy'.format(date_str, int(np.round(self.start_fit_wave)),
                                                                 int(np.round(self.end_fit_wave)),
                                                                 self.grid_max_ppmm, self.grid_increment_ppmm)
        filename_1 = '{}_ld_lookup_{}-{}_0-{}-{}ppmm.npy'.format(date_str, int(np.round(self.start_fit_wave_2)),
                                                                 int(np.round(self.end_fit_wave_2)),
                                                                 self.grid_max_ppmm, self.grid_increment_ppmm)
        file_path_0 = os.path.join(FileLocator.LD_LOOKUP, filename_0)
        file_path_1 = os.path.join(FileLocator.LD_LOOKUP, filename_1)

        # Combine fit and error arrays
        lookup_0 = np.array([ifit_so2_0, ifit_err_0])
        lookup_1 = np.array([ifit_so2_1, ifit_err_1])

        # Save files
        print('Saving light dilution grids: {}, {}'.format(file_path_0, file_path_1))
        np.save(file_path_0, lookup_0)
        np.save(file_path_1, lookup_1)

    def load_ld_lookup(self, file_path, fit_num=0, use_new_window=False):
        """
        Loads lookup table from file
        :param file_path:   str     Path to lookup table file
        :param fit_num:     int     Either 0 or 1, defines whether this should be set to the 0 or 1 window of the object
        :param use_new_window   bool    If False, we revert back to the previous fit window after loading in the LD data
        :return:
        """
        dat = np.load(file_path)
        x, y = dat  # unpack data into cd and error grids
        setattr(self, 'ifit_so2_{}'.format(fit_num), x)
        setattr(self, 'ifit_err_{}'.format(fit_num), y)
        setattr(self, 'ifit_so2_{}_path'.format(fit_num), file_path)

        # Extract grid info from filename
        filename = os.path.split(file_path)[-1]
        fit_windows, grid = filename.split('_')[-2:]
        grid = grid.split('ppmm')[0]
        grid_max_ppmm, grid_increment_ppmm = grid.split('-')[-2:]
        self.update_grid(int(grid_max_ppmm), int(grid_increment_ppmm))

        if not use_new_window:
            start_wave_old, end_wave_old = self.start_fit_wave, self.end_fit_wave

        # Fit window update
        start_fit_wave, end_fit_wave = fit_windows.split('-')
        if fit_num == 0:
            self.start_fit_wave, self.end_fit_wave = int(start_fit_wave), int(end_fit_wave)
        elif fit_num == 1:
            self.start_fit_wave_2, self.end_fit_wave_2 = int(start_fit_wave), int(end_fit_wave)

        # Update analysers with fit window settings
        self.update_ld_analysers()

        # Revert back to old fitting windows
        if not use_new_window:
            self.start_fit_wave, self.end_fit_wave = start_wave_old, end_wave_old


    def ld_lookup(self, so2_dat, so2_dat_err, wavelengths, spectra, spec_time):
        """
        Performs lookup on currently loaded table, to find the best estimate of SO2 and light dilution factor

        :param so2_dat:     array-like
            SO2 data to lookup. If single tuple the function will convert to array of tuple. NOTE: SO2 data should be an
            SO2 data to lookup. If single tuple the function will convert to array of tuple. NOTE: SO2 data should be an
            iterable of tuples. Each tuple (x_1, x_2) represents one spectrum retrieval from the 2 separate fit windows.
            i.e. each spectrum provides 2 SO2 column densities, and this is what should be provided here
        :param so2_dat_err  array-like
            Corresponding so2 errors for above so2 data
        :param wavelengths: array-like
            Contains arrays of wavelength data - used for final refitting of spectrum.
        :param spectra:     array-like
            Contains arrays of spectra. Data corresponds to associated index of so2_dat
        :param spec_time    array-like
            Contains array of spectra times.
        :return:
        """

        try:
            _ = [x for x in so2_dat[0]]
        except TypeError:
            so2_dat = np.array([so2_dat])
        try:
            _ = [x for x in so2_dat_err[0]]
        except TypeError:
            so2_dat_err = np.array([so2_dat_err])
        try:
            _ = [x for x in wavelengths[0]]
        except TypeError:
            wavelengths = np.array([wavelengths])
        try:
            _ = [x for x in spectra[0]]
        except TypeError:
            spectra = np.array([spectra])
        try:
            _ = [x for x in spec_time[0]]
        except TypeError:
            spec_time = np.array([spec_time])

        # Add light dilution parameter to analyser
        self.analyser0.params.add('LDF', value=0.0, vary=False)
        self.analyser1.params.add('LDF', value=0.0, vary=False)

        # Make polygons out of curves
        indices, polygons = lookup.create_polygons(self.ifit_so2_0, self.ifit_so2_1)

        # Reshape polygon object to enable STRtrees
        poly_shape = polygons.shape
        shaped_polygons = polygons.reshape(poly_shape[0]*poly_shape[1]*2, 3, 2)
        shaped_indices = indices.reshape(poly_shape[0]*poly_shape[1]*2, 3)

        # Record dimensions of curve array
        shape = np.shape(self.ifit_so2_0)

        # List column names (2 DFs: one for grid search of LDF one for refit using LDF)
        col_names = ('Number', 'SO2_0', 'SO2_1', 'SO2', 'SO2_min', 'SO2_max', 'LDF', 'LDF_min', 'LDF_max')
        col_names_refit = ('Number', 'time', 'LDF', 'SO2_0', 'SO2_0_err', 'SO2_1', 'SO2_1_err')

        n_spec = np.arange(len(so2_dat))

        # Create dataframe
        results_df = pd.DataFrame(index=n_spec, columns=col_names)
        results_refit = pd.DataFrame(index=n_spec, columns=col_names_refit)

        # Use shapely objects to improve speed
        points_shapely = [Point(point) for point in so2_dat]

        # Ignore IllegalArgumentException on this line
        poly_shapely = [Polygon(poly) for poly in shaped_polygons]

        # Create dictionary to index point list for faster querying
        index_by_id = dict((id(poly), i) for i, poly in enumerate(poly_shapely))

        # Create STRtree
        tree = STRtree(poly_shapely)

        for i, point in enumerate(so2_dat):
            print('Evaluating light dilution for spectrum: {}'.format(spec_time[i].strftime("%Y-%m-%d %H:%M:%S")))

            # Extract error and shapely point on current loop
            point_shapely = points_shapely[i]
            point_err = so2_dat_err[i]

            # =========================================================================
            # Calculate uncertainty
            # =========================================================================
            try:
                point_args, x_idx, y_idx = lookup.calc_uncertainty(point, point_err, self.ifit_so2_0, self.ifit_so2_1)

                so2_min, so2_max = self.so2_grid[x_idx]
                ldf_min, ldf_max = self.ldf_grid[y_idx]

            except:
                so2_min, so2_max = np.nan, np.nan
                ldf_min, ldf_max = np.nan, np.nan

            # =========================================================================
            # Calculate best guess
            # =========================================================================
            answers, bary_coords, answer_flag = lookup.calc_value(point_shapely, tree, polygons,
                                                                  shaped_indices, index_by_id)

            # Make sure that only a single answer was found.
            if answer_flag == 'Single':
                answer = answers[0]
                bary_coord = bary_coords[0]

                # Find vertices of triangle containing point
                vertices_so2 = polygons[(answer[0], answer[1], answer[2])]
                vertices_so2 = np.vstack((vertices_so2, vertices_so2[0]))

                # Extract triangle index to find vertices
                j, k = answer[0], answer[1]
                if answer[2] == 0:
                    # Create polygon corners for triangle type a
                    vertices_model = np.array([[self.so2_grid[j], self.ldf_grid[k]],
                                               [self.so2_grid[j+1], self.ldf_grid[k]],
                                               [self.so2_grid[j], self.ldf_grid[k+1]]])
                else:
                    # Create polygon corners for triangle type b
                    vertices_model = np.array([[self.so2_grid[j+1], self.ldf_grid[k]],
                                               [self.so2_grid[j+1], self.ldf_grid[k+1]],
                                               [self.so2_grid[j], self.ldf_grid[k+1]]])

                # Create best guess using the barycentric coordinates
                so2_best, ldf_best = np.sum(vertices_model.T * bary_coords[0], axis=1)

                # Check that for both ldf and so2, min <= best <= max
                if so2_max < so2_best:so2_max = so2_best
                if so2_min > so2_best:so2_min = so2_best
                if ldf_max < ldf_best:ldf_max = ldf_best
                if ldf_min > ldf_best:ldf_min = ldf_best

            # If no anaswer is found then the uncorrected SO2 SCDs lie outside graph
            # Examine location of the point to determine best way forward
            elif answer_flag == 'None':

                # Check if point is below error zero on either uncorrected SO2 SCD
                if (point[0] < -(np.mean(2*point_err[0])) or
                        point[1] < -(np.mean(2*point_err[1]))):
                    so2_best = np.nan
                    ldf_best = np.nan

                else:
                    # Give undefined best estimate if error range is entire dataset
                    if so2_min == np.nan or (so2_min == 0 and so2_max == self.so2_grid[-1]):
                        so2_best = np.nan

                    # Set SO2 to average of the maximum and minimum error value
                    else:
                        so2_best = np.mean((so2_min, so2_max))

                    # If 306 is bigger than 312, then set best ldf guess to 0
                    if point[0] > point[1]:
                        ldf_best = 0.0

                    # If 306 is smaller than 312 and no answer, LDF must be large or
                    # outside modelled data range
                    else:
                        ldf_best = 1.0

            # If data point is within error of zero, give an undefined SO2
            if np.logical_or((point[0] - 2 * point_err[0] < 0), (point[1] - 2 * point_err[1] < 0)):
                so2_best = np.nan
                ldf_best = np.nan

            # Combine answers into a single row, which will then go to an output .csv
            row = [i, point[0], point[1], so2_best, so2_min, so2_max, ldf_best, ldf_min, ldf_max]
            results_df.loc[i] = row

            # =============================================================
            # Refit using the best LDF (following refit.py)
            # =============================================================
            # TODO - I'm not sure this actually changes anyuthing so may just be unneccesary extra processing
            if np.isnan(ldf_best):
                # Change analyser values
                self.analyser0.params['LDF'].set(value=0)
                self.analyser1.params['LDF'].set(value=0)
            else:
                # Change analyser values
                self.analyser0.params['LDF'].set(value=ldf_best)
                self.analyser1.params['LDF'].set(value=ldf_best)
            print('LDF: {}'.format(self.analyser0.params['LDF'].value))

            # Fit spectrum for in 2 fit windows
            fit0 = self.analyser0.fit_spectrum([wavelengths[i], spectra[i]], calc_od=['SO2', 'Ring', 'O3'],
                                               update_params=False)

            fit1 = self.analyser1.fit_spectrum([wavelengths[i], spectra[i]], calc_od=['SO2', 'Ring', 'O3'],
                                               update_params=False)
            self.ldf_best = ldf_best
            row = [i, spec_time[i], ldf_best]
            row += [fit0.params['SO2'].fit_val, fit0.params['SO2'].fit_err]
            row += [fit1.params['SO2'].fit_val, fit1.params['SO2'].fit_err]
            results_refit.loc[i] = row

        return results_df, results_refit, fit0, fit1

    def ldf_refit(self, wavelengths, spectrum, ldf):
        """
        Runs ifit with ldf defined. Useful if we don't want to calculate a new LDF for every single data point.
        This will save time. i.e. can check when last LDF was calculated and if it was recently you can pass it to
        this rather than running ld_lookpup. ASSUMING LDF DOESN'T CHANGE RAPIDLY IN TIME
        """
        # TODO I don't think this alone works - the ldf doesn't seem to change the fit result
        # TODO instead i will still need to run the LD lookup to find so2_best which is the column density value we
        # TODO are interested in.
        if np.isnan(ldf):
            # Change analyser values
            self.analyser0.params['LDF'].set(value=0)
            self.analyser1.params['LDF'].set(value=0)
        else:
            # Change analyser values
            self.analyser0.params['LDF'].set(value=ldf)
            self.analyser1.params['LDF'].set(value=ldf)

        # Fit spectrum for in 2 fit windows
        fit0 = self.analyser0.fit_spectrum([wavelengths, spectrum], calc_od=['SO2', 'Ring', 'O3'],
                                           update_params=False)

        fit1 = self.analyser1.fit_spectrum([wavelengths, spectrum], calc_od=['SO2', 'Ring', 'O3'],
                                           update_params=False)

        self.ldf_best = ldf
        return fit0, fit1
    # ==================================================================================================================


class SpectraError(Exception):
    """
    Error raised if correct spectra aren't present for processing
    """
    pass


if __name__ == '__main__':
    # Calibration paths
    ils_path = './calibration/2019-07-03_302nm_ILS.txt'
    # ils_path = './calibration/2019-07-03_313nm_ILS.txt'
    frs_path = '../ifit/Ref/sao2010.txt'
    ref_paths = {'SO2': {'path': '../iFit/Ref/SO2_295K.txt', 'value': 1.0e16},  # Value is the inital estimation of CD
                 'O3': {'path': '../iFit/Ref/O3_223K.txt', 'value': 1.0e19},
                 'Ring': {'path': '../iFit/Ref/Ring.txt', 'value': 0.1}
                 }

    # ref_paths = {'SO2': {'path': 'C:\\Users\\tw9616\\Documents\\PostDoc\\Permanent Camera\\PyCamPermanent\\pycam\\doas\\calibration\\Vandaele (2009) x-section in wavelength.txt', 'value': 1.0e16},
    # 'O3': {'path':     'C:\\Users\\tw9616\\Documents\\PostDoc\\Permanent Camera\\PyCamPermanent\\pycam\\doas\\calibration\\Serdyuchenko_O3_223K.txt', 'value': 1.0e19},
    # 'Ring': {'path': '../iFit/Ref/Ring.txt', 'value': 0.1}
    # }

    # Spectra path
    spec_path = 'C:\\Users\\tw9616\\Documents\\PostDoc\\Permanent Camera\\PyCamPermanent\\pycam\\Data\\Spectra\\test_data'

    # Create ifit object
    ifit_worker = IFitWorker(frs_path=frs_path, species=ref_paths, dark_dir=spec_path)
    ifit_worker.load_ils(ils_path)  # Load ILS

    # Update fit wavelengths
    ifit_worker.start_fit_wave = 312
    ifit_worker.end_fit_wave = 320

    # Process directory
    ifit_worker.process_dir(spec_dir=spec_path)
    # ifit_worker.start_processing_threadless(spec_dir=spec_path)

    # ------------
    # Plotting
    # ------------
    # Make the figure and define the subplot grid
    fig = plt.figure(figsize=[10, 6.4])
    gs = GridSpec(2, 2)

    # Define axes
    ax0 = fig.add_subplot(gs[0, 0])
    ax1 = fig.add_subplot(gs[1, 0])
    ax2 = fig.add_subplot(gs[0, 1])
    ax3 = fig.add_subplot(gs[1, 1])

    # Define plot lines
    l0, = ax0.plot([], [], 'C0x-')  # Measured spectrum
    l1, = ax0.plot([], [], 'C1-')   # Model fit

    l2, = ax1.plot([], [], 'C0x-')  # Residual

    l3, = ax2.plot([], [], 'C0x-')  # Measured OD
    l4, = ax2.plot([], [], 'C1-')   # Fit OD

    l5, = ax3.plot([], [], '-')  # Spectrum

    #
    l0.set_data(ifit_worker.fit.grid, ifit_worker.fit.spec)
    l1.set_data(ifit_worker.fit.grid, ifit_worker.fit.fit)
    l2.set_data(ifit_worker.fit.grid, ifit_worker.fit.resid)
    l3.set_data(ifit_worker.fit.grid, ifit_worker.fit.meas_od['SO2'])
    l4.set_data(ifit_worker.fit.grid, ifit_worker.fit.synth_od['SO2'])
    l5.set_data(ifit_worker.wavelengths, ifit_worker.plume_spec_corr)

    for ax in [ax0, ax1, ax2, ax3]:
        ax.relim()
        ax.autoscale_view()

    plt.pause(0.01)
    plt.tight_layout()
    plt.show()
