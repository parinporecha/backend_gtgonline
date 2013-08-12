# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Gettings Things Gnome! - a personal organizer for the GNOME desktop
# Copyright (c) 2012-2013 - Izidor Matušov
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

"""
PubSub backends which synchronizes tasks over XMPP
"""

import glob
import logging
import os
import re
import uuid
import xml.dom.minidom

from GTG import _
from GTG.backends.backendsignals import BackendSignals
from GTG.backends.genericbackend import GenericBackend
from GTG.tools.taskxml import task_from_xml, task_to_xml
from GTG.tools.logger import Log

from sleekxmpp import ClientXMPP
from sleekxmpp.exceptions import IqError
from sleekxmpp.plugins import xep_0004
from sleekxmpp.xmlstream import ET, tostring

from xdg.BaseDirectory import xdg_cache_home

# How much logs do you want to see?
if False:
    logging.basicConfig(level=logging.DEBUG,
        format='%(levelname)-8s %(message)s')
else:
    logging.basicConfig(level=logging.ERROR,
        format='%(levelname)-8s %(message)s')


class Backend(GenericBackend):
    """
    PubSub backend
    """
    _general_description = {
        GenericBackend.BACKEND_NAME: "backend_pubsub",
        GenericBackend.BACKEND_HUMAN_NAME: _("PubSub"),
        GenericBackend.BACKEND_AUTHORS: ["Izidor Matušov"],
        GenericBackend.BACKEND_TYPE: GenericBackend.TYPE_READWRITE,
        GenericBackend.BACKEND_DESCRIPTION:
            _("Synchronize your tasks over XMPP using PubSub"),
    }

    _static_parameters = {
        "username": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_STRING,
            GenericBackend.PARAM_DEFAULT_VALUE:
                'user@example.com', },
        "password": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_PASSWORD,
            GenericBackend.PARAM_DEFAULT_VALUE: '', },
    }

    def __init__(self, params):
        """ Constructor of the object """
        super(Backend, self).__init__(params)
        self._xmpp = None
        self._sync_tasks = set()
        self._changed_locally = set()
        self._changed_remotely = set()

    def initialize(self):
        """ This is called when a backend is enabled """
        super(Backend, self).initialize()

        if "state" not in self._parameters:
            self._parameters["state"] = "start"

        # Prepare parameters
        jid = self._parameters["username"]
        password = self._parameters["password"]
        server = "pubsub." + jid.split('@', 1)[-1]

        self._xmpp = PubsubClient(jid, password, server)
        self._xmpp.register_callback("failed_auth", self.on_failed_auth)
        self._xmpp.register_callback("connected", self.on_connected)
        self._xmpp.register_callback("disconnected", self.on_disconnected)

        if self._xmpp.connect(reattempt=False):
            self._xmpp.process()
        else:
            Log.error("Can't connect to XMPP")
            if self._parameters["state"] == "start":
                self.on_failed_auth()

        BackendSignals().connect('sharing-changed', self.on_sharing_changed)

        self._xmpp.register_callback("project_tag", self.on_new_remote_project)
        self._xmpp.register_callback("set_task", self.on_remote_set_task)
        self._xmpp.register_callback("rm_task", self.on_remote_rm_task)

    def on_failed_auth(self):
        """ Provided credencials are not valid.

        Disable this instance and show error to user """
        Log.error('Failed to authenticate')
        BackendSignals().backend_failed(self.get_id(),
                        BackendSignals.ERRNO_AUTHENTICATION)
        self.quit(disable=True)

    def on_connected(self):
        """ Get the initial set of the tasks from the XMPP """
        if self._parameters["state"] != "onine":
            self._parameters["state"] = "online"

        # Ensure all teams
        tag_tree = self.datastore.get_tagstore().get_main_view()
        for tag_id in tag_tree.get_all_nodes():
            tag = self.datastore.get_tag(tag_id)
            team = tag.get_people_shared_with()
            if len(team) > 0:
                self._xmpp.ensure_team(tag_id, team)

        # Fetch initial tasks
        for task_id, tag, raw_xml in self._xmpp.get_tasks():
            # Parse the raw_xml by DOM
            doc = xml.dom.minidom.parseString(raw_xml)
            task_xml = doc.getElementsByTagName("task")[0]

            # Create a new task or return the existing one with the same id
            task = self.datastore.task_factory(task_id)
            task = task_from_xml(task, task_xml)
            task.add_tag(tag)
            self.datastore.push_task(task)

            self._sync_tasks.add(task_id)
            Log.info("(init) PubSub --set--> GTG: [%s] '%s'" %
                (task_id, task.get_title()))

    def on_disconnected(self):
        """ When disconnected """
        self._parameters["state"] = "offline"

    def on_sharing_changed(self, sender, tag_id):
        """ Changed sharing settings """
        tag = self.datastore.get_tag(tag_id)
        team = tag.get_people_shared_with()
        if self._xmpp:
            self._xmpp.ensure_team(tag_id, team)

    #### OVERRIDDEN METHODS ###################################################
    def get_contacts(self):
        """ Return all contacts to whom the user can share tasks """
        if self._xmpp:
            return self._xmpp.get_contacts()
        else:
            return []

    def get_user_person_id(self):
        """ Return person_id for this user """
        if self._xmpp:
            return self._xmpp.get_user_person_id()
        else:
            return None

    def save_state(self):
        """ The last function before shutting this backend down.

        Disconnect XMPP and store picked_file """
        Log.info("Quitting backend")
        self._xmpp.disconnect()
        Log.info("Backend is shut down")

    #### LOCAL CHANGES ########################################################
    def set_task(self, task):
        """ Propagate a change in local tasks into server """
        if self._parameters["state"] != "online":
            return

        sync_tags = self._xmpp.get_synchronized_tags()
        tags = task.get_tags_name()
        tag_overlap = set(sync_tags) & set(tags)
        if not tag_overlap:
            return

        if task.get_id() in self._changed_remotely:
            self._changed_remotely.remove(task.get_id())
            return

        Log.info("GTG --set--> PubSub: [%s] '%s'" % (task.get_id(),
                task.get_title()))

        doc = xml.dom.minidom.parseString("<task></task>")
        task_id = task.get_id()
        task_xml = task_to_xml(doc, task).toxml()
        tags = task.get_tags_name()

        self._xmpp.set_task(task_id, tags, task_xml)
        self._sync_tasks.add(task_id)
        self._changed_locally.add(task_id)

    def remove_task(self, task_id):
        """ After removing local task remove tasks from the server """
        if self._parameters["state"] != "online":
            return
        if task_id in self._sync_tasks:
            Log.info("GTG --del--> PubSub: [%s]" % task_id)
            self._xmpp.delete_task(task_id)
            self._sync_tasks.remove(task_id)
            if task_id in self._changed_locally:
                self._changed_locally.remove(task_id)
            if task_id in self._changed_remotely:
                self._changed_remotely.remove(task_id)

    #### REMOTE CHANGES #######################################################
    def on_new_remote_project(self, tag_id, team_jids):
        """ New project was added on server """
        if self._parameters["state"] != "online":
            return
        team = [unicode(member) for member in team_jids]
        Log.info("Pubsub --new project--> GTG: %s, teammates: %s" % (tag_id,
                team))

        tag = self.datastore.get_tag(tag_id)
        if tag is None:
            tag = self.datastore.new_tag(tag_id)
        tag.set_people_shared_with(team)

    def on_remote_set_task(self, task_id, raw_xml):
        """ Task was set on server """
        if self._parameters["state"] != "online":
            return

        if task_id in self._changed_locally:
            self._changed_locally.remove(task_id)
            return

        Log.info("PubSub --set--> GTG: [%s]" % task_id)

        # Parse XML string into <task> node
        xmldoc = xml.dom.minidom.parseString(raw_xml)
        task_xml = xmldoc.getElementsByTagName("task")[0]

        self._changed_remotely.add(task_id)
        task = self.datastore.get_task(task_id)
        if task:
            # Already exists
            task = task_from_xml(task, task_xml)
        else:
            # New task
            task = self.datastore.task_factory(task_id)
            task = task_from_xml(task, task_xml)
            self.datastore.push_task(task)

    def on_remote_rm_task(self, task_id):
        """ Task was removed on server """
        if task_id not in self._sync_tasks:
            # This task is not synchronized via this synchronization service
            return

        if task_id in self._changed_locally:
            self._changed_locally.remove(task_id)
        if task_id in self._changed_remotely:
            self._changed_remotely.remove(task_id)

        Log.info("PubSub --del--> GTG: [%s]" % task_id)
        self.datastore.request_task_deletion(task_id)
        if task_id in self._sync_tasks:
            self._sync_tasks.remove(task_id)


