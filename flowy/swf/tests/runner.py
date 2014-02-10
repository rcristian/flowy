import json
import sys

from boto.swf.layer1 import Layer1
from flowy.swf.boilerplate import start_activity_worker, start_workflow_worker


class Layer1WorkflowRecorder(Layer1):

    def __init__(self, *args, **kwargs):
        super(Layer1WorkflowRecorder, self).__init__(*args, **kwargs)
        self.close = False

    def make_request(self, action, body, object_hook=None):
        if self.close:
            sys.exit()
        print '>>>\t' + action + '\t' + body
        result = super(Layer1WorkflowRecorder, self).make_request(
            action, body, object_hook
        )
        print '<<<\t' + json.dumps(result)
        for decision in json.loads(body).get('decisions', []):
            failed = decision['decisionType'] == 'FailWorkflowExecution'
            completed = decision['decisionType'] == 'CompleteWorkflowExecution'
            if failed or completed:
                self.close = True
        return result

if __name__ == '__main__':
    client = Layer1WorkflowRecorder()
    if 'activity' in sys.argv:
        from flowy.swf.tests import activities
        start_activity_worker(
            domain='SeversTest',
            task_list='example_list',
            layer1=client,
            package=activities,
            reg_remote=False
        )
    else:
        from flowy.swf.tests import workflows
        start_workflow_worker(
            domain='SeversTest',
            task_list='example_list',
            layer1=client,
            package=workflows,
            reg_remote=False
        )
