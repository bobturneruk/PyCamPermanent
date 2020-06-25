# -*- coding: utf-8 -*-

"""
Contains all classes associated with building figures for the analysis functions of SO2 cameras
"""

from pycam.gui.cfg import gui_setts
from pycam.setupclasses import CameraSpecs, FileLocator
from pycam.cfg import pyplis_worker
from pycam.pyplis_worker import UnrecognisedSourceError
from pyplis import LineOnImage

import tkinter as tk
import tkinter.ttk as ttk
from tkinter import messagebox
from tkinter import filedialog
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.cm as cm
from mpl_toolkits.axes_grid1 import make_axes_locatable
import os


class ImageSO2:
    """
    Main class for generating an image of SO2. It may be calibated [ppm.m] or uncalibrated [apparent absorbance]
    depending on the user's command

    Parameters
    ----------
    parent: tk.Frame, ttk.Frame
        Frame the widget will be placed inside
    image: np.ndarray
        Image array for plotting
    pix_dim: tuple
        [x_dimension, y_dimension] list of resolution for SO2 camera imagery
    """

    def __init__(self, parent, image=None, pix_dim=(CameraSpecs().pix_num_x, CameraSpecs().pix_num_y)):
        self.parent = parent
        self.image = image
        self.pix_num_x = pix_dim[0]
        self.pix_num_y = pix_dim[1]
        self.dpi = gui_setts.dpi
        self.fig_size = gui_setts.fig_SO2

        self.specs = CameraSpecs()

        self.max_lines = 5
        # ------------------------------------------------------------------------------------------
        # TK variables Setup

        # ICA lines
        self._num_ica = tk.IntVar()
        self.num_ica = 1
        self._current_ica = tk.IntVar()
        self.current_ica = 1
        self.ica_lines_list = [None] * self.max_lines           # Pyplis line objects list
        self.ica_plt_list = [None] * self.max_lines             # Plot line objects list
        self.ica_coords = []                                    # Coordinates for most recent line plot
        self.scat_ica_point = None
        colours = 100                                           # Number of colours in cmap
        cmap = cm.get_cmap("jet", colours)
        self.line_colours = [cmap(int(f * (colours / (self.max_lines-1)))) for f in range(self.max_lines)]  # Line colours

        # Colour map
        self.cmaps = ['Reds',
                      'Blues',
                      'Greens',
                      'Oranges',
                      'Greys',
                      'viridis',
                      'plasma',
                      'inferno',
                      'magma',
                      'cividis',
                      'seismic']
        self._cmap = tk.StringVar()
        self.cmap = self.cmaps[0]

        # Optical flow plotting option
        self._plt_flow = tk.IntVar()
        self.plt_flow = 1

        # -----------------------------------------------------------------------------------------------

        # Generate main frame for figure
        self.frame = ttk.Frame(self.parent)

        if self.image is None:
            self.image = np.random.random([self.pix_num_y, self.pix_num_x]) * self.specs._max_DN

        # Generate frame options
        self._build_options()

        # Generate analysis options frame
        self._build_analysis()

        # Generate figure
        self._build_fig_img()

        self.frame_opts.grid(row=0, column=0, padx=5, pady=5, sticky='nsew')
        self.frame_analysis.grid(row=0, column=1, padx=5, pady=5, sticky='nsew')
        self.frame_fig.grid(row=1, column=0, columnspan=2, padx=5, pady=5)

        self.frame.columnconfigure(1, weight=1)

    @property
    def num_ica(self):
        return self._num_ica.get()

    @num_ica.setter
    def num_ica(self, value):
        self._num_ica.set(value)

    @property
    def current_ica(self):
        return self._current_ica.get()

    @current_ica.setter
    def current_ica(self, value):
        self._current_ica.set(value)

    @property
    def cmap(self):
        return getattr(cm, self._cmap.get())

    @cmap.setter
    def cmap(self, value):
        self._cmap.set(value)

    @property
    def plt_flow(self):
        return self._plt_flow.get()

    @plt_flow.setter
    def plt_flow(self, value):
        self._plt_flow.set(value)

    def _build_fig_img(self):
        """Build figure"""
        # Main frame for figure and all associated widgets
        self.frame_fig = ttk.Frame(self.frame, relief=tk.RAISED, borderwidth=3)

        # Create figure
        self.fig = plt.Figure(figsize=self.fig_size, dpi=self.dpi)
        self.ax = self.fig.subplots(1, 1)
        self.ax.set_aspect(1)

        # Figure colour
        self.fig.set_facecolor('black')
        for child in self.ax.get_children():
            if isinstance(child, matplotlib.spines.Spine):
                child.set_color('white')
        self.ax.tick_params(axis='both', colors='white', direction='in', top='on', right='on')

        # Image display
        self.img_disp = self.ax.imshow(self.image, cmap=self.cmap, interpolation='none', vmin=0,
                                       vmax=self.specs._max_DN, aspect='auto')
        self.ax.set_title('SO2 image', color='white')

        # Colorbar
        divider = make_axes_locatable(self.ax)
        self.ax_divider = divider.append_axes("right", size="10%", pad=0.05)
        self.cbar = plt.colorbar(self.img_disp, cax=self.ax_divider)
        self.cbar.outline.set_edgecolor('white')
        self.cbar.ax.tick_params(axis='both', colors='white', direction='in', top='on', right='on')

        # Plot optical flwo if it is requested at start
        self.plt_opt_flow()

        # Finalise canvas and gridding
        self.img_canvas = FigureCanvasTkAgg(self.fig, master=self.frame_fig)
        self.img_canvas.draw()
        self.img_canvas.get_tk_widget().grid(row=0, column=0, sticky='nsew')

        # Bind click event to figure
        self.fig.canvas.callbacks.connect('button_press_event', self.ica_draw)

    def _build_analysis(self):
        """Build analysis options"""
        self.frame_analysis = ttk.LabelFrame(self.frame, text='Analysis')

        # Number of lines
        label = ttk.Label(self.frame_analysis, text='Num. ICAs:')
        label.grid(row=0, column=0, sticky='w')
        self.ica_spin = ttk.Spinbox(self.frame_analysis, textvariable=self._num_ica, from_=1, to=self.max_lines,
                                    increment=1, command=self.update_ica_num)
        self.ica_spin.grid(row=0, column=1, sticky='ew')

        # Line to edit
        label = ttk.Label(self.frame_analysis, text='Edit ICA:')
        label.grid(row=1, column=0, sticky='w')
        self.ica_edit_spin = ttk.Spinbox(self.frame_analysis, textvariable=self._current_ica, from_=1, to=self.num_ica,
                                         increment=1)
        self.ica_edit_spin.grid(row=1, column=1, sticky='ew')

        # Flip ICA normal button
        self.ica_flip_butt = ttk.Button(self.frame_analysis, text='Flip ICA normal', command=self.flip_ica_normal)
        self.ica_flip_butt.grid(row=1, column=2, sticky='nsew', padx=2, pady=2)


    def _build_options(self):
        """Builds options widget"""
        self.frame_opts = ttk.LabelFrame(self.frame, text='Figure options')

        # Colour maps
        label = ttk.Label(self.frame_opts, text='Colour map:')
        label.grid(row=0, column=0, padx=2, pady=2, sticky='w')
        self.opt_menu = ttk.OptionMenu(self.frame_opts, self._cmap, self._cmap.get(), *self.cmaps,
                                       command=self.change_cmap)
        self.opt_menu.config(width=8)
        self.opt_menu.grid(row=0, column=1, padx=2, pady=2, sticky='ew')

        self.plt_flow_check = ttk.Checkbutton(self.frame_opts, text='Display Optical Flow', variable=self._plt_flow,
                                              command=self.plt_opt_flow)
        self.plt_flow_check.grid(row=1, column=0, columnspan=2, padx=2, pady=2, sticky='w')

    def update_ica_num(self):
        """Makes necessary changes to update the number of ICA lines"""

        # Edit 'to' of ica_edit_spin spinbox and if current ICA is above number of ICAs we update current ICA
        self.ica_edit_spin.configure(to=self.num_ica)
        if self.current_ica > self.num_ica:
            self.current_ica = self.num_ica

        # Delete any drawn lines over the new number requested if they are present
        ica_num = self.num_ica
        for ica in self.ica_lines_list[self.num_ica:]:
            if ica is not None:
                self.del_ica(ica_num)
                self.ica_lines_list[ica_num] = None
            ica_num += 1

    def flip_ica_normal(self):
        """Flips the normal vector of the current ICA"""
        ica_idx = self.current_ica - 1

        # Get line currently being edited
        line = self.ica_lines_list[ica_idx]
        lbl = "{}".format(ica_idx)

        # If current line is not none we find it's orientation and reverse it
        if line is not None:
            self.del_ica(ica_idx)

            if line.normal_orientation == 'right':
                self.ica_lines_list[ica_idx] = LineOnImage(x0=line.x0, y0=line.y0, x1=line.x1, y1=line.y1,
                                                           normal_orientation="left", color=self.line_colours[ica_idx],
                                                           line_id=lbl)

            elif line.normal_orientation == 'left':
                self.ica_lines_list[ica_idx] = LineOnImage(x0=line.x0, y0=line.y0, x1=line.x1, y1=line.y1,
                                                           normal_orientation="right", color=self.line_colours[ica_idx],
                                                           line_id=lbl)

            # Plot pyplis object on figure
            self.ica_lines_list[ica_idx].plot_line_on_grid(ax=self.ax, include_normal=1,
                                                               include_roi_rot=True, label=lbl)

            # Redraw canvas
            self.img_canvas.draw()

    def ica_draw(self, event):
        """Collects points for ICA line and then draws it when a complete line is drawn"""
        # Python indices start at 0, so need to set the correct index for list indexing
        ica_idx = self.current_ica - 1

        if event.inaxes is self.ax:
            idx = len(self.ica_coords)

            # If we are about to overwrite an old line, we first check that the user wants this
            if idx == 1:
                if self.ica_lines_list[ica_idx] is not None:
                    resp = messagebox.askokcancel('Overwriting line',
                                                  'You are about to overwrite an existing line.\n'
                                                  'This could affect processing results if it is currently running.')
                    if not resp:
                        return

            # If 2 points are already defined we want to clear these points
            if idx == 2:
                self.ica_coords = []
                idx = 0  # Resetting index for 'point' definition

            # Update ica_coords with new coordinates
            self.ica_coords.append((event.xdata, event.ydata))

            # Remove last click point and scatter current click
            try:
                self.scat_ica_point.remove()
            except:
                pass
            self.scat_ica_point = self.ax.scatter(event.xdata, event.ydata, s=50, marker='x', color='k', lw=1)

            self.ax.set_xlim(0, self.pix_num_x - 1)
            self.ax.set_ylim(self.pix_num_y - 1, 0)

            if idx == 1:
                # Delete scatter point
                try:
                    self.scat_ica_point.remove()
                except:
                    pass

                # Delete previous line if it exists
                if self.ica_lines_list[ica_idx] is not None:
                    self.del_ica(ica_idx)

                # Update pyplis line object
                lbl = "{}".format(ica_idx)
                self.ica_lines_list[ica_idx] = LineOnImage(x0=self.ica_coords[0][0],
                                                                    y0=self.ica_coords[0][1],
                                                                    x1=self.ica_coords[1][0],
                                                                    y1=self.ica_coords[1][1],
                                                                    normal_orientation="right",
                                                                    color=self.line_colours[ica_idx],
                                                                    line_id=lbl)

                # Plot pyplis object on figure
                self.ica_lines_list[ica_idx].plot_line_on_grid(ax=self.ax, include_normal=1,
                                                               include_roi_rot=True, label=lbl)

                # # Extract ICA values and plotting them
                # self.plot_ica_xsect()

            self.img_canvas.draw()
        else:
            self.messages('Clicked outside axes bounds but inside plot window')


    def del_ica(self, line_num):
        """Searches axis for line object relating to pyplis line object and removes it

        Parameters
        ----------
        line_num: int
            Index of line in ica_lines_list"""
        # Get line
        line = self.ica_lines_list[line_num]

        # Search for line and remove it when the correct one is found
        plt_lines = self.ax.get_lines()
        for l in plt_lines:
            if l._label == line.line_id:
                l.remove()

        # Search for everything else and remove
        children = self.ax.get_children()
        for child in children:
            if isinstance(child, matplotlib.patches.FancyArrow):
                if child._facecolor == line.color:
                    child.remove()
            elif isinstance(child, matplotlib.patches.Polygon):
                if child._original_facecolor == line.color:
                    child.remove()


        # Once removed, set the line to None
        self.ica_lines_list[line_num] = None

        # Redraw canvas
        self.img_canvas.draw()

    def change_cmap(self, cmap):
        """Change colourmap of image"""
        # Set cmap to new value
        self.img_disp.set_cmap(getattr(cm, cmap))

        # Update canvas
        self.img_canvas.draw()

    def plt_opt_flow(self):
        """Plots optical flow onto figure"""