class PubsubClient(ClientXMPP):
    """ Client which deals with the underlying XMPP.

    It provides a nice, higher level interface. You can either connect to
    signals using register_callback() to receive events or call methods
    to change tasks. """

    # Cache dir for avatars
    AVATARS_DIR = os.path.join(xdg_cache_home, "gtg", "avatars")

    # How many items do you want to share over XMPP?
    MAX_ITEMS = 9999

    def __init__(self, jid, password, server):
        super(PubsubClient, self).__init__(jid, password)

        # Register plugins for XMPP
        # vCards
        self.register_plugin('xep_0054')
        # PubSub
        self.register_plugin('xep_0060')

        self._my_jid = jid
        self._pubsub = server

        # List of subscribed nodes
        self._nodes = {}
        # Register callbacks for events
        self._callbacks = {}

        # Hold teams for nodes
        self._teams = {}

        self.add_event_handler('session_start', self._on_start, threaded=True)
        self.add_event_handler('failed_auth', self._on_failed_auth)
        self.add_event_handler('pubsub_publish', self._on_published)
        self.add_event_handler('pubsub_retract', self._on_retract)

    def register_callback(self, name, func):
        """ Register a function for given callback

        There are possible callbacks:
          * connected() -- successfully connected
          * failed_auth() -- failed authentication
          * project_tag(name, teammates) -- a project was added or updated
          * set_task(task_id, task_xml) -- a task was modified
          * rm_task(task_id) -- a task was removed
        """
        self._callbacks[name] = func

    def _callback(self, name, *args):
        """ Trigger a callback defined by its name and pass arguments """
        if name in self._callbacks:
            self._callbacks[name](*args)
        else:
            Log.error("Unknown callback '%s'(%s)" % (name, args))

    #### Helper methods #######################################################
    def _get_avatar(self, jid):
        """ Return avatar for jid if it is possible.

        The avatar is cached for the future runs.
        """
        if not os.path.exists(self.AVATARS_DIR):
            os.makedirs(self.AVATARS_DIR)

        # If avatar was cached, return it
        avatars = glob.glob(os.path.join(self.AVATARS_DIR, jid + ".*"))
        if len(avatars) > 0:
            return avatars[0]

        # Download vCard and avatar in it
        vcard = self['xep_0054'].get_vcard(jid)
        img_type = vcard['vcard_temp']['PHOTO']['TYPE']
        photo = vcard['vcard_temp']['PHOTO']['BINVAL']

        # Determine a name for the file
        if not img_type.startswith("image/") or " " in img_type:
            return None
        suffix = img_type[len("image/"):]
        name = os.path.join(self.AVATARS_DIR, "%s.%s" % (jid, suffix))

        Log.info("Saving avatar for '%s'" % jid)
        with open(name, 'wb') as avatar_file:
            avatar_file.write(photo)

        return name

    def _get_subscribed_nodes(self):
        """ Return list of subscribed nodes """
        result = self['xep_0060'].get_subscriptions(self._pubsub)
        return [subscription['node'] for subscription
            in result['pubsub']['subscriptions']['substanzas']
            if subscription['subscription'] == 'subscribed']

    def _discover_nodes(self):
        """ Discover all nodes user can access """
        subscriptions = self._get_subscribed_nodes()
        self._nodes = {}

        affiliations = self['xep_0060'].get_affiliations(self._pubsub)
        affiliations = affiliations['pubsub']['affiliations']
        if 'substanzas' not in affiliations.values:
            # No nodes available
            return
        for affiliation in affiliations.values['substanzas']:
            affiliation, node = affiliation['affiliation'], affiliation['node']
            if affiliation == 'owner' and node.startswith('GTG_'):
                # Check node config
                config = self['xep_0060'].get_node_config(self._pubsub, node)
                values = config['pubsub_owner']['configure']['form']['values']

                form = xep_0004.Form()
                form.add_field(var='FORM_TYPE', type='hidden',
                    value='http://jabber.org/protocol/pubsub#node_config')

                if int(values['pubsub#max_items']) < self.MAX_ITEMS:
                    Log.info("Max items is set only to %s" %
                        values['pubsub#max_items'])
                    form.add_field(var='pubsub#max_items',
                        value=str(self.MAX_ITEMS))

                if values['pubsub#access_model'] != 'whitelist':
                    form.add_field(var='pubsub#access_model',
                        value='whitelist')

                if not values['pubsub#notify_delete']:
                    form.add_field(var='pubsub#notify_delete', value='1')

                if not values['pubsub#notify_config']:
                    form.add_field(var='pubsub#notify_config', value='1')

                m = re.match('Project (@\w+)', values['pubsub#title'])
                if not m:
                    Log.warning("Malformed node name '%s'" %
                        values['pubsub#title'])
                    continue

                project_name = m.group(1)
                self._nodes[node] = project_name
                Log.info("Discovered project '%s'" % project_name)

                if len(form.field) > 1:
                    form['type'] = 'submit'
                    self['xep_0060'].set_node_config(self._pubsub, node, form)

                if node not in subscriptions:
                    self['xep_0060'].subscribe(self._pubsub, node)

                # Find teammates for cache
                self._teams[node] = self._get_teammates(node)

    def _create_node(self, tag):
        """ Create a new node for tag """
        name = 'GTG_%s' % uuid.uuid4()
        form = xep_0004.Form()
        form.add_field(var='pubsub#max_items', value=str(self.MAX_ITEMS))
        form.add_field(var='pubsub#access_model', value='whitelist')
        form.add_field(var='pubsub#notify_delete', value='1')
        form.add_field(var='pubsub#notify_config', value='1')
        title = "Project %s" % tag
        form.add_field(var='pubsub#title', value=title)

        Log.info("Creating node '%s' for tag %s" % (name, tag))
        self['xep_0060'].create_node(self._pubsub, name, config=form)
        self['xep_0060'].subscribe(self._pubsub, name)
        self._nodes[name] = tag
        return name

    def _get_teammates(self, node):
        """ Return a simple list of teammembers JID given the node """
        result = self['xep_0060'].get_node_affiliations(self._pubsub, node)
        affiliations = result['pubsub_owner']['affiliations']['substanzas']
        self._teams[node] = [aff['jid'] for aff in affiliations]
        return self._teams[node]

    def _set_teammates(self, node, new_teammates):
        """ Ensure that the list of teammates is this one """
        if len(new_teammates) > 0:
            former = set(self._get_teammates(node))
            new_teammates = set(new_teammates)

            to_add = [(jid, 'owner') for jid in (new_teammates - former)]
            to_remove = [(jid, 'none') for jid in (former - new_teammates)]
            changes = to_add + to_remove

            if len(changes) > 0:
                self['xep_0060'].modify_affiliations(self._pubsub, node,
                    changes)

            self._teams[node] = new_teammates
        else:
            # For empty teams delete node
            self['xep_0060'].delete_node(self._pubsub, node)
            self._nodes.pop(node)
            self._teams.pop(node)

    #### Handling events ######################################################
    def _on_failed_auth(self, stanza):
        """ Let know the backend about failed authentication """
        self._callback('failed_auth')

    def _on_start(self, event):
        """ Do stuff after connection

        Fetch a list of assigned nodes, create a home node if needed. Get
        avatars if there are not available. """

        Log.info("Connected to PubSub")

        # Get roster notifications
        self.get_roster()
        self.send_presence()

        # Discover nodes and notify GTG about project nodes
        self._discover_nodes()
        for node, name in self._nodes.items():
            self._callback("project_tag", name, self._get_teammates(node))

        self._callback("connected")

    def _on_published(self, msg):
        """ A task was modified by a teammate """
        task_id = msg['pubsub_event']['items']['item']['id']
        raw_xml = tostring(msg['pubsub_event']['items']['item']['payload'])
        self._callback("set_task", task_id, raw_xml)

    def _on_retract(self, msg):
        """ Task was deleted by a teammate """
        task_id = msg['pubsub_event']['items']['retract']['id']
        self._callback("rm_task", task_id)

    #### PUBLIC INTERFACE #####################################################
    def get_synchronized_tags(self):
        """ Return list of all synchronized tags """
        return self._nodes.values()

    def get_contacts(self):
        """ Return available contacts """
        used_contacts = set()

        contacts = [(self._my_jid, _("Me"),
            self._get_avatar(self._my_jid))]
        used_contacts.add(self._my_jid)

        for contact in list(self.client_roster.keys()):
            if contact != self._my_jid:
                # Use either name or JID if name not available
                name = self.client_roster[contact]['name']
                if name.strip() == "":
                    name = contact

                avatar = self._get_avatar(contact)
                contacts.append((contact, name, avatar))
                used_contacts.add(contact)

        for team in self._teams.values():
            for jid in team:
                if jid not in used_contacts:
                    contacts.append((jid, jid, None))
                    used_contacts.add(jid)

        return contacts

    def get_user_person_id(self):
        """ Return person_id for this user """
        return self._my_jid

    def get_calendar(self, node):
        """ Return calendar for node """
        item = self['xep_0060'].get_item(self._pubsub, node, 'calendar')
        item = ['pubsub']['items']['substanzas'][0]['payload']
        return tostring(item)

    def set_calendar(self, node, calendar_str):
        """ Set calendar of node """
        payload = ET.fromstring(calendar_str)
        self['xep_0060'].publish(self._pubsub, node, id='calendar',
            payload=payload)

    def get_tasks(self):
        """ Return list of all available tasks """
        Log.info("Looking for available tasks")
        for node in self._nodes:
            project_tag = self._nodes[node]
            result_items = self['xep_0060'].get_items(self._pubsub, node,
                    max_items=self.MAX_ITEMS, block=True)
            items = result_items['pubsub']['items']['substanzas']
            for item in items:
                yield item['id'], project_tag, tostring(item['payload'])

    def set_task(self, task_id, tags, task):
        """ Publish task to teammates """
        payload = ET.fromstring(task)

        for node, project_tag in self._nodes.items():
            if project_tag in tags:
                self['xep_0060'].publish(self._pubsub, node, id=task_id,
                        payload=payload)
            else:
                # Has this node still this task?
                item_req = self['xep_0060'].get_item(self._pubsub, node,
                            task_id)
                items = item_req['pubsub']['items']['substanzas']
                if len(items) > 0:
                    self['xep_0060'].retract(self._pubsub, node, task_id)

    def delete_task(self, task_id):
        """ Delete task also for teammates """
        for node in self._nodes:
            try:
                self['xep_0060'].retract(self._pubsub, node, task_id,
                    notify="true")
            except IqError:
                # Task was not in the node
                pass

    def ensure_team(self, tag, team):
        """ Set the team members of the tag

        If a node for the tag doesn't exists, create it. """
        Log.info("Set team for tag '%s' to '%s'" % (tag, team))
        team_node = None
        for node, associated_tag in self._nodes.items():
            if associated_tag == tag:
                team_node = node
                break

        if team_node is None:
            team_node = self._create_node(tag)

        self._set_teammates(team_node, team)
