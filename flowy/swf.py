import json
import logging
import uuid
from collections import namedtuple
from functools import partial
from itertools import ifilter, imap
from pkgutil import simplegeneric

from boto.swf.exceptions import SWFResponseError, SWFTypeAlreadyExistsError
from boto.swf.layer1 import Layer1
from boto.swf.layer1_decisions import Layer1Decisions

__all__ = ['CachingClient', 'SWFClient']


class SWFClient(object):
    def __init__(self, domain, client=None):
        self._client = client if client is not None else Layer1()
        self._domain = domain
        self._scheduled_activities = []
        self._scheduled_workflows = []
        self._scheduled_timers = []

    def register_workflow(self, name, version, task_list,
                          execution_start_to_close=3600,
                          task_start_to_close=60,
                          child_policy='TERMINATE',
                          descr=None):
        v = str(version)
        estc = str(execution_start_to_close)
        tstc = str(task_start_to_close)
        try:
            self._client.register_workflow_type(
                domain=self._domain,
                name=name,
                version=v,
                task_list=task_list,
                default_child_policy=child_policy,
                default_execution_start_to_close_timeout=estc,
                default_task_start_to_close_timeout=tstc,
                description=descr
            )
            logging.info("Registered workflow: %s %s", name, version)
        except SWFTypeAlreadyExistsError:
            logging.warning("Workflow already registered: %s %s",
                            name, version)
            try:
                reg_w = self._client.describe_workflow_type(
                    domain=self._domain, workflow_name=name, workflow_version=v
                )
            except SWFResponseError:
                logging.warning("Could not check workflow defaults: %s %s",
                                name, version)
                return False
            conf = reg_w['configuration']
            reg_estc = conf['defaultExecutionStartToCloseTimeout']
            reg_tstc = conf['defaultTaskStartToCloseTimeout']
            reg_tl = conf['defaultTaskList']['name']
            reg_cp = conf['defaultChildPolicy']

            if (reg_estc != estc
                    or reg_tstc != tstc
                    or reg_tl != task_list
                    or reg_cp != child_policy):
                logging.warning("Registered workflow "
                                "has different defaults: %s %s",
                                name, version)
                return False
        except SWFResponseError:
            logging.warning("Could not register workflow: %s %s",
                            name, version, exc_info=1)
            return False
        return True

    def register_activity(self, name, version, task_list, heartbeat=60,
                          schedule_to_close=420, schedule_to_start=120,
                          start_to_close=300, descr=None):
        version = str(version)
        schedule_to_close = str(schedule_to_close)
        schedule_to_start = str(schedule_to_start)
        start_to_close = str(start_to_close)
        heartbeat = str(heartbeat)
        try:
            self._client.register_activity_type(
                domain=self._domain,
                name=name,
                version=version,
                task_list=task_list,
                default_task_heartbeat_timeout=heartbeat,
                default_task_schedule_to_close_timeout=schedule_to_close,
                default_task_schedule_to_start_timeout=schedule_to_start,
                default_task_start_to_close_timeout=start_to_close,
                description=descr
            )
            logging.info("Registered activity: %s %s", name, version)
        except SWFTypeAlreadyExistsError:
            logging.warning("Activity already registered: %s %s",
                            name, version)
            try:
                reg_a = self._client.describe_activity_type(
                    domain=self._domain, activity_name=name,
                    activity_version=version
                )
            except SWFResponseError:
                logging.warning("Could not check activity defaults: %s %s",
                                name, version)
                return False
            conf = reg_a['configuration']
            reg_tstc = conf['defaultTaskStartToCloseTimeout']
            reg_tsts = conf['defaultTaskScheduleToStartTimeout']
            reg_tschtc = conf['defaultTaskScheduleToCloseTimeout']
            reg_hb = conf['defaultTaskHeartbeatTimeout']
            reg_tl = conf['defaultTaskList']['name']

            if (reg_tstc != start_to_close
                    or reg_tsts != schedule_to_start
                    or reg_tschtc != schedule_to_close
                    or reg_hb != heartbeat
                    or reg_tl != task_list):
                logging.warning("Registered activity "
                                "has different defaults: %s %s",
                                name, version)
                return False
        except SWFResponseError:
            logging.warning("Could not register activity: %s %s",
                            name, version, exc_info=1)
            return False
        return True

    def start_workflow(self, name, version, task_list, input,
                       workflow_id=None):
        if workflow_id is None:
            workflow_id = uuid.uuid4()
        try:
            r = self._client.start_workflow_execution(
                domain=self._domain,
                workflow_id=str(workflow_id),
                workflow_name=name,
                workflow_version=str(version),
                task_list=task_list,
                input=input
            )
        except SWFResponseError:
            logging.warning("Could not start workflow: %s %s",
                            name, version, exc_info=1)
            return None
        return r['runId']

    def poll_decision(self, task_list):
        poller = partial(self._client.poll_for_decision_task,
                         task_list=task_list, domain=self._domain,
                         reverse_order=True)

        first_page = _repeated_poller(poller)

        def all_events():
            page = first_page
            while 1:
                for event in page['events']:
                    yield event
                if not page.get('nextPageToken'):
                    break
                # If a workflow is stopped and a decision page fetching fails
                # forever we avoid infinite loops
                p = _repeated_poller(
                    poller, next_page_token=page['nextPageToken'], retries=3
                )
                if p is None:
                    raise PageError()
                assert (
                    p['taskToken'] == page['taskToken']
                    and (
                        p['workflowType']['name']
                        == page['workflowType']['name'])
                    and (
                        p['workflowType']['version']
                        == page['workflowType']['version'])
                    and (
                        p.get('previousStartedEventId')
                        == page.get('previousStartedEventId')
                    )
                ), 'Inconsistent decision pages.'
                page = p

        return DecisionResponse(
            name=first_page['workflowType']['name'],
            version=first_page['workflowType']['version'],
            token=first_page['taskToken'],
            last_event_id=first_page.get('previousStartedEventId'),
            events_iter=ifilter(None, imap(_event_factory, all_events()))
        )

    def poll_activity(self, task_list):
        poller = partial(self._client.poll_for_activity_task,
                         task_list=task_list, domain=self._domain)
        response = _repeated_poller(poller)
        return ActivityResponse(
            name=response['activityType']['name'],
            version=response['activityType']['version'],
            input=response['input'],
            token=response['taskToken']
        )

    def queue_activity(self, call_id, name, version, input,
                       heartbeat=None,
                       schedule_to_close=None,
                       schedule_to_start=None,
                       start_to_close=None,
                       task_list=None,
                       context=None):
        self._scheduled_activities.append((
            (str(call_id), name, str(version)),
            {
                'heartbeat_timeout': _str_or_none(heartbeat),
                'schedule_to_close_timeout': _str_or_none(schedule_to_close),
                'schedule_to_start_timeout': _str_or_none(schedule_to_start),
                'start_to_close_timeout': _str_or_none(start_to_close),
                'task_list': task_list,
                'input': input,
                'control': context,
            }
        ))

    def queue_subworkflow(self, workflow_id, name, version, input,
                          task_start_to_close=None,
                          execution_start_to_close=None,
                          task_list=None,
                          context=None):
        self._scheduled_workflows.append((
            (name, str(version), str(workflow_id)),
            {
                'execution_start_to_close_timeout': execution_start_to_close,
                'task_start_to_close_timeout': task_start_to_close,
                'task_list': task_list,
                'input': input,
                'control': context,
            }
        ))

    def queue_timer(self, call_id, delay, context=None):
        self._scheduled_timers.append((str(delay), str(call_id), context))

    def schedule_queued(self, token, context=None):
        d = Layer1Decisions()
        for args, kwargs in self._scheduled_activities:
            d.schedule_activity_task(*args, **kwargs)
            name, version = args[1:]
            logging.info("Scheduled activity: %s %s", name, version)
        for args, kwargs in self._scheduled_workflows:
            d.start_child_workflow_execution(*args, **kwargs)
            name, version = args[:2]
            logging.info("Scheduled child workflow: %s %s", name, version)
        for args in self._scheduled_timers:
            d.start_timer(*args)
        data = d._data
        try:
            self._client.respond_decision_task_completed(
                task_token=token, decisions=data, execution_context=context
            )
        except SWFResponseError:
            logging.warning("Could not send decisions: %s", token, exc_info=1)
            return False
        finally:
            self._scheduled_activities = []
            self._scheduled_workflows = []
            self._scheduled_timers = []
        return True

    def complete_workflow(self, token, result=None):
        d = Layer1Decisions()
        d.complete_workflow_execution(result=result)
        data = d._data
        try:
            self._client.respond_decision_task_completed(
                task_token=token, decisions=data
            )
            logging.info("Completed workflow: %s %s", token, result)
        except SWFResponseError:
            logging.warning("Could not complete the workflow: %s",
                            token, exc_info=1)
            return False
        return True

    def fail_workflow(self, token, reason):
        d = Layer1Decisions()
        d.fail_workflow_execution(reason=reason)
        data = d._data
        try:
            self._client.respond_decision_task_completed(
                task_token=token, decisions=data
            )
            logging.info("Terminated workflow: %s", reason)
        except SWFResponseError:
            logging.warning("Could not fail the workflow: %s",
                            token, exc_info=1)
            return False
        return True

    def complete_activity(self, token, result):
        try:
            self._client.respond_activity_task_completed(
                task_token=token, result=result
            )
            logging.info("Completed activity: %s %r", token, result)
        except SWFResponseError:
            logging.warning("Could not complete activity: %s",
                            token, exc_info=1)
            return False
        return True

    def fail_activity(self, token, reason):
        try:
            self._client.respond_activity_task_failed(task_token=token,
                                                      reason=reason)
            logging.info("Failed activity: %s %s", token, reason)
        except SWFResponseError:
            logging.warning("Could not terminate activity: %s",
                            token, exc_info=1)
            return False
        return True

    def heartbeat(self, token):
        try:
            self._client.record_activity_task_heartbeat(task_token=token)
            logging.info("Sent activity heartbeat: %s", token)
        except SWFResponseError:
            logging.warning("Error when sending activity heartbeat: %s",
                            token, exc_info=1)
            return False
        return True


