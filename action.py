#!/usr/bin/env python
# coding: utf-8

from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__ = 'GPL v3'
__copyright__ = '2013, Greg Riker <griker@hotmail.com>'
__docformat__ = 'restructuredtext en'

import os, sys, threading, time

from zipfile import ZipFile

from PyQt4.Qt import (pyqtSignal, QIcon, QMenu, QTimer, QToolButton, QUrl)

from calibre.constants import DEBUG, isosx, iswindows
from calibre.devices.idevice.libimobiledevice import libiMobileDevice
from calibre.gui2 import open_url
from calibre.gui2.actions import InterfaceAction
from calibre.gui2.device import device_signals
from calibre.gui2.dialogs.message_box import MessageBox
from calibre.devices.usbms.driver import debug_print
from calibre.utils.config import config_dir

from calibre_plugins.marvin_manager import MarvinManagerPlugin
from calibre_plugins.marvin_manager.book_status import BookStatusDialog
from calibre_plugins.marvin_manager.common_utils import (IndexLibrary,
    ProgressBar, Struct,
    get_icon, set_plugin_icon_resources)
import calibre_plugins.marvin_manager.config as cfg

# The first icon is the plugin icon, referenced by position.
# The rest of the icons are referenced by name
PLUGIN_ICONS = ['images/icon.png']

