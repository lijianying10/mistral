# Copyright 2013 - Mirantis, Inc.
# Copyright 2015 - StackStorm, Inc.
# Copyright 2016 - Brocade Communications Systems, Inc.
# Copyright 2018 - Extreme Networks, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from oslo_config import cfg
from oslo_log import log as logging
from osprofiler import profiler

from mistral.db import utils as db_utils
from mistral.db.v2 import api as db_api
from mistral.db.v2.sqlalchemy import models as db_models
from mistral.engine import action_handler
from mistral.engine import action_queue
from mistral.engine import base
from mistral.engine import workflow_handler as wf_handler
from mistral import exceptions
from mistral import utils as u
from mistral.workflow import states


# Submodules of mistral.engine will throw NoSuchOptError if configuration
# options required at top level of this  __init__.py are not imported before
# the submodules are referenced.

LOG = logging.getLogger(__name__)


class DefaultEngine(base.Engine):
    @db_utils.retry_on_db_error
    @action_queue.process
    @profiler.trace('engine-start-workflow', hide_args=True)
    def start_workflow(self, wf_identifier, wf_namespace='', wf_ex_id=None,
                       wf_input=None, description='', **params):
        if wf_namespace:
            params['namespace'] = wf_namespace

        if cfg.CONF.notifier.notify:
            if 'notify' not in params or not params['notify']:
                params['notify'] = []

            params['notify'].extend(cfg.CONF.notifier.notify)

        try:
            with db_api.transaction():
                wf_ex = wf_handler.start_workflow(
                    wf_identifier,
                    wf_namespace,
                    wf_ex_id,
                    wf_input or {},
                    description,
                    params
                )

                return wf_ex.get_clone()

        except exceptions.DBDuplicateEntryError:
            # NOTE(akovi): the workflow execution with a provided
            # wf_ex_id may already exist. In this case, simply
            # return the existing entity.
            with db_api.transaction():
                wf_ex = db_api.get_workflow_execution(wf_ex_id)

                return wf_ex.get_clone()

    @db_utils.retry_on_db_error
    @action_queue.process
    def start_action(self, action_name, action_input,
                     description=None, **params):
        with db_api.transaction():
            action = action_handler.build_action_by_name(action_name)

            action.validate_input(action_input)

            sync = params.get('run_sync')
            save = params.get('save_result')
            target = params.get('target')
            timeout = params.get('timeout')

            is_action_sync = action.is_sync(action_input)

            if sync and not is_action_sync:
                raise exceptions.InputException(
                    "Action does not support synchronous execution.")

            if not sync and (save or not is_action_sync):
                action.schedule(action_input, target, timeout=timeout)

                return action.action_ex.get_clone()

            output = action.run(action_input, target, save=False,
                                timeout=timeout)

            state = states.SUCCESS if output.is_success() else states.ERROR

            if not save:
                # Action execution is not created but we need to return similar
                # object to the client anyway.
                return db_models.ActionExecution(
                    name=action_name,
                    description=description,
                    input=action_input,
                    output=output.to_dict(),
                    state=state
                )

            action_ex_id = u.generate_unicode_uuid()

            values = {
                'id': action_ex_id,
                'name': action_name,
                'description': description,
                'input': action_input,
                'output': output.to_dict(),
                'state': state,
                'is_sync': is_action_sync
            }

            return db_api.create_action_execution(values)

    @db_utils.retry_on_db_error
    @action_queue.process
    @profiler.trace('engine-on-action-complete', hide_args=True)
    def on_action_complete(self, action_ex_id, result, wf_action=False,
                           async_=False):
        with db_api.transaction():
            if wf_action:
                action_ex = db_api.get_workflow_execution(action_ex_id)
            else:
                action_ex = db_api.get_action_execution(action_ex_id)

            action_handler.on_action_complete(action_ex, result)

            return action_ex.get_clone()

    @db_utils.retry_on_db_error
    @action_queue.process
    @profiler.trace('engine-on-action-update', hide_args=True)
    def on_action_update(self, action_ex_id, state, wf_action=False,
                         async_=False):
        with db_api.transaction():
            if wf_action:
                action_ex = db_api.get_workflow_execution(action_ex_id)
            else:
                action_ex = db_api.get_action_execution(action_ex_id)

            action_handler.on_action_update(action_ex, state)

            return action_ex.get_clone()

    @db_utils.retry_on_db_error
    @action_queue.process
    def pause_workflow(self, wf_ex_id):
        with db_api.transaction():
            wf_ex = db_api.get_workflow_execution(wf_ex_id)

            wf_handler.pause_workflow(wf_ex)

            return wf_ex.get_clone()

    @db_utils.retry_on_db_error
    @action_queue.process
    def rerun_workflow(self, task_ex_id, reset=True, env=None):
        with db_api.transaction():
            task_ex = db_api.get_task_execution(task_ex_id)

            wf_ex = task_ex.workflow_execution

            wf_handler.rerun_workflow(wf_ex, task_ex, reset=reset, env=env)

            return wf_ex.get_clone()

    @db_utils.retry_on_db_error
    @action_queue.process
    def resume_workflow(self, wf_ex_id, env=None):
        with db_api.transaction():
            wf_ex = db_api.get_workflow_execution(wf_ex_id)

            wf_handler.resume_workflow(wf_ex, env=env)

            return wf_ex.get_clone()

    @db_utils.retry_on_db_error
    @action_queue.process
    def stop_workflow(self, wf_ex_id, state, message=None):
        with db_api.transaction():
            wf_ex = db_api.get_workflow_execution(wf_ex_id)

            wf_handler.stop_workflow(wf_ex, state, message)

            return wf_ex.get_clone()

    def rollback_workflow(self, wf_ex_id):
        # TODO(rakhmerov): Implement.
        raise NotImplementedError

    @db_utils.retry_on_db_error
    @action_queue.process
    def report_running_actions(self, action_ex_ids):
        with db_api.transaction():
            now = u.utc_now_sec()
            for exec_id in action_ex_ids:
                try:
                    db_api.update_action_execution(
                        exec_id,
                        {"last_heartbeat": now},
                        insecure=True
                    )
                except exceptions.DBEntityNotFoundError:
                    LOG.debug("Action execution heartbeat update failed. {}"
                              .format(exec_id), exc_info=True)
                    # Ignore this error and continue with the
                    # remaining ids.
                    pass