DecisionResponse = namedtuple(
    'DecisionResponse',
    'name version events_iter last_event_id token'
)

ActivityResponse = namedtuple(
    'ActivityResponse',
    'name version input token'
)


class PageError(RuntimeError):
    """ Raised when a page in a decision response is unavailable. """


def _repeated_poller(poller, retries=-1, **kwargs):
    response = {}
    while 'taskToken' not in response or not response['taskToken']:
        try:
            response = poller(**kwargs)
        except (IOError, SWFResponseError):
            logging.warning("Unknown error when polling.", exc_info=1)
        if retries == 0:
            return
        retries = max(retries - 1, -1)
    return response


def _make_event_factory(event_map):
    tuples = {}
    for event_class_name, attrs in event_map.values():
        tuples[event_class_name] = namedtuple(event_class_name, attrs.keys())

    globals().update(tuples)

    def factory(event):
        event_type = event['eventType']
        if event_type in event_map:
            event_class_name, attrs = event_map[event_type]
            kwargs = {}
            for attr_name, attr_path in attrs.items():
                attr_value = event
                for attr_path_part in attr_path.split('.'):
                    attr_value = attr_value.get(attr_path_part)
                kwargs[attr_name] = attr_value
            event_class = tuples.get(event_class_name, lambda **k: None)
            return event_class(**kwargs)
        return None

    return factory