class MarvinManagerAction(InterfaceAction):

    # Location reporting template
    LOCATION_TEMPLATE = "{cls}:{func}({arg1}) {arg2}"

    icon = PLUGIN_ICONS[0]
    name = 'Marvin Mangler'
    prefs = cfg.plugin_prefs
    verbose = prefs.get('debug_plugin', False)

    # Declare the main action associated with this plugin
    action_spec = ('Marvin Mangler', None, None, None)
    popup_type = QToolButton.InstantPopup

    marvin_device_status_changed = pyqtSignal(str)
    plugin_device_connection_changed = pyqtSignal(object)

    def about_to_show_menu(self):
        self.rebuild_menus()

    def backup_restore(self):
        self._log_location("not implemented")

    def create_menu_item(self, m, menu_text, image=None, tooltip=None, shortcut=None):
        ac = self.create_action(spec=(menu_text, None, tooltip, shortcut), attr=menu_text)
        if image:
            ac.setIcon(QIcon(image))
        m.addAction(ac)
        return ac

    # subclass override
    def genesis(self):
        self._log_location("v%d.%d.%d" % MarvinManagerPlugin.version)

        # General initialization, occurs when calibre launches
        self.connected_device = None
        self.marvin_content_invalid = False
        self.menus_lock = threading.RLock()
        self.sync_lock = threading.RLock()
        self.connected_device = None
        self.indexed_library = None
        self.library_indexed = False
        self.library_last_modified = None
        self.reconnect_request_pending = False
        self.resources_path = os.path.join(config_dir, 'plugins', "%s_resources" % self.name.replace(' ', '_'))

        # Read the plugin icons and store for potential sharing with the config widget
        icon_resources = self.load_resources(PLUGIN_ICONS)
        set_plugin_icon_resources(self.name, icon_resources)

        # Piggyback on the device driver's connection to Marvin
        self.ios = None

        # Build an opts object
        self.opts = self.init_options()

        # Assign our menu to this action and an icon
        self.menu = QMenu(self.gui)
        self.qaction.setMenu(self.menu)
        self.qaction.setIcon(get_icon(PLUGIN_ICONS[0]))
        self.qaction.triggered.connect(self.main_menu_button_clicked)
        self.menu.aboutToShow.connect(self.about_to_show_menu)

        # Init the prefs file
        self.init_prefs()

        # Populate the help resources
        self.inflate_help_resources()

        # Populate icons
        self.inflate_icon_resources()

    def inflate_icon_resources(self):
        '''
        Extract the icon resources from the plugin
        '''
        icons = []
        with ZipFile(self.plugin_path, 'r') as zf:
            for candidate in zf.namelist():
                if candidate.endswith('/'):
                    continue
                if candidate.startswith('icons/'):
                    icons.append(candidate)
        ir = self.load_resources(icons)
        for icon in icons:
            if not icon in ir:
                continue
            fs = os.path.join(self.resources_path, icon)
            if not os.path.exists(fs):
                if not os.path.exists(os.path.dirname(fs)):
                    os.makedirs(os.path.dirname(fs))
                with open (fs, 'wb') as f:
                    f.write(ir[icon])

    def inflate_help_resources(self):
        '''
        Extract the help resources from the plugin
        '''
        help_resources = []
        with ZipFile(self.plugin_path, 'r') as zf:
            for candidate in zf.namelist():
                if candidate == 'help/help.html' or candidate.startswith('help/images/'):
                    help_resources.append(candidate)

        rd = self.load_resources(help_resources)
        for resource in help_resources:
            if not resource in rd:
                continue
            fs = os.path.join(self.resources_path, resource)
            if os.path.isdir(fs) or fs.endswith('/'):
                continue
            if not os.path.exists(os.path.dirname(fs)):
                os.makedirs(os.path.dirname(fs))
            with open(fs, 'wb') as f:
                f.write(rd[resource])

    def init_options(self, disable_caching=False):
        """
        Build an opts object with a ProgressBar
        """
        opts = Struct(
            gui=self.gui,
            icon=get_icon(PLUGIN_ICONS[0]),
            ios = self.ios,
            parent=self,
            prefs=self.prefs,
            resources_path=self.resources_path,
            verbose=DEBUG)

        opts['pb'] = ProgressBar(parent=self.gui, window_title=self.name)
        self._log_location()
        return opts

    def init_prefs(self):
        '''
        Set the initial default values as needed
        '''
        pref_map = {
            'plugin_version': "%d.%d.%d" % self.interface_action_base_plugin.version}
        for pm in pref_map:
            if not self.prefs.get(pm, None):
                self.prefs.set(pm, pref_map[pm])

    # subclass override
    def initialization_complete(self):
        self.rebuild_menus()

        # Subscribe to device connection events
        device_signals.device_connection_changed.connect(self.on_device_connection_changed)

    def launch_library_scanner(self):
        '''
        Call IndexLibrary() to index current_db by uuid, title
        Need a test to see if db has been updated since last run. Until then,
        optimization disabled.
        After indexing, self.library_scanner.uuid_map and .title_map are populated
        '''
        if (self.library_last_modified == self.gui.current_db.last_modified() and
                self.indexed_library is self.gui.current_db and
                self.library_indexed):
            self._log_location("library index current")
        else:
            self._log_location("updating library index")
            self.library_scanner = IndexLibrary(self)
            self.connect(self.library_scanner, self.library_scanner.signal, self.library_index_complete)
            QTimer.singleShot(1, self.start_library_indexing)

    # subclass override
    def library_changed(self, db):
        self._log_location()
        self.library_indexed = False
        self.indexed_library = None
        self.library_last_modified = None

    def library_index_complete(self):
        self._log_location()
        self.library_indexed = True
        self.indexed_library = self.gui.current_db
        self.library_last_modified = self.gui.current_db.last_modified()

    def main_menu_button_clicked(self):
        '''
        This isn't being called
        '''
        self._log_location()
        if self.connected_device:
            self.show_installed_books()
        else:
            self.show_configuration()

    def marvin_status_changed(self, command):
        '''
        The Marvin driver emits a signal after completion of protocol commands.
        This method receives the notification. If the content on Marvin changed
        as a result of the operation, we need to invalidate our cache of Marvin's
        installed books.
        '''
        self.marvin_device_status_changed.emit(command)

        self._log_location(command)
        if command in ['delete_books', 'upload_books']:
            self.marvin_content_invalid = True

    def on_device_connection_changed(self, is_connected):
        '''
        self.connected_device is the handle to the driver.
        '''
        self.plugin_device_connection_changed.emit(is_connected)
        if is_connected:
            self.connected_device = self.gui.device_manager.device

            self._log_location(self.connected_device.gui_name)

            if (hasattr(self.connected_device, 'ios_reader_app') and
                self.connected_device.ios_reader_app == 'Marvin'):
                if not self.reconnect_request_pending:
                    self.launch_library_scanner()

                    # Subscribe to Marvin driver change events
                    self.connected_device.marvin_device_signals.reader_app_status_changed.connect(
                        self.marvin_status_changed)
                else:
                    self._log("reconnect request pending…")

        else:
            self._log_location("device disconnected")
            self.connected_device.marvin_device_signals.reader_app_status_changed.disconnect()
            self.connected_device = None
            self.library_scanner.hash_map = None

            if hasattr(self, 'book_status_dialog') and self.book_status_dialog.reconnect_request_pending:
                self.reconnect_request_pending = True
                self.book_status_dialog.close()
                self.book_status_dialog = None

        self.rebuild_menus()

    def rebuild_menus(self):
        self._log_location()
        with self.menus_lock:
            m = self.menu
            m.clear()

            # Add 'About…'
            ac = self.create_menu_item(m, 'About' + '…')
            ac.triggered.connect(self.show_about)
            m.addSeparator()

            # Add menu options for connected Marvin
            if self.connected_device:
                if (self.connected_device.ios_reader_app == 'Marvin' and
                    self.connected_device.ios_connection['connected'] is True):
                    self._log("Marvin connected")
                    ac = self.create_menu_item(m, 'Marvin Library', image=I("dialog_information.png"))
                    ac.triggered.connect(self.show_installed_books)

                    ac = self.create_menu_item(m, 'Backup or Restore Library', image=I("swap.png"))
                    ac.triggered.connect(self.backup_restore)

                    ac = self.create_menu_item(m, 'Reset Marvin Library', image=I("trash.png"))
                    ac.triggered.connect(self.reset_marvin_library)

                    self.ios = self.connected_device.ios

                    # If reconnecting, allow time for Device to be added before redisplaying
                    if self.reconnect_request_pending:
                        self.reconnect_request_pending = False
                        QTimer.singleShot(100, self.show_installed_books)
                else:
                    self._log("Marvin not connected")
                    ac = self.create_menu_item(m, 'Marvin not connected')
                    ac.setEnabled(False)
            else:
                self._log("Marvin not connected")
                ac = self.create_menu_item(m, 'Marvin not connected')
                ac.setEnabled(False)
            m.addSeparator()

            # Add 'Customize plugin…'
            ac = self.create_menu_item(m, 'Customize plugin' + '…', image=I("config.png"))
            ac.triggered.connect(self.show_configuration)

            m.addSeparator()

            # Add 'Help'
            ac = self.create_menu_item(m, 'Help', image=I('help.png'))
            ac.triggered.connect(self.show_help)

    def reset_marvin_library(self):
        self._log_location("not implemented")

    def show_configuration(self):
        self.interface_action_base_plugin.do_user_config(self.gui)

    def show_about(self):
        version = self.interface_action_base_plugin.version
        title = "%s v %d.%d.%d" % (self.name, version[0], version[1], version[2])
        msg = ('<p>To learn more about this plugin, visit the '
               '<a href="http://www.mobileread.com/forums/showthread.php?t=205062">THIS NEEDS TO BE ADDED</a> '
               'at MobileRead’s Calibre forum.</p>')
        text = get_resources('about.txt')
        text = text.decode('utf-8')
        d = MessageBox(MessageBox.INFO, title, msg, det_msg=text, show_copy_button=False)
        d.exec_()

    def show_help(self):
        path = os.path.join(self.resources_path, 'help/help.html')
        open_url(QUrl.fromLocalFile(path))

    def show_installed_books(self):
        '''
        Show Marvin Library spreadsheet
        '''
        self._log_location()
        self.book_status_dialog = BookStatusDialog(self, 'marvin_library')
        self.book_status_dialog.initialize(self)
        self.book_status_dialog.exec_()

    # subclass override
    def shutting_down(self):
        self._log_location()

    def start_library_indexing(self):
        self.library_scanner.start()

    def _log(self, msg=None):
        '''
        Print msg to console
        '''
        if not self.verbose:
            return

        if msg:
            debug_print(" %s" % str(msg))
        else:
            debug_print()

    def _log_location(self, *args):
        '''
        Print location, args to console
        '''
        if not self.verbose:
            return

        arg1 = arg2 = ''

        if len(args) > 0:
            arg1 = str(args[0])
        if len(args) > 1:
            arg2 = str(args[1])

        debug_print(self.LOCATION_TEMPLATE.format(cls=self.__class__.__name__,
            func=sys._getframe(1).f_code.co_name,
            arg1=arg1, arg2=arg2))

