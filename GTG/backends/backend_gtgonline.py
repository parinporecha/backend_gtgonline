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
import requests
import json
import cookielib

from dateutil.tz import tzutc, tzlocal
from lxml import html

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
    
    # USE BELOW ONLY IF ACCESSING LOCALHOST INSIDE CAMPUS
    NO_PROXY = {'no': 'pass'}
    
    BASE_URL = "http://localhost:8000/"
    URLS = {
        'auth': BASE_URL + 'user/auth_gtg/',
        'tasks': BASE_URL + 'tasks/serial/',
        'tags': BASE_URL + 'tags/all/',
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
        #hdr = {'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8', 'Referer': login_url}
        
        proxy = os.environ.get('http_proxy')
        print "proxy = " + str(proxy)
        #proxy = urllib2.ProxyHandler({'http': proxy})
        #opener = urllib2.build_opener(proxy)
        #urllib2.install_opener(opener)
        
        #cj = cookielib.CookieJar()

        #opener = urllib2.build_opener(
            #urllib2.HTTPCookieProcessor(cj), 
        #)
        
        #login_form = opener.open("http://gtgonline-parinporecha.rhcloud.com/user/landing/").read()
        #csrf_token = html.fromstring(login_form).xpath(
            #'//input[@name="csrfmiddlewaretoken"]/@value'
        #)[0]
        #print "csrf token = " + csrf_token
        
        self.try_auth()
        print "returned here"
            
    def try_auth(self):
        params = {"email": self._parameters["username"],
                  "password": self._parameters["password"],}
        auth_response = requests.post(self.URLS['auth'], \
                                      params, proxies = self.NO_PROXY)
        if auth_response.text != '1':
            self.auth_has_failed()
        #try:
        #    data = urllib.urlencode(params)
        #    print "data = " + data
        #    request = urllib2.Request(login_url, data)
        #    page = urllib2.urlopen(request)
        #    content = page.read()
        #    print "content = " + content
        #    if content == '0':
        #        self.auth_has_failed()
        #except urllib2.HTTPError, e:
        #    print "error = " + e.fp.read()
    
    def auth_has_failed(self):
        """
        Provided credentials are not valid.
        Disable this instance and show error to user
        """
        #Log.error('Failed to authenticate')
        BackendSignals().backend_failed(self.get_id(),
                        BackendSignals.ERRNO_AUTHENTICATION)
        self.quit(disable=True)
        
    def do_periodic_import(self, ):
        # Start working on this
        print "Importing ..."
        tasks = self.fetch_tasks_from_server()
        self.process_tasks(tasks)
        tags = self.fetch_tags_from_server()
        self.process_tags(tags)
        
    def save_state(self):
        '''Saves the state of the synchronization'''
        self._store_pickled_file(self.data_path, self.sync_engine)
        
    def fetch_tasks_from_server(self, ):
        print "Fetching tasks started ..."
        params = {"email": self._parameters["username"],
                  "password": self._parameters["password"],}
        tasks = requests.post(self.URLS['tasks'], \
                                      params, proxies = self.NO_PROXY)
        print "response received = " + str(tasks.json)
    
    def process_tasks(self, tasks):
        print "Tasks = " + str(tasks)
    
    def fetch_tags_from_server(self, ):
        print "Fetching tags started ..."
        params = {"email": self._parameters["username"],
                  "password": self._parameters["password"],}
        tags = requests.post(self.URLS['tags'], \
                                      params, proxies = self.NO_PROXY)
        print "response received = " + str(tags.json)
    
    def process_tags(self, tags):
        print "Tags = " + str(tags)
    
