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
from re import sub
from hashlib import md5

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
from GTG.tools.dates import Date

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
        'tasks': {
            'get': BASE_URL + 'tasks/serial/',
            'new': BASE_URL + 'tasks/new/',
            'update': BASE_URL + 'tasks/update/',
        },
        'tags': BASE_URL + 'tags/all/',
    }
    
    CONVERT_24_HR = '%d/%m/%y'
    CONVERT_24_HR_WITH_TIME = '%d/%m/%y %H:%M:%S'
    
    LOCAL = 0
    REMOTE = 1
    
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
        self.hash_dict_path = os.path.join('backends/gtgonline/',
                                      "hash_dict-" + self.get_id())
        print "Data path = \n****\n****\n" + str(self.hash_dict_path) + "\n****\n****\n"
        self.hash_dict = self._load_pickled_file(self.hash_dict_path, default_value = {})
    
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
        self.save_state()
        
    def save_state(self):
        '''Saves the state of the synchronization'''
        print "Saving Data path = \n****\n****\n" + str(self.hash_dict_path) + "\n****\n****\n"
        print "Hash Dict = \n****\n****\n" + str(self.hash_dict) + "\n****\n****\n"
        self._store_pickled_file(self.data_path, self.sync_engine)
        self._store_pickled_file(self.hash_dict_path, self.hash_dict)
        
    def fetch_tasks_from_server(self, ):
        print "Fetching tasks started ..."
        params = {"email": self._parameters["username"],
                  "password": self._parameters["password"],}
        tasks = requests.post(self.URLS['tasks']['get'], \
                              params, proxies = self.NO_PROXY)
        print "response received = " + str(tasks.json)
        return tasks.json
    
    def process_tasks(self, remote_tasks):
        print "Tasks = " + str(remote_tasks)
        print "Backend id = " + self.get_id()
        
        local_tasks = self.datastore.get_all_tasks()
        gtg_titles_dic = {}
        remote_add = []
        update = []
        remote_delete = []
        local_delete = []
        remote_ids_list = []
        #server_ids = [task['id'] for task in remote_tasks]
        server_id_dict = {}
        local_id_dict = {}
        for task in remote_tasks:
            server_id_dict[str(task['id'])] = task
        
        print "server id dict = " + str(server_id_dict)
        
        for tid in local_tasks:
            gtg_task = self.datastore.get_task(tid)
            #if not self._gtg_task_is_syncable_per_attached_tags(gtg_task):
                #print "NOT SYNCABLE = " + gtg_task.get_title()
                #continue
            task_hash = self.get_or_create_hash_from_dict(tid)[0]
            remote_ids = gtg_task.get_remote_ids()
            print "Remote ids for " + tid + " = " + str(remote_ids)
            web_id = remote_ids.get(self.get_id(), None)
            print "web_id = " + str(web_id) + " server keys = " + str(server_id_dict.keys())
            if web_id == None:
                local_id_dict[tid] = gtg_task
                remote_add.append(gtg_task)
            else:
                local_id_dict[web_id] = gtg_task
                remote_ids_list.append(web_id)
                if web_id in server_id_dict.keys():
                    print "Sending task to update scenario"
                    self.process_update_scenario(gtg_task, \
                                                 server_id_dict[web_id], \
                                                 task_hash)
                else:
                    local_delete.append(gtg_task)
                    self.send_task_for_deletion(gtg_task)
            gtg_task.sync()
            
        new_remote_tasks = list(set(server_id_dict.keys()) - \
                                set(remote_ids_list))
        old_local_tasks = list(set(self.hash_dict.keys()) - set(local_tasks))
        
        print "*\n*\nRemote Tasks list = " + str(new_remote_tasks) + "\n*\n*\n"
        print "*\n*\nOld Local tasks list = " + str(old_local_tasks) + "\n*\n*\n"
        print "*\n*\nLocal Id dict = " + str(local_id_dict) + "\n*\n*\n"
        print "*\n*\nServer Id dict = " + str(server_id_dict) + "\n*\n*\n"
        #server_diff_tasks = list(set(server_id_dict.keys()) - \
                                #set(local_id_dict.keys()))
        #local_diff_tasks = list(set(local_id_dict.keys()) - \
                               #set(server_id_dict.keys()))
        #common_tasks = list(set(server_id_dict.keys()).intersection(local_id_dict.keys()))
        #print "SERVER New Tasks = "
        
        remote_add = self.modify_tasks_for_gtgonline(remote_add)
        id_dict = self.remote_add_tasks(remote_add)
        self.add_remote_id_to_local_tasks(id_dict)
        print "Id dict = " + str(id_dict)
        
        print "Remote add = " + str(remote_add)
        print "Update = " + str(update)
        print "Local delete = " + str(local_delete)
        
        self.save_state()
    
    def modify_tasks_for_gtgonline(self, task_list):
        details = {}
        for task in task_list:
            start_date = self.convert_date_to_str(task.get_start_date().date())
            due_date = self.convert_date_to_str(task.get_due_date().date())
            details[task.get_id()] = {
                'name': task.get_title(),
                'description': self.strip_xml_tags(task.get_text()),
                'start_date': start_date,
                'due_date': due_date,
                'status': task.get_status(),
                'subtasks': [subt.get_id() for subt in task.get_subtasks()]
            }
            #details.append()
        #print "Tasks Details = " + str(details)
        return details
    
    def remote_add_tasks(self, task_list):
        print "Adding tasks started ..."
        #print "Task list to send = " + json.dumps(task_list)
        params = {
            "email": self._parameters["username"],
            "password": self._parameters["password"],
            "task_list": json.dumps(task_list),
        }
        ids = requests.post(self.URLS['tasks']['new'], \
                            proxies = self.NO_PROXY, \
                            data = { key: str(value) for key, value in params.items() })
        #print "ids received = " + str(ids.json)
        return ids.json
    
    def add_remote_id_to_local_tasks(self, id_dict):
        for key, value in id_dict.iteritems():
            with self.datastore.get_backend_mutex():
                gtg_task = self.datastore.get_task(key)
                gtg_task.add_remote_id(self.get_id(), value)
                self.datastore.push_task(gtg_task)
    
    def process_update_scenario(self, local_task, remote_task, task_hash):
        task = self.get_latest_task(local_task, remote_task, task_hash)
        if task == local_task:
            print "Sent remote task to update"
            self.remote_update_task(local_task, remote_task['id'])
        elif task == remote_task:
            print "Send local task to update"
            self.local_update_task(remote_task, local_task)
    
    def get_latest_task(self, local_task, remote_task, task_hash):
        local_hash = self.compute_task_hash(local_task)
        remote_hash = self.compute_task_hash(remote_task, mode = self.REMOTE)
        
        if local_hash == task_hash == remote_hash:
            print "ALL HASHES ARE EQUAL"
            return None
        elif local_hash != task_hash and remote_hash == task_hash:
            print "Local is Latest. Update Remote"
            return local_task
        elif local_hash == task_hash and remote_hash != task_hash:
            print "Remote is Latest. Update Local"
            return remote_task
        else:
            print "BOTH HASHES ARE DIFFERENT, Update local"
            return remote_task
        
        '''
        local_mod = local_task.get_modified()
        remote_mod = self.str_to_datetime(remote_task['last_modified_date'])
        print "local_mod = " + str(local_mod) + " remote_due = " + str(remote_mod)
        
        if local_mod < remote_mod:
            print "Remote is Latest. Update Local"
            return remote_task
        else:
            print "Local is Latest. Update Remote"
            return local_task
        '''
    
    def remote_update_task(self, task, task_id):
        print "Updating remote task started ..."
        start_date = self.convert_date_to_str(task.get_start_date().date())
        due_date = self.convert_date_to_str(task.get_due_date().date())
        params = {
            "email": self._parameters["username"],
            "password": self._parameters["password"],
            "task_id": task_id,
            "name": task.get_title(),
            "description": self.strip_xml_tags(task.get_text()),
            "start_date": start_date,
            "due_date": due_date,
            "origin": "gtg",
        }
        response = requests.post(self.URLS['tasks']['update'], \
                                 params, proxies = self.NO_PROXY)
        
        print "Update response = " + str(response.json)
        return
    
    def local_update_task(self, remote_task, local_task):
        print "Updating local task started ..."
        local_task.set_title(_(remote_task["name"]))
        local_task.set_text(_(remote_task["description"]))
        local_task.set_status(remote_task["status"])
        
        start_date = self.str_to_datetime(remote_task["start_date"], \
                                        return_date = True, without_time = True)
        due_date = self.str_to_datetime(remote_task["due_date"], \
                                        return_date = True, without_time = True)
        local_task.set_start_date(Date(start_date))
        local_task.set_due_date(Date(due_date))
        new_tags = set(['@' + tag["name"] for tag in remote_task["tags"]])
        print "new_tags = " + str(new_tags)
        current_tags = set(local_task.get_tags_name())
        # remove the lost tags
        for tag in current_tags.difference(new_tags):
            local_task.remove_tag(tag)
        # add the new ones
        for tag in new_tags.difference(current_tags):
            local_task.add_tag(tag)
    
    def send_task_for_deletion(self, task):
        self.datastore.request_task_deletion(task.get_id())
    
    def fetch_tags_from_server(self, ):
        print "Fetching tags started ..."
        params = {"email": self._parameters["username"],
                  "password": self._parameters["password"],}
        tags = requests.post(self.URLS['tags'], \
                                      params, proxies = self.NO_PROXY)
        #print "response received = " + str(tags.json)
        return tags.json
    
    def process_tags(self, tags):
        print "Tags = " + str(tags)
        
    def strip_xml_tags(self, text):
        text = sub(r"<.?content>", "", text)
        text = sub(r"<.?tag>", "", text)
        text = sub(r"<subtask>.*</subtask>\n*", "", text)
        return text
    
    def convert_date_to_str(self, date_obj):
        return date_obj.strftime(self.CONVERT_24_HR)
    
    def str_to_datetime(self, date_str, return_date = False, \
                        without_time = False):
        try:
            if without_time:
                datetime_obj = datetime.datetime.strptime(date_str, \
                                                      self.CONVERT_24_HR)
            else:
                datetime_obj = datetime.datetime.strptime(date_str, \
                                                  self.CONVERT_24_HR_WITH_TIME)
        except Exception:
            return None
        if return_date:
            return datetime_obj.date()
        return datetime_obj
    
    def set_task(self, task):
        print "BACKEND_GTGONLINE : Set task was called"
        #task.sync()
        self.save_state()
    
    def get_or_create_hash_from_dict(self, task_id):
        task_hash_tuple = self.hash_dict.get(task_id, (None, None))
        if task_hash_tuple[0] == None:
            task = self.datastore.get_task(task_id)
            task_hash = self.compute_task_hash(task, mode = self.LOCAL)
            remote_ids = task.get_remote_ids()
            web_id = remote_ids.get(self.get_id(), None)
            task_hash_tuple = (task_hash, web_id)
            self.hash_dict[task_id] = task_hash_tuple
        return task_hash_tuple
    
    def compute_task_hash(self, task, mode = None):
        if mode == self.REMOTE:
            in_str = task['name']
            in_str += task['description']
            in_str += task['start_date']
            in_str += task['due_date']
        else:
            in_str = task.get_title()
            in_str += self.strip_xml_tags(task.get_text())
            in_str += self.convert_date_to_str(task.get_start_date().date())
            in_str += self.convert_date_to_str(task.get_due_date().date())
        return md5(in_str).hexdigest()