# Dynamically create all the event tuples and a factory for them
_event_factory = _make_event_factory({
    # Activities

    'ActivityTaskScheduled': ('ActivityScheduled', {
        'event_id': 'eventId',
        'call_id': 'activityTaskScheduledEventAttributes.activityId',
        'context': 'activityTaskScheduledEventAttributes.control',
    }),
    'ActivityTaskCompleted': ('ActivityCompleted', {
        'event_id': 'activityTaskCompletedEventAttributes.scheduledEventId',
        'result': 'activityTaskCompletedEventAttributes.result',
    }),
    'ActivityTaskFailed': ('ActivityFailed', {
        'event_id': 'activityTaskFailedEventAttributes.scheduledEventId',
        'reason': 'activityTaskFailedEventAttributes.reason',
    }),
    'ActivityTaskTimedOut': ('ActivityTimedout', {
        'event_id': 'activityTaskTimedOutEventAttributes.scheduledEventId',
    }),

    # Subworkflows

    'StartChildWorkflowExecutionInitiated': ('SubworkflowStarted', {
        'event_id': 'startChildWorkflowExecutionInitiatedEventAttributes'
                    '.workflowId',
        'context': '.startChildWorkflowExecutionInitiatedEventAttribute'
                   '.control'
    }),

    'ChildWorkflowExecutionCompleted': ('SubworkflowCompleted', {
        'event_id': 'childWorkflowExecutionCompletedEventAttributes'
                    '.workflowExecution.workflowId',
        'result': 'childWorkflowExecutionCompletedEventAttributes.result',
    }),

    'ChildWorkflowExecutionFailed': ('SubworkflowFailed', {
        'event_id': 'childWorkflowExecutionFailedEventAttributes'
                    '.workflowExecution.workflowId',
        'reason': 'childWorkflowExecutionFailedEventAttributes.reason',
    }),

    'StartChildWorkflowExecutionFailed': ('SubworkflowFailed', {
        'event_id': 'startChildWorkflowExecutionFailed'
                    '.workflowExecution.workflowId',
        'reason': 'startChildWorkflowExecutionFailed.cause',
    }),

    'ChildWorkflowExecutionTimedOut': ('SubworkflowTimedout', {
        'event_id': 'childWorkflowExecutionTimedOutEventAttributes'
                    '.workflowExecution.workflowId',
    }),

    # Timers

    'TimerStarted': ('TimerStarted', {
        'call_id': 'timerStartedEventAttributes.timerId',
        'context': 'timerStartedEventAttributes.control',
    }),
    'TimerFired': ('TimerFired', {
        'call_id': 'timerFiredEventAttributes.timerId',
    }),

    # Misc

    'WorkflowExecutionStarted': ('WorkflowStarted', {
        'input': 'workflowExecutionStartedEventAttributes.input',
    }),
    'DecisionTaskCompleted': ('DecisionCompleted', {
        'context': 'decisionTaskCompletedEventAttributes.executionContext',
        'started_by': 'decisionTaskCompletedEventAttributes.startedEventId',
    }),
})


