# -*- coding: utf-8 -*-

"""Code to build interface between the GUI and the instrument"""

import pycam.gui.cfg as cfg
from pycam.networking.ssh import open_ssh, close_ssh, ssh_cmd
from pycam.setupclasses import FileLocator, ConfigInfo
from pycam.io import write_witty_schedule_file, read_witty_schedule_file, write_script_crontab, read_script_crontab

import tkinter as tk
import tkinter.ttk as ttk
from tkinter import messagebox
import subprocess
import platform
import time
import datetime


def run_pycam(ip):
    """Runs main pycam script on remote machine"""
    if messagebox.askyesno("Please confirm", "Are you sure you want to run pycam_masterpi.py?\n"
                                             "Running this on a machine which already has the script running could cause issues"):
        print('Running pycam_masterpi on {}'.format(ip))

        # Path to executable
        pycam_path = FileLocator.SCRIPTS + 'pycam_masterpi.py'

        # Open ssh connection
        connection = open_ssh(ip)

        # Run ssh command
        stderr, stdout = ssh_cmd(connection, 'python3 {}'.format(pycam_path), background=False)

        # Close ssh connection
        close_ssh(connection)


def instrument_cmd(cmd):
    """Checks if you wanted to shutdown the camera and then sends EXT command to instrument"""
    timeout = 5  # Timeout for instrument shutting down on 'EXT' request

    # Generate message depending on command
    if cmd == 'EXT':
        mess = "Are you sure you want to shutdown the instrument?"
    elif cmd == 'RST':
        mess = "Are you sure you want to restart the instrument?"
    elif cmd == 'RSC':
        mess = "Are you sure you want to restart the cameras?"
    elif cmd == 'RSS':
        mess = "Are you sure you want to restart the spectrometer?"

    # Check if we have a connection to the instrument
    if cfg.indicator.connected:

        if messagebox.askyesno("Please confirm", mess):

            # Add command to queue to shutdown instrument
            cfg.send_comms.q.put({cmd: 1})

            # If EXT: Wait for system to shutdown then indicate that we no longer are connected to it
            if cmd == 'EXT':
                start_time = time.time()
                while time.time() - start_time < timeout:
                    if not cfg.recv_comms.working:
                        cfg.indicator.indicator_off()
                        break
                else:
                    messagebox.showerror('Command Error', 'Instrument appears to still be running')

    # Raise instrument connection error
    else:
        messagebox.showerror('Connection error', 'No instrument connected')
        # NoInstrumentConnected()


class ConnectionGUI:
    """Frame containing code to generate a GUI allowing definition of connection parameters to the instrument"""
    def __init__(self, parent, name='Connection'):
        self.parent = parent
        self.name = name

        self.frame = ttk.Frame(self.parent)
        self.pdx = 5
        self.pdy = 5

        self._host_ip = tk.StringVar()
        self._port = tk.IntVar()
        self.host_ip = cfg.sock.host_ip
        self.port = cfg.sock.port

        ttk.Label(self.frame, text='IP address:').grid(row=0, column=0, padx=self.pdx, pady=self.pdy, sticky='e')
        ttk.Label(self.frame, text='Port:').grid(row=1, column=0, padx=self.pdx, pady=self.pdy, sticky='e')
        ttk.Entry(self.frame, width=15, textvariable=self._host_ip).grid(row=0, column=1, padx=self.pdx, pady=self.pdy, sticky='ew')
        ttk.Entry(self.frame, width=6, textvariable=self._port).grid(row=1, column=1, padx=self.pdx, pady=self.pdy, sticky='ew')

        self.test_butt = ttk.Button(self.frame, text='Test Connection', command=self.test_connection)
        self.test_butt.grid(row=0, column=2, padx=self.pdx, pady=self.pdy)

        self.connection_label = ttk.Label(self.frame, text='')
        self.connection_label.grid(row=0, column=3, padx=self.pdx, pady=self.pdy)

        self.update_butt = ttk.Button(self.frame, text='Update connection', command=self.update_connection)
        self.update_butt.grid(row=2, column=1, padx=self.pdx, pady=self.pdy)

    @property
    def host_ip(self):
        """Public access to tk variable _host_ip"""
        return self._host_ip.get()

    @host_ip.setter
    def host_ip(self, value):
        """Public access setter of tk variable _host_ip"""
        self._host_ip.set(value)

    @property
    def port(self):
        """Public access to tk variable _port"""
        return self._port.get()

    @port.setter
    def port(self, value):
        """Public access setter of tk variable _port"""
        self._port.set(value)

    def update_connection(self):
        """Updates socket address information"""
        cfg.sock.update_address(self.host_ip, self.port)

    def test_connection(self):
        """Tests that IP address is available"""
        # Attempt ping
        try:
            output = subprocess.check_output(
                "ping -{} 1 {}".format('n' if platform.system().lower() == "windows" else 'c', self.host_ip),
                shell=True)

            # If output says host unreachable we flag that there is no connection
            if b'Destination host unreachable' in output:
                raise Exception

            # Otherwise there must be a connection, so we update our label to say this
            else:
                self.connection_label.configure(text='Connection found')

                # Update the connection if we have a good connection
                self.update_connection()

        except Exception as e:
            print(e)
            self.connection_label.configure(text='No connection found at this address')


