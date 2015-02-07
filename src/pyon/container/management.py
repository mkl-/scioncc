#!/usr/bin/env python

""" WELCOME to the home of container management

    This is a framework for performing administrative actions on a pycc container or a full distributed system of containers.

    A request is an event of type ContainerManagementRequest and contains a selector and an action.
    The selector is used to determine which containers the action should be performed on
    (or from the point of view of one container, if this action should be performed on this container).
    The action describes the change requested, and may be performed by one or more handlers.
    Handlers are registered with the system and can choose which actions they are able to perform.

    There is a peer relationship between IonObject subclasses ContainerManagementPredicate.
    The normal python object can be created from the peer by calling ContainerSelector.from_object(ion_object)
    and the Ion object can be created from the peer object by calling obj.get_peer()

    To maintain this relationship:
    1) the Ion object definition should include an (unused) peer attribute
       with decorators module (string name of the module containing the peer class) and class (optional string name of the peer class).
       If the class decorator is missing, the peer is assumed to have the same class name as the Ion object.
    2) the peer object class should define get_peer_class() (string name of Ion object type)
       and get_peer_args() (dictionary of attribute values for the Ion object)
"""

import sys
import gc
from threading import Lock

from putil.logging import log, config
from putil.timer import get_accumulators

from pyon.core.bootstrap import CFG
from pyon.container.snapshot import ContainerSnapshot
from pyon.core import bootstrap
from pyon.core.bootstrap import IonObject
from pyon.ion.event import EventPublisher, EventSubscriber
from pyon.util.tracer import CallTracer

from interface.objects import ContainerManagementRequest, ChangeLogLevel, ReportStatistics, ClearStatistics, \
    ResetPolicyCache, TriggerGarbageCollection, TriggerContainerSnapshot, PrepareSystemShutdown


# define selectors to determine if this message should be handled by this container.
# used by the message, manager should not interact with this directly

class ContainerSelector(object):
    """ base class for predicate classes to select which containers should handle messages
    """
    def __init__(self, peer):
        self.peer = peer

    def should_handle(self, container):
        raise Exception('subclass must override this method')

    def get_peer(self):
        return IonObject(self.get_peer_class(), **self.get_peer_args())

    def get_peer_class(self):
        return self.__class__.__name__

    def get_peer_args(self):
        return {}

    def __str__(self):
        return self.__class__.__name__

    @classmethod
    def from_object(cls, obj):
        """ get peer type from Ion object """
        mod = obj.get_decorator_value('peer', 'module')
        clazz = obj.get_decorator_value('peer', 'class') or obj.__class__.__name__
        subclass = getattr(sys.modules[mod], clazz)
        return subclass(obj)


class AllContainers(ContainerSelector):
    """ all containers should perform the action """
    def should_handle(self, container):
        return True

# TODO: more selectors
#class ContainersByName(ContainerSelector):
#class ContainersByIP(ContainerSelector):
#class ContainersRunningProcess(ContainerSelector):
#class ContainersInExecutionEngine(ContainerSelector):


# define types of messages that can be sent and handled

class EventHandler(object):
    """ base class for event handler objects registered to handle a particular type of container management action """
    def can_handle_request(self, action):
        raise Exception('subclass must implement this method')
    def handle_request(self, action):
        raise Exception('subclass must implement this method')
    def __str__(self):
        """ subclass should implement better name if behavior varies with args """
        return self.__class__.__name__


class LogLevelHandler(EventHandler):
    def can_handle_request(self, action):
        return isinstance(action, ChangeLogLevel)
    def handle_request(self, action):
        config.set_level(action.logger, action.level, action.recursive)


class StatisticsHandler(EventHandler):
    def can_handle_request(self, action):
        return isinstance(action, ReportStatistics) or isinstance(action, ClearStatistics)
    def handle_request(self, action):
        if isinstance(action, ReportStatistics):
            for a in get_accumulators().values():
                a.log()
        else:
            for a in get_accumulators().values():
                a.clear()


class PolicyCacheHandler(EventHandler):
    def can_handle_request(self, action):
        return isinstance(action, ResetPolicyCache)
    def handle_request(self, action):
        if bootstrap.container_instance.has_capability(bootstrap.container_instance.CCAP.GOVERNANCE_CONTROLLER):
            bootstrap.container_instance.governance_controller.reset_policy_cache()


