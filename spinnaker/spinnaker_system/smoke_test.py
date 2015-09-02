# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


# See testable_service/integration_test.py and spinnaker_testing/spinnaker.py
# for more details.
#
# The smoke test will use ssh to peek at the spinnaker configuration
# to determine the managed project it should verify, and to determine
# the spinnaker account name to use when sending it commands.
#
# Sample Usage:
#     Assuming you have created $PASSPHRASE_FILE (which you should chmod 400):
#
#   python test/smoke_test.py \
#     --gce_ssh_passphrase_file=$PASSPHRASE_FILE \
#     --gce_project=$PROJECT \
#     --gce_zone=$ZONE \
#     --gce_instance=$INSTANCE
# or
#   python test/smoke_test.py \
#     --native_hostname=host-running-smoke-test
#     --managed_gce_project=$PROJECT \
#     --test_gce_zone=$ZONE

# Standard python modules.
import json
import sys
import time

# citest modules.
import citest.gcp_testing as gcp
import citest.base.scribe
import citest.json_contract as jc
import citest.service_testing as st
import citest.service_testing.http_agent as http_agent

# Spinnaker modules.
import spinnaker_testing as sk
import spinnaker_testing.gate as gate


_TEST_DECORATOR = time.strftime('%H%M%S')


class SmokeTestScenario(sk.SpinnakerTestScenario):
  @classmethod
  def new_agent(cls, bindings):
    return gate.new_agent(bindings)

  @classmethod
  def initArgumentParser(cls, parser):
    """Initialize command line argument parser.

    Args:
      parser: argparse.ArgumentParser
    """
    super(SmokeTestScenario, cls).initArgumentParser(parser, 'gate')

    parser.add_argument(
        '--test_stack',
        default='smoke',
        help='Spinnaker application stack for resources created by this test.')
    parser.add_argument(
        '--test_app_name',
        default='smoketestapp%s' % _TEST_DECORATOR,
        help='Spinnaker application name created by this test.')
    parser.add_argument(
        '--test_component_detail',
        default='fe',
        help='Refinement for component name to create.')
    parser.add_argument(
        '--test_email',
        default='test-spinnaker@google.com',
        help='EMail address for creating test applications.')

  def __init__(self, bindings, agent):
    super(SmokeTestScenario, self).__init__(bindings, agent)

    bindings = self.bindings
    bindings['TEST_APP_COMPONENT_NAME'] = (
        '{app}-{stack}-{detail}'.format(
            app=bindings['TEST_APP_NAME'],
            stack=bindings['TEST_STACK'],
            detail=bindings['TEST_COMPONENT_DETAIL']))

    # We'll call out the app name because it is widely used
    # because it scopes the context of our activities.
    self.TEST_APP_NAME = bindings['TEST_APP_NAME']

  def create_app(self):
    contract = jc.Contract()
    return st.OperationContract(
        self.agent.make_create_app_operation(
            bindings=self._bindings, application=self.TEST_APP_NAME),
        contract=contract)

  def delete_app(self):
    contract = jc.Contract()
    return st.OperationContract(
        self.agent.make_delete_app_operation(
            bindings=self._bindings, application=self.TEST_APP_NAME),
        contract=contract)

  def create_network_load_balancer(self):
    load_balancer_name = self._bindings['TEST_APP_COMPONENT_NAME']
    target_pool_name = '{0}/targetPools/{1}-tp'.format(
        self._bindings['TEST_GCE_REGION'], load_balancer_name)

    bindings = self._bindings
    account_name = bindings['GCE_CREDENTIALS']

    spec = {
      'checkIntervalSec': 9,
      'healthyThreshold': 3,
      'unhealthyThreshold': 5,
      'timeoutSec': 2,
      'port': 80
    }

    payload = self.agent.make_payload(
      job=[{
          'provider': 'gce',
          'stack': bindings['TEST_STACK'],
          'detail': bindings['TEST_COMPONENT_DETAIL'],
          'credentials': bindings['GCE_CREDENTIALS'],
          'region': bindings['TEST_GCE_REGION'],
          'healthCheckPort': spec['port'],
          'healthTimeout': spec['timeoutSec'],
          'healthInterval': spec['checkIntervalSec'],
          'healthyThreshold': spec['healthyThreshold'],
          'unhealthyThreshold': spec['unhealthyThreshold'],
          'listeners':[{
              'protocol':'TCP', 'portRange':spec['port'], 'healthCheck':True
          }],
          'name': load_balancer_name,
          'providerType': 'gce',
          'healthCheck': 'HTTP:{0}/'.format(spec['port']),
          'type': 'upsertAmazonLoadBalancer',
          'availabilityZones': { bindings['TEST_GCE_REGION']: [] },
          'user': '[anonymous]'
      }],
      description='Create Load Balancer: ' + load_balancer_name,
      application=self.TEST_APP_NAME)

    builder = gcp.GceContractBuilder(self.gce_observer)
    (builder.new_clause_builder('Health Check Added',
                                retryable_for_secs=30)
         .list_resources('http-health-checks')
         .contains_group(
            [jc.PathContainsPredicate('name', '%s-hc' % load_balancer_name),
             jc.DICT_SUBSET(spec)]))
    (builder.new_clause_builder('Target Pool Added')
         .list_resources('target-pools')
         .contains('name', '%s-tp' % load_balancer_name))
    (builder.new_clause_builder('Forwarding Rules Added',
                                retryable_for_secs=30)
         .list_resources('forwarding-rules')
         .contains_group([jc.PathContainsPredicate('name', load_balancer_name),
                          jc.PathContainsPredicate('target', target_pool_name)]))

    return st.OperationContract(
        self.new_post_operation(
            title='create_network_load_balancer', data=payload,
            path='applications/%s/tasks' % self.TEST_APP_NAME),
        contract=builder.build())

  def delete_network_load_balancer(self):
    load_balancer_name = self._bindings['TEST_APP_COMPONENT_NAME']
    bindings = self._bindings
    payload = self.agent.make_payload(
       job=[{
          'type': 'deleteLoadBalancer',
          'loadBalancerName': load_balancer_name,
          'regions': [bindings['TEST_GCE_REGION']],
          'credentials': bindings['GCE_CREDENTIALS'],
          'providerType': 'gce',
          'user': '[anonymous]'
       }],
       description='Delete Load Balancer: {0} in {1}:{2}'.format(
          load_balancer_name,
          bindings['GCE_CREDENTIALS'],
          bindings['TEST_GCE_REGION']),
      application=self.TEST_APP_NAME)

    builder = gcp.GceContractBuilder(self.gce_observer)
    (builder.new_clause_builder('Health Check Removed', retryable_for_secs=30)
         .list_resources('http-health-checks')
         .excludes('name', '%s-hc' % load_balancer_name))
    (builder.new_clause_builder('TargetPool Removed')
         .list_resources('target-pools')
         .excludes('name', '%s-tp' % load_balancer_name))
    (builder.new_clause_builder('Forwarding Rule Removed')
         .list_resources('forwarding-rules')
         .excludes('name', load_balancer_name))

    return st.OperationContract(
        self.new_post_operation(
            title='delete_network_load_balancer', data=payload,
            path='applications/%s/tasks' % self.TEST_APP_NAME),
        contract=builder.build())


  def create_server_group(self):
    # Spinnaker determines the group name created,
    # which will be the following:
    group_name = '{app}-{stack}-v000'.format(
        app=self.TEST_APP_NAME,
        stack=self._bindings['TEST_STACK'])

    bindings=self.bindings
    payload = self.agent.make_payload(
      job=[{
          'application': bindings['TEST_APP_NAME'],
          'strategy':'', 'capacity': {'desired':2},
          'providerType': 'gce',
          'image': 'ubuntu-1404-trusty-v20150316',
          'zone': bindings['TEST_GCE_ZONE'], 'stack': bindings['TEST_STACK'],
          'instanceType': 'f1-micro',
          'type': 'linearDeploy',
          'loadBalancers': [bindings['TEST_APP_COMPONENT_NAME']],
          'instanceMetadata': {
              'startup-script': 'sudo apt-get update'
                  ' && sudo apt-get install apache2 -y',
              'load-balancer-names': bindings['TEST_APP_COMPONENT_NAME']},
          'account': bindings['GCE_CREDENTIALS'],
          'user': '[anonymous]'
          }],
      description='Create Server Group in ' + group_name,
      application=self.TEST_APP_NAME)

    builder = gcp.GceContractBuilder(self.gce_observer)
    (builder.new_clause_builder('Managed Instance Group Added',
                                 retryable_for_secs=30)
        .inspect_resource('managed-instance-groups', group_name)
        .contains_eq('targetSize', 2))

    return st.OperationContract(
        self.new_post_operation(
            title='create_server_group', data=payload,
            path='applications/%s/tasks' % self.TEST_APP_NAME),
        contract=builder.build())

  def delete_server_group(self):
    bindings = self._bindings
    group_name = '{app}-{stack}-v000'.format(
        app=self.TEST_APP_NAME,
        stack=bindings['TEST_STACK'])

    payload = self.agent.make_payload(
      job=[{
          'asgName': group_name,
          'type': 'destroyAsg',
          'regions': [bindings['TEST_GCE_REGION']],
          'zones': [bindings['TEST_GCE_ZONE']],
          'credentials': bindings['GCE_CREDENTIALS'],
          'providerType': 'gce',
          'user': '[anonymous]'
          }],
      application=bindings['TEST_APP_NAME'],
      description='DestroyServerGroup: ' + group_name)

    builder = gcp.GceContractBuilder(self.gce_observer)
    (builder.new_clause_builder('Managed Instance Group Removed')
        .inspect_resource('managed-instance-groups', group_name,
                          no_resource_ok=True)
        .contains_eq('targetSize', 0))

    (builder.new_clause_builder('Instances Are Removed',
                                 retryable_for_secs=30)
        .list_resources('instances')
        .excludes('name', group_name))

    return st.OperationContract(
        self.new_post_operation(
            title='delete_server_group', data=payload,
            path='applications/%s/tasks' % self.TEST_APP_NAME),
        contract=builder.build())


class SmokeTest(st.AgentTestCase):
  def test_a_create_app(self):
    self.run_test_case(self._scenario.create_app())

  def test_b_create_network_load_balancer(self):
    self.run_test_case(self._scenario.create_network_load_balancer())

  def test_c_create_server_group(self):
    # We'll permit this to timeout for now
    # because it might be waiting on confirmation
    # but we'll continue anyway because side effects
    # should have still taken place.
    self.run_test_case(self._scenario.create_server_group(), timeout_ok=True)

  def test_x_delete_server_group(self):
    self.run_test_case(self._scenario.delete_server_group(), max_retries=5)

  def test_y_delete_network_load_balancer(self):
    self.run_test_case(self._scenario.delete_network_load_balancer(),
                       max_retries=5)

  def test_z_delete_app(self):
    # Give a total of a minute because it might also need
    # an internal cache update
    self.run_test_case(self._scenario.delete_app(),
                       retry_interval_secs=8, max_retries=8)


def main():
  SmokeTest.main(SmokeTestScenario)


if __name__ == '__main__':
  main()
  sys.exit(0)