class InstrumentConfiguration:
    """
    Class creating a widget for configuring the instrument, e.g. adjusting its off/on time through Witty Pi

    To add a new script to be run in the crontab scheduler:
    1. Add script to config.txt and add associated identifier to ConfigInfo
    2. Initiate tk variables below and define script name
    3. Add a hunt for the script name in read_script_crontab() below
    4. Unpack values from "results" for associated script name
    5. Create widgets for controlling new variables and create properties for quick access
    6. Update the update_acq_time() script by adding to cmds and times lists (if necessary add to the check time loop)
    7. Update messagebox to display settings after they have been updated
    8. Add line to script_schedule.txt so that it can be read by this class on first startup
    """
    def __init__(self, ftp, cfg):
        self.ftp = ftp
        self.time_fmt = '{}:{}'
        self.frame = None
        self.in_frame = False
        self.start_script = cfg[ConfigInfo.start_script]
        self.stop_script = cfg[ConfigInfo.stop_script]
        self.dark_script = cfg[ConfigInfo.dark_script]
        self.temp_script = cfg[ConfigInfo.temp_log]
        self.disk_space_script = cfg[ConfigInfo.disk_space_script]

    def initiate_variable(self):
        """Initiate tkinter variables"""
        self._on_hour = tk.IntVar()  # Hour to turn on pi
        self._on_min = tk.IntVar()

        self._off_hour = tk.IntVar()        # Hour to shutdown pi
        self._off_min = tk.IntVar()

        self._on_hour_2 = tk.IntVar()       # Hour to turn on pi (second time)
        self._on_min_2 = tk.IntVar()

        self._off_hour_2 = tk.IntVar()        # Hour to shutdown pi (second time)
        self._off_min_2 = tk.IntVar()

        self._use_second_shutdown = tk.IntVar()     # If True, the second shutdown/startup sequence is used

        self._capt_start_hour = tk.IntVar()     # Hour to start capture
        self._capt_start_min = tk.IntVar()

        self._capt_stop_hour = tk.IntVar()      # Hour to stop capture
        self._capt_stop_min = tk.IntVar()

        self._dark_capt_hour = tk.IntVar()
        self._dark_capt_min = tk.IntVar()

        self._temp_logging = tk.IntVar()        # Temperature logging frequency (minutes)
        self._check_disk_space = tk.IntVar()    # Check disk space frequency (minutes)

        on_time, off_time = read_witty_schedule_file(FileLocator.SCHEDULE_FILE)
        self.on_hour, self.on_min = on_time
        self.off_hour, self.off_min = off_time

        # Read cronfile looking for defined scripts. ADD SCRIPT TO LIST HERE TO SEARCH FOR IT
        results = read_script_crontab(FileLocator.SCRIPT_SCHEDULE,
                                      [self.start_script, self.stop_script, self.dark_script,
                                       self.temp_script, self.disk_space_script])

        self.capt_start_hour, self.capt_start_min = results[self.start_script]
        self.capt_stop_hour, self.capt_stop_min = results[self.stop_script]
        self.dark_capt_hour, self.dark_capt_min = results[self.dark_script]

        self.temp_logging = results[self.temp_script][1]     # Only interested in minutes for temperature logging
        self.check_disk_space = results[self.disk_space_script][1]     # Only interested in minutes for disk space check

    def generate_frame(self):
        """Generates frame containing GUI widgets"""
        if self.in_frame:
            self.frame.attributes('-topmost', 1)
            self.frame.attributes('-topmost', 0)
            return

        self.frame = tk.Toplevel()
        self.frame.title('Instrument configuration')
        self.frame.protocol('WM_DELETE_WINDOW', self.close_frame)
        self.in_frame = True

        frame_on = tk.LabelFrame(self.frame, text='Start-up/Shut-down times', relief=tk.RAISED, borderwidth=2)
        frame_on.grid(row=0, column=0, sticky='nsew', padx=2, pady=2)

        ttk.Label(frame_on, text='Start-up (hour:minutes):').grid(row=0, column=0, sticky='w', padx=2, pady=2)
        ttk.Label(frame_on, text='Shut-down (hour:minutes):').grid(row=1, column=0, sticky='w', padx=2, pady=2)

        hour_start = ttk.Spinbox(frame_on, textvariable=self._on_hour, from_=00, to=23, increment=1, width=2)
        # hour_start.set("{:02d}".format(self.on_hour))
        hour_start.grid(row=0, column=1, padx=2, pady=2)
        ttk.Label(frame_on, text=':').grid(row=0, column=2, padx=2, pady=2)
        min_start = ttk.Spinbox(frame_on, textvariable=self._on_min, from_=00, to=59, increment=1, width=2)
        # min_start.set("{:02d}".format(self.on_min))
        min_start.grid(row=0, column=3, padx=2, pady=2)

        hour_stop = ttk.Spinbox(frame_on, textvariable=self._off_hour, from_=00, to=23, increment=1, width=2)
        # hour_stop.set("{:02d}".format(self.off_hour))
        hour_stop.grid(row=1, column=1, padx=2, pady=2)
        ttk.Label(frame_on, text=':').grid(row=1, column=2, padx=2, pady=2)
        min_stop = ttk.Spinbox(frame_on, textvariable=self._off_min, from_=00, to=59, increment=1, width=2)
        # min_stop.set("{:02d}".format(self.off_min))
        min_stop.grid(row=1, column=3, padx=2, pady=2)

        # Second shutdown option
        check_shut = ttk.Checkbutton(frame_on, text='Use start-up/shut-down sequence 2',
                                     variable=self._use_second_shutdown, command=self.second_shutdown_config)
        check_shut.grid(row=2, column=0, columnspan=4, sticky='w', padx=2, pady=2)
        ttk.Label(frame_on, text='Start-up 2 (hour:minutes):').grid(row=3, column=0, sticky='w', padx=2, pady=2)
        ttk.Label(frame_on, text='Shut-down 2 (hour:minutes):').grid(row=4, column=0, sticky='w', padx=2, pady=2)

        self.hour_start_2 = ttk.Spinbox(frame_on, textvariable=self._on_hour_2, from_=00, to=23, increment=1, width=2)
        self.hour_start_2.grid(row=3, column=1, padx=2, pady=2)
        ttk.Label(frame_on, text=':').grid(row=3, column=2, padx=2, pady=2)
        self.min_start_2 = ttk.Spinbox(frame_on, textvariable=self._on_min_2, from_=00, to=59, increment=1, width=2)
        self.min_start_2.grid(row=3, column=3, padx=2, pady=2)

        self.hour_stop_2 = ttk.Spinbox(frame_on, textvariable=self._off_hour_2, from_=00, to=23, increment=1, width=2)
        self.hour_stop_2.grid(row=4, column=1, padx=2, pady=2)
        ttk.Label(frame_on, text=':').grid(row=4, column=2, padx=2, pady=2)
        self.min_stop_2 = ttk.Spinbox(frame_on, textvariable=self._off_min_2, from_=00, to=59, increment=1, width=2)
        self.min_stop_2.grid(row=4, column=3, padx=2, pady=2)
        self.second_shutdown_config()   # Set current state of widgets based on start-up variable values

        # Update button
        butt = ttk.Button(frame_on, text='Update', command=self.update_on_off)
        butt.grid(row=5, column=0, columnspan=4, sticky='e', padx=2, pady=2)

        # ---------------------------------------
        # Start/stop control of acquisition times
        # ---------------------------------------
        frame_cron = tk.LabelFrame(self.frame, text='Scheduled scripts', relief=tk.RAISED, borderwidth=2)
        frame_cron.grid(row=0, column=1, sticky='nsew', padx=2, pady=2)

        ttk.Label(frame_cron, text='Start pycam (hr:min):').grid(row=0, column=0, sticky='w', padx=2, pady=2)
        ttk.Label(frame_cron, text='Stop pycam (hr:min):').grid(row=1, column=0, sticky='w', padx=2, pady=2)

        hour_start = ttk.Spinbox(frame_cron, textvariable=self._capt_start_hour, from_=00, to=23, increment=1, width=2)
        # hour_start.set("{:02d}".format(self.capt_start_hour))
        hour_start.grid(row=0, column=1, padx=2, pady=2)
        ttk.Label(frame_cron, text=':').grid(row=0, column=2, padx=2, pady=2)
        min_start = ttk.Spinbox(frame_cron, textvariable=self._capt_start_min, from_=00, to=59, increment=1, width=2)
        # min_start.set("{:02d}".format(self.capt_start_min))
        min_start.grid(row=0, column=3, padx=2, pady=2, sticky='w')

        hour_stop = ttk.Spinbox(frame_cron, textvariable=self._capt_stop_hour, from_=00, to=23, increment=1, width=2)
        # hour_stop.set("{:02d}".format(self.capt_stop_hour))
        hour_stop.grid(row=1, column=1, padx=2, pady=2)
        ttk.Label(frame_cron, text=':').grid(row=1, column=2, padx=2, pady=2)
        min_stop = ttk.Spinbox(frame_cron, textvariable=self._capt_stop_min, from_=00, to=59, increment=1, width=2)
        # min_stop.set("{:02d}".format(self.capt_stop_min))
        min_stop.grid(row=1, column=3, padx=2, pady=2, sticky='w')

        # ------------------
        # Start dark capture
        # ------------------
        row = 2
        lab = ttk.Label(frame_cron, text='Start dark capture (hr:min):')
        lab.grid(row=row, column=0, sticky='w', padx=2, pady=2)
        hour_dark = ttk.Spinbox(frame_cron, textvariable=self._dark_capt_hour, from_=00, to=23, increment=1, width=2)
        hour_dark.grid(row=row, column=1, padx=2, pady=2)
        ttk.Label(frame_cron, text=':').grid(row=row, column=2, padx=2, pady=2)
        min_dark = ttk.Spinbox(frame_cron, textvariable=self._dark_capt_min, from_=00, to=59, increment=1, width=2)
        min_dark.grid(row=row, column=3, padx=2, pady=2, sticky='w')

        # -------------------
        # Temperature logging
        # -------------------
        row += 1
        ttk.Label(frame_cron, text='Temperature log [minutes]:').grid(row=row, column=0, sticky='w', padx=2, pady=2)
        temp_log = ttk.Spinbox(frame_cron, textvariable=self._temp_logging, from_=0, to=60, increment=1, width=3)
        temp_log.grid(row=row, column=1, columnspan=2, sticky='w', padx=2, pady=2)
        ttk.Label(frame_cron, text='0=no log').grid(row=row, column=3, sticky='w', padx=2, pady=2)

        # ----------------------------
        # Temperature check disk space
        # ----------------------------
        row += 1
        ttk.Label(frame_cron, text='Check disk storage [minutes]:').grid(row=row, column=0, sticky='w', padx=2, pady=2)
        disk_stor = ttk.Spinbox(frame_cron, textvariable=self._check_disk_space, from_=0, to=60, increment=1, width=3)
        disk_stor.grid(row=row, column=1, columnspan=2, sticky='w', padx=2, pady=2)
        ttk.Label(frame_cron, text='0=no log').grid(row=row, column=3, sticky='w', padx=2, pady=2)

        # -------------
        # Update button
        # -------------
        row += 1
        butt = ttk.Button(frame_cron, text='Update', command=self.update_acq_time)
        butt.grid(row=row, column=0, columnspan=4, sticky='e', padx=2, pady=2)

    def second_shutdown_config(self):
        """Controls configuration of widgets for if a second shutdown is to be used or not"""
        if self.use_second_shutdown:
            state = tk.NORMAL
        else:
            state = tk.DISABLED

        # Loop through widgets and disable/enable them
        for widget in [self.hour_start_2, self.min_start_2, self.hour_stop_2, self.min_stop_2]:
            widget.configure(state=state)

    def check_second_shutdown(self):
        """Checks whether second shutdown sequence is valid (if it is being used)"""
        if not self.use_second_shutdown:
            return

        if self.on_time_2 == self.off_time_2:
            self.use_second_shutdown = 0
            print('Start-up/shut-down times are the same for second schedule. Second sequence will not be used')
            return

        # Check if on/off times fall within the first on/off schedule - we return if this is not the case, so return the
        # function if all is well
        if self.on_time < self.off_time:
            if self.on_time_2 < self.off_time_2:
                if self.off_time_2 < self.on_time or self.on_time_2 > self.off_time:
                    return
            elif self.on_time_2 > self.off_time_2:
                if self.on_time_2 > self.off_time and self.off_time_2 < self.on_time:
                    return

        elif self.on_time > self.off_time:
            if self.on_time_2 < self.off_time_2:
                if self.on_time_2 > self.off_time and self.off_time_2 < self.on_time:
                    return
            # If on_time_2 > off_time_2 then both times pass through midnight so they can't be compatible

        self.use_second_shutdown = 0
        a = messagebox.showwarning('Incompatible second start-up/shut-down sequence',
                                   'Second start-up/shut-down sequence is incompatible with the first\n'
                                   'Second sequence will be removed.\n'
                                   'This happens when a second start-up/shut-down is attempted at a time when\n'
                                   'the first sequence already has the instrument turned on or if the on/off\n'
                                   'times overlap at any point. Please check times.')

    def check_script_time(self, script_time, script_name):
        """
        Checks the scheduled time of a script to be run and ensures that the Pi is turned on at this point. If it isn't
        it raises a warning box indicating that the script probably won't be run. It only highlights this to the user,
        it does not make any changes to times to enforce compatibility.
        :param script_time:     datetime.datetime       Scheduled time of script to be run
        :param script_name:     str                     Name of script - used for flagging it if an issue is found
        :return:
        """
        if self.on_time < self.off_time:
            if script_time > self.on_time and script_time <= self.off_time:
                return

        elif self.on_time > self.off_time:
            if script_time > self.on_time or script_time < self.off_time:
                return
        else:
            # The pi is not being turned off if the time is the same, so we're all good?
            return

        # Check on second start-up/shut-down sequence
        if self.use_second_shutdown:
            if self.on_time_2 < self.off_time_2:
                if script_time > self.on_time_2 and script_time <= self.off_time_2:
                    return
                elif self.on_time_2 > self.off_time_2:
                    if script_time > self.on_time_2 or script_time < self.off_time_2:
                        return
                else:
                    return

        if self.use_second_shutdown:
            mess = 'Script start time incompatible with instrument on/off time\n\n'\
                   'Script name: {}\nScript start time: {}\n'\
                   'Instrument start time: {}\nInstruments shutdown time: {}\nInstrument start time 2: {}\n' \
                   'Instrument shutdown time 2: {}\n'.format(script_name, script_time.strftime('%H:%M'),
                                                             self.on_time.strftime('%H:%M'),
                                                             self.off_time.strftime('%H:%M'),
                                                             self.on_time_2.strftime('%H:%M'),
                                                             self.off_time_2.strftime('%H:%M'))
        else:
            mess = 'Script start time incompatible with instrument on/off time\n\n' \
                   'Script name: {}\nScript start time: {}\nInstrument start time: {}\n' \
                   'Instruments shutdown time: {}\n'.format(script_name, script_time.strftime('%H:%M'),
                                                            self.on_time.strftime('%H:%M'),
                                                            self.off_time.strftime('%H:%M'))

        a = tk.messagebox.showwarning('Configuration incompatible', mess, parent=self.frame)
        self.frame.attributes('-topmost', 1)
        self.frame.attributes('-topmost', 0)

    def update_on_off(self):
        """Controls updating start/stop time of pi"""
        # Write wittypi schedule file locally
        write_witty_schedule_file(FileLocator.SCHEDULE_FILE, self.on_time, self.off_time)

        # Transfer file to instrument
        self.ftp.move_file_to_instrument(FileLocator.SCHEDULE_FILE, FileLocator.SCHEDULE_FILE_PI)

        # Open ssh and run wittypi update script
        ssh_cli = open_ssh(self.ftp.host_ip)

        std_in, std_out, std_err = ssh_cmd(ssh_cli, '(cd /home/pi/wittypi; sudo ./runScript.sh)', background=False)
        print(std_out.readlines())
        # print(std_err.readlines())
        close_ssh(ssh_cli)

        a = tk.messagebox.showinfo('Instrument update',
                                   'Updated instrument start-up/shut-down schedule:\n\n'
                                   'Start-up: {} UTC\n''Shut-down: {} UTC'.format(self.on_time.strftime('%H:%M'),
                                                                                  self.off_time.strftime('%H:%M')),
                                   parent=self.frame)

        self.frame.attributes('-topmost', 1)
        self.frame.attributes('-topmost', 0)

    def update_acq_time(self):
        """Updates acquisition period of instrument"""
        # Create strings
        temp_log_str = self.minute_cron_fmt(self.temp_logging)
        disk_space_str = self.minute_cron_fmt(self.check_disk_space)

        # Preparation of lists for writing crontab file
        times = [self.start_capt_time, self.stop_capt_time, self.start_dark_time, temp_log_str, disk_space_str]
        cmds = ['python3 {}'.format(self.start_script), 'python3 {}'.format(self.stop_script),
                'python3 {}'.format(self.dark_script), self.temp_script, 'python3 {}'.format(self.disk_space_script)]

        # Check time compatibility (only on scripts which have specific start times, not those run every x minutes)
        self.check_second_shutdown()
        for i, script_name in enumerate([self.start_script, self.stop_script, self.dark_script]):
            self.check_script_time(times[i], script_name)

        # Write crontab file
        write_script_crontab(FileLocator.SCRIPT_SCHEDULE, cmds, times)

        # Transfer file to instrument
        self.ftp.move_file_to_instrument(FileLocator.SCRIPT_SCHEDULE, FileLocator.SCRIPT_SCHEDULE_PI)

        # Setup crontab
        ssh_cli = open_ssh(self.ftp.host_ip)

        std_in, std_out, std_err = ssh_cmd(ssh_cli, 'crontab ' + FileLocator.SCRIPT_SCHEDULE_PI, background=False)
        close_ssh(ssh_cli)

        a = tk.messagebox.showinfo('Instrument update',
                                   'Updated instrument software schedules:\n\n'
                                   'Start capture script: {} UTC\n'
                                   'Shut-down capture script: {} UTC\n'
                                   'Dark capture time: {} UTC\n'
                                   'Log temperature: {} minutes\n'
                                   'Check disk space: {} minutes'.format(self.start_capt_time.strftime('%H:%M'),
                                                                         self.stop_capt_time.strftime('%H:%M'),
                                                                         self.start_dark_time.strftime('%H:%M'),
                                                                         self.temp_logging,
                                                                         self.check_disk_space))

        self.frame.attributes('-topmost', 1)
        self.frame.attributes('-topmost', 0)

    def minute_cron_fmt(self, minutes):
        """Creates the correct string for the crontab based on the minutes provided"""
        # Some initial organising for the temperature logging
        if minutes == 0:
            log_str = '#* * * * *'     # Script is turned off
        elif minutes == 60:
            log_str = '0 * * * *'      # Script is every hour
        else:
            log_str = '*/{} * * * *'.format(minutes)     # Script is every {} minutes
        return log_str

    def close_frame(self):
        self.in_frame = False
        self.frame.destroy()

    @property
    def on_time(self):
        """Return datetime object of time to turn pi on. Date is not important, only time, so use arbitrary date"""
        return datetime.datetime(year=2020, month=1, day=1, hour=self.on_hour, minute=self.on_min)

    @property
    def off_time(self):
        """Return datetime object of time to turn pi off. Date is not important, only time, so use arbitrary date"""
        return datetime.datetime(year=2020, month=1, day=1, hour=self.off_hour, minute=self.off_min)

    @property
    def on_time_2(self):
        """Return datetime object of time to turn pi on. Date is not important, only time, so use arbitrary date"""
        return datetime.datetime(year=2020, month=1, day=1, hour=self.on_hour_2, minute=self.on_min_2)

    @property
    def off_time_2(self):
        """Return datetime object of time to turn pi off. Date is not important, only time, so use arbitrary date"""
        return datetime.datetime(year=2020, month=1, day=1, hour=self.off_hour_2, minute=self.off_min_2)

    @property
    def start_capt_time(self):
        """Return datetime object of time to turn start acq. Date is not important, only time, so use arbitrary date"""
        return datetime.datetime(year=2020, month=1, day=1, hour=self.capt_start_hour, minute=self.capt_start_min)

    @property
    def stop_capt_time(self):
        """Return datetime object of time to turn stop acq. Date is not important, only time, so use arbitrary date"""
        return datetime.datetime(year=2020, month=1, day=1, hour=self.capt_stop_hour, minute=self.capt_stop_min)

    @property
    def start_dark_time(self):
        """Return datetime object of time to turn start acq. Date is not important, only time, so use arbitrary date"""
        return datetime.datetime(year=2020, month=1, day=1, hour=self.dark_capt_hour, minute=self.dark_capt_min)

    @property
    def on_hour(self):
        return self._on_hour.get()

    @on_hour.setter
    def on_hour(self, value):
        self._on_hour.set(value)

    @property
    def on_min(self):
        return self._on_min.get()

    @on_min.setter
    def on_min(self, value):
        self._on_min.set(value)

    @property
    def off_hour(self):
        return self._off_hour.get()

    @off_hour.setter
    def off_hour(self, value):
        self._off_hour.set(value)

    @property
    def off_min(self):
        return self._off_min.get()

    @off_min.setter
    def off_min(self, value):
        self._off_min.set(value)

    @property
    def on_hour_2(self):
        return self._on_hour_2.get()

    @on_hour_2.setter
    def on_hour_2(self, value):
        self._on_hour_2.set(value)

    @property
    def on_min_2(self):
        return self._on_min_2.get()

    @on_min_2.setter
    def on_min_2(self, value):
        self._on_min_2.set(value)

    @property
    def off_hour_2(self):
        return self._off_hour_2.get()

    @off_hour_2.setter
    def off_hour_2(self, value):
        self._off_hour_2.set(value)

    @property
    def off_min_2(self):
        return self._off_min_2.get()

    @off_min_2.setter
    def off_min_2(self, value):
        self._off_min_2.set(value)

    @property
    def use_second_shutdown(self):
        return self._use_second_shutdown.get()

    @use_second_shutdown.setter
    def use_second_shutdown(self, value):
        self._use_second_shutdown.set(value)

    @property
    def capt_start_hour(self):
        return self._capt_start_hour.get()

    @capt_start_hour.setter
    def capt_start_hour(self, value):
        self._capt_start_hour.set(value)

    @property
    def capt_start_min(self):
        return self._capt_start_min.get()

    @capt_start_min.setter
    def capt_start_min(self, value):
        self._capt_start_min.set(value)

    @property
    def capt_stop_hour(self):
        return self._capt_stop_hour.get()

    @capt_stop_hour.setter
    def capt_stop_hour(self, value):
        self._capt_stop_hour.set(value)

    @property
    def capt_stop_min(self):
        return self._capt_stop_min.get()

    @capt_stop_min.setter
    def capt_stop_min(self, value):
        self._capt_stop_min.set(value)

    @property
    def dark_capt_hour(self):
        return self._dark_capt_hour.get()

    @dark_capt_hour.setter
    def dark_capt_hour(self, value):
        self._dark_capt_hour.set(value)

    @property
    def dark_capt_min(self):
        return self._dark_capt_min.get()

    @dark_capt_min.setter
    def dark_capt_min(self, value):
        self._dark_capt_min.set(value)

    @property
    def temp_logging(self):
        return self._temp_logging.get()

    @temp_logging.setter
    def temp_logging(self, value):
        self._temp_logging.set(value)

    @property
    def check_disk_space(self):
        return self._check_disk_space.get()

    @check_disk_space.setter
    def check_disk_space(self, value):
        self._check_disk_space.set(value)