class GeomSettings:
    """
    Creates frame holding all geometry and associated settings required by pyplis. This frame links to the PyplisWorker
    object to update settings when needed.

    This object should be instantiated on startup, so a conf file need to do this, and then the generate_frame() method
    should be used at the point at which it is required
    """
    def __init__(self, parent=None, geom_path=FileLocator.CAM_GEOM):
        self.parent = parent
        self.frame = None
        self.geom_path = geom_path
        self.filename = None        # Path to file of current settings

        self.pyplis_worker = pyplis_worker

    def initiate_variables(self):
        """Initiates object, and builds frame if parent is a tk.Frame"""
        # Tk Variables
        self._lat = tk.StringVar()
        self._lon = tk.StringVar()
        self._altitude = tk.IntVar()
        self._elev = tk.DoubleVar()
        self._azim = tk.DoubleVar()
        self._volcano = tk.StringVar()

        self.geom_dict = {'lat': None,
                          'lon': None,
                          'altitude': None,
                          'elev': None,
                          'azim': None}    # List of attributes

        # Setting start values of variables
        with open(FileLocator.DEFAULT_GEOM, 'r') as f:
            self.filename = f.readline()
        self.load_instrument_setup(self.filename)

        # If class is instantiated with a Frame we generate the widgets
        if self.parent is not None:
            self.generate_frame(self.parent)

    def generate_frame(self, parent):
        """Generates the GUI for this frame, given the parent frame. This method of generating the frame means that
        the object can exist, with variables being instantiated, prior to building the frame"""
        self.parent = parent
        self.frame = ttk.Frame(self.parent)
        # ----------------------------------------------------------------------
        # Tkinter widgets
        row = 0

        label = ttk.Label(self.frame, text='Latitude [dec]:')
        label.grid(row=row, column=0, padx=2, pady=2, sticky='w')
        entry = ttk.Entry(self.frame, width=10, textvariable=self._lat)
        entry.grid(row=row, column=1, padx=2, pady=2, sticky='nsew')

        row += 1
        label = ttk.Label(self.frame, text='Longitude [dec]:')
        label.grid(row=row, column=0, padx=2, pady=2, sticky='w')
        entry = ttk.Entry(self.frame, width=10, textvariable=self._lon)
        entry.grid(row=row, column=1, padx=2, pady=2, sticky='nsew')

        row += 1
        label = ttk.Label(self.frame, text='Altitude [m]:')
        label.grid(row=row, column=0, padx=2, pady=2, sticky='w')
        spinbox = ttk.Spinbox(self.frame, textvariable=self._altitude, from_=0, to=8000, increment=1, width=4)
        spinbox.grid(row=row, column=1, padx=2, pady=2, sticky='nsew')

        row += 1
        label = ttk.Label(self.frame, text='Elevation angle [°]:')
        label.grid(row=row, column=0, padx=2, pady=2, sticky='w')
        spinbox = ttk.Spinbox(self.frame, format='%.1f', textvariable=self._elev,
                              from_=-90, to=90, increment=0.1, width=4)
        spinbox.set('{:.1f}'.format(self.elev))
        spinbox.grid(row=row, column=1, padx=2, pady=2, sticky='nsew')

        row += 1
        label = ttk.Label(self.frame, text='Azimuth [°]:')
        label.grid(row=row, column=0, padx=2, pady=2, sticky='w')
        spinbox = ttk.Spinbox(self.frame, textvariable=self._azim, from_=0, to=359, increment=1, width=4)
        spinbox.grid(row=row, column=1, padx=2, pady=2, sticky='nsew')

        row += 1
        label = ttk.Label(self.frame, text='Volcano:')
        label.grid(row=row, column=0, padx=2, pady=2, sticky='w')
        entry = ttk.Entry(self.frame, width=10, textvariable=self._volcano)
        entry.grid(row=row, column=1, padx=2, pady=2, sticky='nsew')

        row += 1
        button = ttk.Button(self.frame, text='Update settings', command=self.update_geom)
        button.grid(row=row, column=0, padx=2, pady=2, sticky='nsew')

        button = ttk.Button(self.frame, text='Save settings', command=self.save_instrument_setup)
        button.grid(row=row, column=1, padx=2, pady=2, sticky='nsew')

        button = ttk.Button(self.frame, text='Load settings', command=self.load_instrument_setup)
        button.grid(row=row, column=2, padx=2, pady=2, sticky='nsew')

        button = ttk.Button(self.frame, text='Set as default', command=self.set_default_instrument_setup)
        button.grid(row=row, column=3, padx=2, pady=2, sticky='nsew')

    @property
    def lat(self):
        return self._lat.get()

    @lat.setter
    def lat(self, value):
        self._lat.set(value)

    @property
    def lon(self):
        return self._lon.get()

    @lon.setter
    def lon(self, value):
        self._lon.set(value)

    @property
    def altitude(self):
        return self._altitude.get()

    @altitude.setter
    def altitude(self, value):
        self._altitude.set(value)

    @property
    def elev(self):
        return self._elev.get()

    @elev.setter
    def elev(self, value):
        self._elev.set(value)

    @property
    def azim(self):
        return self._azim.get()

    @azim.setter
    def azim(self, value):
        self._azim.set(value)

    @property
    def volcano(self):
        return self._volcano.get()

    @volcano.setter
    def volcano(self, value):
        self._volcano.set(value)

    def gather_geom(self):
        """Gathers all geometry data and adds it to the dictionary"""
        for key in self.geom_dict:
            self.geom_dict[key] = getattr(self, key)

    def update_geom(self):
        """Updates pyplis MeasGeom object with values from this frame"""
        # Update measurement setup with location
        try:
            self.pyplis_worker.measurement_setup(location=self.volcano)
        except UnrecognisedSourceError:
            messagebox.showerror('Source not recognised.',
                                 'The volcano source {} was not recognised. \n'
                                 'Please try a different source name or add '
                                 'source information manually.'.format(self.volcano))

        # Update geometry dictionary and pyplis worker
        self.gather_geom()
        self.pyplis_worker.update_cam_geom(self.geom_dict)


    def save_instrument_setup(self):
        """Saves all of the current instrument geometry settings to a text file, so it can be loaded on start-up"""
        # Ensure that all tkinter variables have been gathered and that current volcano location exists
        # We do not want to save a geometry which isn't fully functional
        self.update_geom()

        # Ask user to define filename for saving geometry settings
        self.filename = filedialog.asksaveasfilename(initialdir=self.geom_path)

        # Ensure filename has .txt format
        self.filename = '{}.txt'.format(self.filename.split('.')[0])

        # Open file object and write all attributes to it
        with open(self.filename, 'w') as f:
            f.write('# Geometry setup file\n')
            f.write('volcano={}\n'.format(self.volcano))
            for key in self.geom_dict:
                f.write('{}={}\n'.format(key, self.geom_dict[key]))

    def load_instrument_setup(self, filepath=None):
        """Loads existing instrument setup"""
        # Get filename to load
        if filepath is None:
            self.filename = filedialog.askopenfilename(initialdir=self.geom_path)

        with open(self.filename, 'r') as f:
            for line in f:
                # Ignore first line
                if line[0] == '#':
                    continue

                # Extract key-value pair, remove the newline character from the value, then recast
                key, value = line.split('=')
                value = value.replace('\n', '')
                if key == 'volcano':
                    setattr(self, key, value)
                elif key == 'altitude':
                    setattr(self, key, int(value))
                else:
                    setattr(self, key, float(value))

        # Update geometry settings
        self.update_geom()

    def set_default_instrument_setup(self):
        """Sets default instrument setup to load on startup"""
        # First save the settings
        self.save_instrument_setup()

        # Update default
        with open(FileLocator.DEFAULT_GEOM, 'w') as f:
            f.write(self.filename)