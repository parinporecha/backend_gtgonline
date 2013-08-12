# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Getting Things GNOME! - a personal organizer for the GNOME desktop
# Copyright (c) 2013-2014 - Parin Porecha
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.
# -----------------------------------------------------------------------------

'''
GTGOnline! backend
'''

import os
import cgi
import uuid
import time
import threading
import datetime
import subprocess
import exceptions
from dateutil.tz import tzutc, tzlocal

from GTG.backends.genericbackend import GenericBackend
from GTG import _
from GTG.backends.backendsignals import BackendSignals
from GTG.backends.syncengine import SyncEngine, SyncMeme
from GTG.backends.rtm.rtm import createRTM, RTMError, RTMAPIError
from GTG.backends.periodicimportbackend import PeriodicImportBackend
from GTG.tools.dates import Date
from GTG.core.task import Task
from GTG.tools.interruptible import interruptible
from GTG.tools.logger import Log

class Backend(PeriodicImportBackend):
    """
    GTGOnline! Backend
    """

    _general_description = {
        GenericBackend.BACKEND_NAME: "backend_gtgonline",
        GenericBackend.BACKEND_HUMAN_NAME: _("GTGOnline!"),
        GenericBackend.BACKEND_AUTHORS: ["Parin Porecha"],
        GenericBackend.BACKEND_TYPE: GenericBackend.TYPE_READWRITE,
        GenericBackend.BACKEND_DESCRIPTION:
        _("This service synchronizes your tasks with Getting Things Gnome's"
          " Web Application - GTGOnline!\n\n"
          "Note: This product uses the GTGOnline! API and is"
          " certified by GTGOnline!\n"
          "How cool is that !"),
    }

    _static_parameters = {
        "username": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_STRING,
            GenericBackend.PARAM_DEFAULT_VALUE:
                'user@example.com', },
        "password": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_PASSWORD,
            GenericBackend.PARAM_DEFAULT_VALUE: '', },
        "period": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_INT,
            GenericBackend.PARAM_DEFAULT_VALUE: 5, },
    }
    
    def __init__(self, params):
        """ Constructor of the object """
        super(Backend, self).__init__(params)
        self._sync_tasks = set()
        self._changed_locally = set()
        self._changed_remotely = set()
        # loading the saved state of the synchronization, if any
        self.data_path = os.path.join('backends/gtgonline/',
                                      "sync_engine-" + self.get_id())
        self.sync_engine = self._load_pickled_file(self.data_path,
                                                   SyncEngine())
    
    def initialize(self):
        """ This is called when a backend is enabled """
        super(Backend, self).initialize()
        tasks = self.datastore.get_all_tasks()
        print "parameters = " + str(self._parameters)
        print "tasks = " + str(tasks)
        gtg_titles_dic = {}
        for tid in self.datastore.get_all_tasks():
            gtg_task = self.datastore.get_task(tid)
            if not self._gtg_task_is_syncable_per_attached_tags(gtg_task):
                print "NOT SYNCABLE = " + gtg_task.get_title()
                continue
            gtg_title = gtg_task.get_title()
            print "Task name = " + gtg_title
            if gtg_title in gtg_titles_dic:
                gtg_titles_dic[gtg_task.get_title()].append(tid)
            else:
                gtg_titles_dic[gtg_task.get_title()] = [tid]
        print "titles dic = " + str(gtg_titles_dic)