class ActivityTask(object):
    def __init__(self, client, token):
        self._client = client
        self._token = token

    def complete(self, result):
        """ Triggers the successful completion of the activity with *result*.

        Returns a boolean indicating the success of the operation.

        """
        return self._client.complete_activity(token=self._token, result=result)

    def fail(self, reason):
        """ Triggers the failure of the activity for the specified reason.

        Returns a boolean indicating the success of the operation.

        """
        return self._client.fail_activity(token=self._token, reason=reason)

    def heartbeat(self):
        """ Report that the activity is still making progress.

        Returns a boolean indicating the success of the operation or whether
        the heartbeat exceeded the time it should have taken to report activity
        progress. In the latter case the activity execution should be stopped.

        """
        return self._client.heartbeat(token=self._token)


class CachingDecision(object):
    def __init__(self, client, new_events, token, execution_context=None):
        self._client = client
        # Cache the events in case of an iterator because we may need to walk
        # over it multiple times
        self._new_events = tuple(new_events)
        self._token = token
        self._contexts = {}
        self._to_call_id = {}
        self._running = set()
        self._timedout = set()
        self._results = {}
        self._errors = {}
        self._fired = set()
        self._global_context = None
        self._is_finished = False

        if execution_context is not None:
            self.load(execution_context)

        self._setup_internal_dispatch()
        self.update(new_events)

    def _setup_internal_dispatch(self):
        iu = self._internal_update = simplegeneric(self._internal_update)
        iu.register(ActivityScheduled, self._activity_scheduled)  # noqa
        iu.register(ActivityCompleted, self._job_completed)  # noqa
        iu.register(ActivityFailed, self._job_failed)  # noqa
        iu.register(ActivityTimedout, self._job_timedout)  # noqa
        iu.register(SubworkflowStarted, self._subworkflow_started)  # noqa
        iu.register(SubworkflowCompleted, self._job_completed)  # noqa
        iu.register(SubworkflowFailed, self._job_failed)  # noqa
        iu.register(SubworkflowTimedout, self._job_timedout)  # noqa
        iu.register(TimerStarted, self._timer_started)  # noqa
        iu.register(TimerFired, self._timer_fired)  # noqa

    def update(self, new_events):
        for event in self._new_events:
            self._internal_update(event)

    def _internal_update(self, event):
        """ Dispatch an event for internal purposes. """

    def _activity_scheduled(self, event):
        self._to_call_id[event.event_id] = event.call_id
        self._running.add(event.call_id)

    def _subworkflow_started(self, event):
        call_id = self._to_call_id[event.event_id]
        self._running.add(call_id)

    def _job_completed(self, event):
        call_id = self._to_call_id[event.event_id]
        assert call_id not in self._results
        assert call_id not in self._errors
        assert call_id not in self._timedout
        self._running.remove(call_id)
        self._results[call_id] = event.result

    def _job_failed(self, event):
        call_id = self._to_call_id[event.event_id]
        assert call_id not in self._results
        assert call_id not in self._errors
        assert call_id not in self._timedout
        self._running.remove(call_id)
        self._errors[call_id] = event.reason

    def _job_timedout(self, event):
        call_id = self._to_call_id[event.event_id]
        assert call_id not in self._results
        assert call_id not in self._errors
        assert call_id not in self._timedout
        self._running.remove(call_id)
        self._timedout.add(call_id)

    def _timer_started(self, event):
        self._running.add(event.call_id)

    def _timer_fired(self, event):
        self._running.remove(event.call_id)
        self._fired.add(event.call_id)

    def _check_call_id(self, call_id):
        if (
            call_id in self._running
            or call_id in self._results
            or call_id in self._errors
            or call_id in self._timedout
            or call_id in self._fired
        ):
            raise RuntimeError("call_id %s was already used." % call_id)

    def queue_activity(self, call_id, name, version, input,
                       heartbeat=None,
                       schedule_to_close=None,
                       schedule_to_start=None,
                       start_to_close=None,
                       task_list=None,
                       context=None):
        """ Queue an activity.

        This will schedule a run of a previously registered activity with the
        specified *name* and *version*. The *call_id* is used to assign a
        custom identity to this particular queued activity run inside its own
        workflow history. It must be unique and can only be reused for timedout
        jobs. The queueing is done internally and all queued activities will be
        discarded if at a later point in time any meth:`complete` or
        meth:`fail` methods are called.

        The activity will be queued in its default task list that was set when
        it was registered, this can be changed by setting a different
        *task_list* value.

        The activity options specified here, if any, have a higher priority
        than the ones used when the activity was registered. For more
        information about the various arguments see
        :meth:`Client.register_activity`.

        When queueing an acctivity a custom *context* can be set. It can be
        retrieved later in the methods used by :meth:`dispatch_events`.

        """
        call_id = str(call_id)
        self._check_call_id(call_id)
        self._client.queue_activity(
            call_id=call_id,
            name=name,
            version=version,
            input=input,
            heartbeat=heartbeat,
            schedule_to_close=schedule_to_close,
            schedule_to_start=schedule_to_start,
            start_to_close=start_to_close,
            task_list=task_list
        )
        if context is not None:
            self._contexts[call_id] = str(context)

    def queue_subworkflow(self, call_id, name, version, input,
                          task_start_to_close=None,
                          execution_start_to_close=None,
                          task_list=None,
                          context=None):
        call_id = str(call_id)
        self._check_call_id(call_id)
        workflow_id = str(uuid.uuid4())
        self._client.queue_subworkflow(
            workflow_id=workflow_id,
            name=name,
            version=version,
            input=input,
            task_start_to_close=task_start_to_close,
            execution_start_to_close=execution_start_to_close,
            task_list=task_list
        )
        self._to_call_id[workflow_id] = call_id
        if context is not None:
            self._subworkflow_contexts[call_id] = str(context)

    def queue_timer(self, call_id, delay, context=None):
        call_id = str(call_id)
        self._check_call_id(call_id)
        self._client.queue_timer(call_id=call_id, delay=delay)
        if context is not None:
            self._contexts[call_id] = str(context)

    def complete(self, result):
        """ Triggers the successful completion of the workflow.

        Completes the workflow the *result* value. Returns a boolean indicating
        the success of the operation.

        """
        if self._is_finished:
            return
        self._is_finished = True
        return self._client.complete_workflow(token=self._token,
                                              result=str(result))

    def fail(self, reason):
        """ Triggers the termination of the workflow.

        Terminate the workflow identified by *workflow_id* for the specified
        *reason*. All the workflow activities will be abandoned and the final
        result won't be available.
        The *workflow_id* required here is the one obtained when
        :meth:`start_workflow` was called.
        Returns a boolean indicating the success of the operation.

        """
        if self._is_finished:
            return
        self._is_finished = True
        return self._client.fail_workflow(token=self._token,
                                          reason=str(reason))

    def is_finished(self):
        return self._is_finished

    def is_running(self, call_id):
        return call_id in self._running

    def is_fired(self, call_id):
        return call_id in self._fired

    def get_result(self, call_id, default=None):
        return self._results.get(call_id, default)

    def get_error(self, call_id, default=None):
        return self._errors.get(call_id, default)

    def is_timeout(self, call_id):
        return call_id in self._timedout

    def override_global_context(self, context=None):
        self._global_context = str(context)

    def global_context(self):
        return self._global_context

    def dump(self):
        return _str_concat(json.dumps((
            self._contexts,
            # json makes int keys as strings
            list(self._to_call_id.items()),
            list(self._running),
            list(self._timedout),
            self._results,
            self._errors,
            list(self._fired),
        )), self._global_context)

    def load(self, data):
        json_data, self._global_context = _str_deconcat(data)
        (self._contexts,
         to_call_id,
         running,
         timedout,
         self._results,
         self._errors,
         fired) = json.loads(json_data)
        self._to_call_id = dict(to_call_id)
        self._running = set(running)
        self._timedout = set(timedout)
        self._fired = set(fired)