class GarbageCollectionHandler(EventHandler):
    def can_handle_request(self, action):
        return isinstance(action, TriggerGarbageCollection)
    def handle_request(self, action):
        gc.collect()


class ContainerSnapshotHandler(EventHandler):
    def can_handle_request(self, action):
        return isinstance(action, TriggerContainerSnapshot)
    def handle_request(self, action):
        try:
            cs = ContainerSnapshot(bootstrap.container_instance)
            if action.clear_all:
                cs.clear_snapshots()
                log.info("Container %s snapshot cleared (id=%s)" % (bootstrap.container_instance.id,
                                                                    bootstrap.container_instance.proc_manager.cc_id))
                return

            cs.take_snapshot(snapshot_id=action.snapshot_id, snapshot_kwargs=action.snapshot_kwargs,
                             include_list=action.include_snapshots, exclude_list=action.exclude_snapshots)
            if action.persist_snapshot:
                cs.persist_snapshot()
                log.info("Container %s snapshot persisted (id=%s)" % (bootstrap.container_instance.id,
                                                                      bootstrap.container_instance.proc_manager.cc_id))
            else:
                cs.log_snapshot()
        except Exception as ex:
            log.warn("Error taking container snapshot", exc_info=True)


class PrepareSystemShutdownHandler(EventHandler):
    def can_handle_request(self, action):
        return isinstance(action, PrepareSystemShutdown)
    def handle_request(self, action):
        # TODO: Perform some sensible action here
        # Mode: stop all listeners from consuming
        # Mode: disconnect all listerers
        # Mode: interrupt all processing
        pass


# TODO: other useful administrative actions
#    """ request that containers perform a thread dump """
#    """ request that containers log timing stats """
#    """ request that containers clear all timing stats """


# event listener to handle the messages

SEND_RESULT_IF_NOT_SELECTED = False  # terrible idea... but might want for debug or audit?

DEFAULT_HANDLERS = [ LogLevelHandler(), StatisticsHandler(), PolicyCacheHandler(), GarbageCollectionHandler(),
                     ContainerSnapshotHandler(), PrepareSystemShutdownHandler() ]


class ContainerManager(object):
    def __init__(self, container, handlers=DEFAULT_HANDLERS):
        self.container = container
        self.running = False
        # make sure start() completes before an event is handled,
        # and any event is either handled before stop() begins,
        # or the handler begins after stop() completes and the event is dropped
        self.lock = Lock()
        self.handlers = handlers[:]

    def start(self):
        # Install the container tracer (could be its own
        self.container_tracer = ContainerTracer()
        self.container_tracer.start_tracing()
        self.container.tracer = CallTracer
        self.container.tracer.configure(CFG.get_safe("container.tracer", {}))

        ## create queue listener and publisher
        self.sender = EventPublisher(event_type="ContainerManagementResult")
        self.receiver = EventSubscriber(event_type="ContainerManagementRequest", callback=self._receive_event)
        with self.lock:
            self.running = True
            self.receiver.start()
        log.info('ready for container management requests')

    def stop(self):
        log.debug('container management stopping')
        with self.lock:
            self.receiver.stop()
            self.sender.close()
            self.running = False
        log.debug('container management stopped')

        self.container_tracer.stop_tracing()

    def add_handler(self, handler):
        self.handlers.append(handler)

    def _get_handlers(self, action):
        out = []
        for handler in self.handlers:
            if handler.can_handle_request(action):
                out.append(handler)
        return out

    def _receive_event(self, event, headers):
        with self.lock:
            if not isinstance(event, ContainerManagementRequest):
                log.trace('ignoring wrong type event: %r', event)
                return
            if not self.running:
                log.warn('ignoring admin message received after shutdown: %s', event.action)
                return
            predicate = ContainerSelector.from_object(event.predicate)
            if predicate.should_handle(self.container):
                log.trace('handling admin message: %s', event.action)
                self._perform_action(event.action)
            else:
                log.trace('ignoring admin action: %s', event.action)
                if SEND_RESULT_IF_NOT_SELECTED:
                    self.sender.publish_event(origin=self.container.id, action=event.action, outcome='not selected')
                    log.debug('received action: %s, outcome: not selected', event.action)

    def _perform_action(self, action):
        handlers = self._get_handlers(action)
        if not handlers:
            log.info('action accepted but no handlers found: %s', action)
            result = 'unhandled'
            self.sender.publish_event(origin=self.container.id, action=action, outcome=str(result))
            log.debug('received action: %s, outcome: %s', action, result)
        else:
            for handler in handlers:
                try:
                    result = handler.handle_request(action) or "completed"
                except Exception,e:
                    log.error("handler %r failed to perform action: %s", handler, action, exc_info=True)
                    result = e
                self.sender.publish_event(origin=self.container.id, action=action, outcome=str(result))
                log.debug('performed action: %s, outcome: %s', action, result)