class CachingClient(object):
    """ A simple wrapper around Boto's SWF Layer1 that provides a cleaner
    interface and some convenience.

    Initialize and bind the client to a *domain*. A custom
    :class:`boto.swf.layer1.Layer1` instance can be sent as the *client*
    argument and it will be used instead of the default one.

    """
    ActivityTask = ActivityTask
    Decision = CachingDecision

    def __init__(self, client):
        self._client = client
        self._workflow_registry = {}
        self._activity_registry = {}

    def register_workflow(self, decision_maker, name, version, task_list,
                          execution_start_to_close=3600,
                          task_start_to_close=60,
                          child_policy='TERMINATE',
                          descr=None):

        """ Register a workflow with the given configuration options.

        If a workflow with the same *name* and *version* is already registered,
        this method returns a boolean indicating whether the registered
        workflow is compatible. A compatible workflow is a workflow that was
        registered using the same default values. The default total workflow
        running time can be specified in seconds using
        *execution_start_to_close* and a specific decision task runtime can be
        limited by setting *task_start_to_close*. The default task list the
        workflows of this type will be scheduled on can be set with
        *task_list*.

        """
        version = str(version)
        reg_result = self._client.register_workflow(
            name=name,
            version=version,
            task_list=task_list,
            execution_start_to_close=execution_start_to_close,
            task_start_to_close=task_start_to_close,
            child_policy=child_policy,
            descr=descr
        )
        if reg_result:
            self._workflow_registry[(name, version)] = decision_maker
        return reg_result

    def register_activity(self, activity_runner, name, version, task_list,
                          heartbeat=60, schedule_to_close=420,
                          schedule_to_start=120, start_to_close=300,
                          descr=None):
        """ Register an activity with the given configuration options.

        If an activity with the same *name* and *version* is already
        registered, this method returns a boolean indicating whether the
        registered activity is compatible. A compatible activity is an
        activity that was registered using the same default values.
        The allowed running time can be specified in seconds using
        *start_to_close*, the allowed time from the moment it was scheduled
        to the moment it finished can be specified using *schedule_to_close*
        and the time it can spend in the queue before the processing itself
        starts can be specified using *schedule_to_start*. The default task
        list the activities of this type will be scheduled on can be set with
        *task_list*.

        """
        version = str(version)
        reg_result = self._client.register_activity(
            name=name,
            version=version,
            task_list=task_list,
            heartbeat=heartbeat,
            schedule_to_close=schedule_to_close,
            schedule_to_start=schedule_to_start,
            start_to_close=start_to_close,
            descr=descr
        )
        if reg_result:
            self._activity_registry[(name, version)] = activity_runner
        return reg_result

    def start_workflow(self, name, version, task_list, input,
                       workflow_id=None):
        return self._client.start_workflow(
            name=name,
            version=version,
            task_list=task_list,
            input=input,
            workflow_id=workflow_id,
        )

    def dispatch_next_decision(self, task_list):
        """ Poll for the next decision and call the matching runner registered.

        If any runner previously registered with :meth:`register_workflow`
        matches the polled decision it will be called with two arguments in
        this order: the input that was used when the workflow was scheduled and
        a :class:`Decision` instance. It returns the matched runner if any or
       ``None``.

        """
        decision_response = self._client.poll_decision(task_list)
        # Polling a decision may fail if some pages are unavailable
        if decision_response is None:
            return

        decision_maker_key = decision_response.name, decision_response.version
        decision_maker = self._workflow_registry.get(decision_maker_key)
        if decision_maker is None:
            return

        first_run = decision_response.last_event_id == 0
        if first_run:
            # The first decision is always just after a workflow started and at
            # this point this should also be first event in the history but it
            # may not be the only one - there may be also be previous decisions
            # that have timed out.
            try:
                all_events = tuple(decision_response.events_iter)
            except PageError:
                return  # Not all pages were available
            workflow_started = all_events[-1]
            new_events = all_events[:-1]
            assert isinstance(workflow_started, WorkflowStarted)  # noqa
            input, context_data = workflow_started.input, None
        else:
            # The workflow had previous decisions completed and we should
            # search for the last one
            new_events = []
            try:
                for event in decision_response.events_iter:
                    if isinstance(event, DecisionCompleted):  # noqa
                        break
                    new_events.append(event)
                else:
                    assert False, 'Last decision was not found.'
            except PageError:
                return
            assert event.started_by == decision_response.last_event_id
            input, context_data = _str_deconcat(event.context)

        decision = self.Decision(self._client, reversed(new_events),
                                 decision_response.token, context_data)

        decision_maker(input, decision)

        if not decision.is_finished():
            self._client.schedule_queued(decision_response.token,
                                         _str_concat(input, decision.dump()))

        return decision_maker

    def dispatch_next_activity(self, task_list):
        """ Poll for the next activity and call the matching runner registered.

        If any runner previously registered with :meth:`register_activity`
        matches the polled activity it will be called with two arguments in
        this order: the input that was used when the activity was scheduled and
        a :class:`ActivityTask` instance. It returns the matched runner if any
        or ``None``.

        """
        activity_response = self._client.poll_activity(task_list)
        activity_runner_key = activity_response.name, activity_response.version
        activity_runner = self._activity_registry.get(activity_runner_key)
        if activity_runner is not None:
            activity_task = self.ActivityTask(self._client,
                                              activity_response.token)
            activity_runner(activity_response.input, activity_task)
            return activity_runner


def _str_or_none(maybe_none):
    if maybe_none is not None:
        return str(maybe_none)
    return None


def _str_concat(str1, str2=None):
    str1 = str(str1)
    if str2 is None:
        return '%d %s' % (len(str1), str1)
    return '%d %s%s' % (len(str1), str1, str2)


def _str_deconcat(s):
    str1_len, str1str2 = s.split(' ', 1)
    str1_len = int(str1_len)
    str1, str2 = str1str2[:str1_len], str1str2[str1_len:]
    if str2 == '':
        str2 = None
    return str1, str2