class ContainerTracer(object):
    """Sets up the CallTracer utility for entities within the container"""

    SAVE_MSG_MAX = 1000

    def start_tracing(self):
        # Messaging tracing
        CallTracer.set_formatter("MSG.out", self._msg_trace_formatter)
        CallTracer.set_formatter("MSG.in", self._msg_trace_formatter)

        from pyon.net import endpoint
        endpoint.callback_msg_out = self.trace_message_out
        endpoint.callback_msg_in = self.trace_message_in

    def stop_tracing(self):
        from pyon.net import endpoint
        endpoint.callback_msg_out = None

    @staticmethod
    def trace_message_in(msg, headers, env):
        log_entry = dict(status="RECV %s bytes" % len(msg), headers=headers, env=env,
                         content_length=len(msg), content=str(msg)[:ContainerTracer.SAVE_MSG_MAX])
        CallTracer.log_scope_call("MSG.in", log_entry, include_stack=False)

    @staticmethod
    def trace_message_out(msg, headers, env):
        log_entry = dict(status="SENT %s bytes" % len(msg), headers=headers, env=env,
                         content_length=len(msg), content=str(msg)[:ContainerTracer.SAVE_MSG_MAX])
        CallTracer.log_scope_call("MSG.out", log_entry, include_stack=False)

    @staticmethod
    def _msg_trace_formatter(log_entry, **kwargs):
        # Warning: Make sure this code is reentrant. Will be called multiple times for the same entry
        frags = []
        msg_type = "UNKNOWN"
        sub_type = ""
        try:
            content = log_entry.get("content", "")
            headers = dict(log_entry.get("headers", {}))
            env = log_entry.get("env", {})

            if "sender" in headers or "sender-service" in headers:
                # Case RPC msg
                sender_service = headers.get('sender-service', '')
                sender = headers.pop('sender', '').split(",", 1)[-1]
                sender_name = headers.pop('sender-name', '')
                sender_txt = (sender_name or sender_service) + " (%s)" % sender if sender else ""
                recv = headers.pop('receiver', '?').split(",", 1)[-1]
                op = "op=%s" % headers.pop('op', '?')
                sub_type = op
                stat = "status=%s" % headers.pop('status_code', '?')
                conv_seq = headers.get('conv-seq', '0')

                if conv_seq == 1:
                    msg_type = "RPC REQUEST"
                    frags.append("%s %s -> %s %s" % (msg_type, sender_txt, recv, op))
                else:
                    msg_type = "RPC REPLY"
                    frags.append("%s %s -> %s %s" % (msg_type, sender_txt, recv, stat))
                try:
                    import msgpack
                    msg = msgpack.unpackb(content)
                    frags.append("\n C:")
                    frags.append(str(msg))
                except Exception as ex:
                    pass
            else:
                # Case event/other msg
                try:
                    import msgpack
                    msg = msgpack.unpackb(content)
                    ev_type = msg["type_"] if isinstance(msg, dict) and "type_" in msg else "?"
                    msg_type = "EVENT"
                    sub_type = ev_type
                    frags.append("%s %s" % (msg_type, ev_type))
                    frags.append("\n C:")
                    frags.append(str(msg))

                except Exception:
                    msg_type = "UNKNOWN"
                    frags.append(msg_type)
                    frags.append("\n C:")
                    frags.append(content)

            frags.append("\n H:")
            frags.append(str(headers))
            frags.append("\n E:")
            frags.append(str(env))
        except Exception as ex:
            frags = ["ERROR parsing message: %s" % str(ex)]
        log_entry["statement"] = "".join(frags)
        log_entry["msg_type"] = msg_type
        log_entry["sub_type"] = sub_type

        return CallTracer._default_formatter(log_entry, **kwargs)

