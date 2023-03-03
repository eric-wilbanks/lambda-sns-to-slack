""" A Lambda function to bridge SNS messages to Slack"""
import json
import os
import logging
import boto3
from botocore.exceptions import ClientError
from slack import WebClient
from slack.errors import SlackApiError


# This was inspired by Robb Wagoner's work that can be find here:
# https://github.com/robbwagoner/aws-lambda-sns-to-slack
# Code was modified by:
#   Bryce Wade (https://github.com/brycewade1)
#   Eric Wilbanks (https://github.com/eric-wilbanks)
# to suit our needs while working at Pearson Education (https://Pearson.com)


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(os.environ.get('LOG_LEVEL', logging.INFO))

DEFAULT_CHANNEL = '#sns-to-slack-testing'
DEFAULT_SECRET = '/slack_secrets/slack_token'
DEFAULT_REGION = 'us-east-1'

DEFAULT_CONFIG = {
    "channel": os.getenv('DEFAULT_CHANNEL', DEFAULT_CHANNEL),
    "searches": {}
}

DIVIDER = {
    'type': 'divider'
}


def get_slack_token():
    """Retrieve Slack API token from AWS Secrets Manager"""
    secret_slack = os.getenv('SLACK_TOKEN', DEFAULT_SECRET)
    secret_region = os.getenv('SECRET_REGION', DEFAULT_REGION)

    client = boto3.client('secretsmanager', region_name=secret_region)
    try:
        response = client.get_secret_value(
            SecretId=secret_slack
        )
    except ClientError as err:
        if err.response['Error']['Code'] == 'ResourceNotFoundException':
            LOGGER.error("The requested secret %s was not found", secret_slack)
        elif err.response['Error']['Code'] == 'InvalidRequestException':
            LOGGER.error("The request was invalid due to: %s", err)
        elif err.response['Error']['Code'] == 'InvalidParameterException':
            LOGGER.error("The request had invalid params: %s", err)
        else:
            raise err
    else:
        # Secrets Manager decrypts the secret value using the associated
        # KMS CMK
        if 'SecretString' in response:
            return response['SecretString']
        raise ValueError('Could not retrieve webhook URL')


def send_to_slack(slack, message):
    """Post a messge to Slack"""
    if message.get('channel') is None:
        message['channel'] = DEFAULT_CONFIG.get('channel', DEFAULT_CHANNEL)
    LOGGER.debug(json.dumps(message))
    try:
        slack.chat_postMessage(**message)
    except SlackApiError as err:
        # You will get a SlackApiError if "ok" is False
        # str like 'invalid_auth', 'channel_not_found'
        assert err.response["error"]


def decode_sns(event):
    """Pull the SNS portion of the event"""
    try:
        sns = event['Records'][0]['Sns']
    except KeyError:
        LOGGER.info("Event is not properly formatted, ignoring it")
        return None
    return sns


def merge_items(dict1, dict2):
    """Recursively merge two dictionaries"""
    key = None
    # ## debug output
    # sys.stderr.write("DEBUG: %s to %s\n" %(b,a))
    try:
        if dict1 is None or isinstance(dict1, (str, int, float, bool)):
            # border case for first run or if dict1 is a primitive
            dict1 = dict2
        elif isinstance(dict1, list):
            # lists can be only appended
            if isinstance(dict2, list):
                # merge lists
                dict1.extend(dict2)
            else:
                # append to list
                dict1.append(dict2)
        elif isinstance(dict1, dict):
            # dicts must be merged
            if isinstance(dict2, dict):
                for key in dict2:
                    if key in dict1:
                        dict1[key] = merge_items(dict1[key], dict2[key])
                    else:
                        dict1[key] = dict2[key]
            else:
                raise Exception(
                    'Cannot merge non-dict "%s" into dict "%s"' %
                    (dict2, dict1))
        else:
            raise Exception(
                'NOT IMPLEMENTED "%s" into "%s"' % (dict2, dict1))
    except TypeError as err:
        raise Exception(
            'TypeError "%s" in key "%s" when merging "%s" into "%s"' %
            (err, key, dict2, dict1))
    return dict1


def load_config(sns):
    """Figure out what the config is based on the SNS topic"""
    sns_topic = sns['TopicArn']
    config_bucket = os.environ.get('CONFIG_BUCKET')
    config_key = os.environ.get('CONFIG_KEY')
    if config_bucket and config_key:
        # If we have a bucket & key configured, read that in and read the
        # content as json
        s3_resource = boto3.resource('s3')
        content_object = s3_resource.Object(config_bucket, config_key)
        file_content = content_object.get()['Body'].read().decode('utf-8')
        json_content = json.loads(file_content)
        # Read the deaults from the file
        defaults = json_content.get('defaults', DEFAULT_CONFIG)
        # And any topic specific overrides
        topic_overrides = json_content.get(sns_topic, {})
        # And recursively merge the two to get the final config
        config = merge_items(defaults, topic_overrides)
        return config
    # If we didn't supply a bucket or key, just return the defaults
    return DEFAULT_CONFIG


def find_source(message):
    """Figure out what the source of the message was"""
    source_checks = [
        'Event Source',
        'source'
    ]
    LOGGER.debug(json.dumps(message))
    for check in source_checks:
        LOGGER.debug('Checking if Message even has %s attribute', check)
        try:
            if message.get(check):
                return message.get(check)
        except AttributeError:
            LOGGER.info("Got an AttributeError, Message source is unknown")
            return 'unknown'
    type_checks = [
        'APIKeyRotation',
        'raw_text',
        'AlarmArn'
    ]
    for check in type_checks:
        LOGGER.debug('Checking if Message type is %s', check)
        try:
            if message.get(check):
                return check
        except AttributeError:
            LOGGER.info("Got an AttributeError, Message type is unknown")
            return 'unknown'
    return 'unknown'


def get_field(data, field):
    """Recursively look for the specific field in a nested dictionary"""
    levels = field.split('.', 1)
    values = data.get(levels[0])
    if len(levels) > 1:
        return get_field(values, levels[1])
    return values


def get_mentions(sns, sns_message, config):
    """Cycle through search stings to see if there is someone to mention"""
    searches = config.get('searches', {})
    for search_string, info in searches.items():
        variable = info.get('variable')
        if variable:
            # If we are looking for something in the SNS message look in
            # the decoded message
            if variable.startswith('sns.message.'):
                levels = variable.split(".")
                message_variable = ".".join(levels[2:])
                text_string = get_field(sns_message, message_variable)
            else:
                text_string = get_field(sns, variable)
            if text_string is None:
                return None
            if search_string in text_string:
                mentions = info.get('mention', [])
                if mentions:
                    return '<{0}>'.format('> <'.join(mentions))
    # If we got this far we never found the string, or the mention list was
    # empty, so return None
    return None


def create_markdown_block(text):
    """Create a Markdown block for Slack API"""
    block = {
        'type': "section",
        'text': {
            'type': 'mrkdwn',
            'text': text
        }
    }
    return block


def format_slack_messge(header, blocks, channel=None):
    """Create the slack message"""
    message = {
        'text': header,
        'blocks': blocks
    }
    if channel:
        message['channel'] = channel
    return message


def process_db_instance(sns, message, channel, mention):
    """Format the message for a db-instance message"""
    LOGGER.debug(json.dumps(sns))
    LOGGER.debug(json.dumps(message))
    if message.get('Identifier Link'):
        instance_id = "<{}|{}".format(
            message.get('Identifier Link'),
            message.get('Source ID')
        )
    else:
        instance_id = "*{}*".format(
            message.get('Source ID')
        )
    text = "*{}*  \n{} -> {} at {}".format(
        sns["Subject"],
        instance_id,
        message.get('Event Message'),
        message.get('Event Time')
    )
    if mention:
        text = "{}\n*Attention*: {}".format(text, mention)

    slack_message = format_slack_messge(
        sns["Subject"],
        [
            DIVIDER,
            create_markdown_block(text)
        ],
        channel
    )
    return slack_message


def get_instance_name(instance_id, region):
    """Return the Name tag for an instance ID"""
    # Don't bother trying to get the info if we don't have both
    # instance ID and region
    if not instance_id or not region:
        return None
    # Get the Name tag for the instance ID
    ec2_client = boto3.client('ec2', region_name=region)
    response = ec2_client.describe_tags(
        Filters=[
            {
                'Name': 'resource-id',
                'Values': [instance_id]
            },
            {
                'Name': 'key',
                'Values': ['Name']
            }
        ]
    )
    tags = response.get('Tags', [])
    # If we returned a tag (it should be only one), return the value as
    # the name
    if tags:
        return tags[0].get('Value')
    return None


def process_autoscaling(sns, message, channel, mention):
    """Format the message for an autoscaling message"""
    LOGGER.debug(json.dumps(sns))
    LOGGER.debug(json.dumps(message))
    region = message.get('region')
    detail_type = message.get('detail-type')
    detail = message.get('detail', {})
    instance_id = detail.get("EC2InstanceId")
    name = get_instance_name(instance_id, region)
    if name:
        instance_info = '{} ("{}")'.format(instance_id, name)
    else:
        instance_info = instance_id
    if detail_type == "EC2 Instance-terminate Lifecycle Action":
        header = "*{}*".format(detail_type)
        text = ("*Auto Scaling Group:* {}\n"
                "*Transistion:* {}\n"
                "*Instance:* {}").format(
                    detail.get("AutoScalingGroupName"),
                    detail.get("LifecycleTransition"),
                    instance_info)
    else:
        header = "*{}*".format(detail.get("Description"))
        text = ("{}\n"
                "*Instance:* {}\n"
                "*Auto Scaling Group:* {}\n"
                "*Status:* {}").format(
                    detail.get("Cause"),
                    instance_info,
                    detail.get("AutoScalingGroupName"),
                    detail.get("StatusCode"))
        if detail.get("StatusMessage", "") != "":
            text = text + "\n" + detail.get("StatusMessage")
    if mention:
        text = "{}\n*Attention*: {}".format(text, mention)
    slack_message = format_slack_messge(
        detail.get("Description"),
        [
            DIVIDER,
            create_markdown_block(header),
            create_markdown_block(text)
        ],
        channel
    )
    return slack_message


def process_ec2(sns, message, channel, mention):
    """Format the message for an EC2 message"""
    LOGGER.debug(json.dumps(sns))
    LOGGER.debug(json.dumps(message))
    region = message.get('region')
    detail_type = message.get('detail-type')
    detail = message.get('detail', {})
    instance_id = detail.get("instance-id")
    name = get_instance_name(instance_id, region)
    if name:
        instance_info = '{} ("{}")'.format(instance_id, name)
    else:
        instance_info = instance_id
    header = "*{}*".format(detail_type)
    text = ("*Instance:* {}\n"
            "*Action:* {}\n"
            "*Account:* {}\n"
            "*Region:* {}\n"
            "*Time:* {}").format(
                instance_info,
                detail.get('instance-action', "Not Available"),
                message.get('account', "Not Available"),
                region,
                message.get('time', "Not Available"))
    if mention:
        text = "{}\n*Attention*: {}".format(text, mention)
    slack_message = format_slack_messge(
        detail.get("Description"),
        [
            DIVIDER,
            create_markdown_block(header),
            create_markdown_block(text)
        ],
        channel
    )
    return slack_message


def process_alarm(sns, message, channel, mention):
    """Format the message for an EC2 message"""
    LOGGER.debug(json.dumps(sns))
    LOGGER.debug(json.dumps(message))
    alarm_name = message.get('AlarmName')
    alarm_desc = message.get('AlarmDescription')
    alarm_trigger = message.get('Trigger')
    instance_ids = []
    for dimension in alarm_trigger.get('Dimensions',[]):
        instance = dimension.get('value')
        if instance:
            instance_ids.append(instance)
    if instance_ids is None:
            instance_ids = "No instances related to this alert."
    header = "*{}: {}*".format(message.get('NewStateValue'), alarm_name)
    text = ("*Reason:* {}\n"
            "*Time:* {}\n"
            "*Account:* {}\n"
            "*Region:* {}\n"
            "*Instance_Id(s):* {}\n"
            "*Description:* {}").format(
                message.get('NewStateReason', "Not Available"),
                message.get('StateChangeTime', "Not Available"),
                message.get('AWSAccountId', "Not Available"),
                message.get('Region', "Not Available"),
                instance_ids,
                alarm_desc)
    if mention:
        text = "{}\n*Attention*: {}".format(text, mention)
    slack_message = format_slack_messge(
        alarm_desc,
        [
            DIVIDER,
            create_markdown_block(header),
            create_markdown_block(text)
        ],
        channel
    )
    return slack_message


def process_api_key_rotation(sns, message, channel, mention):
    """Format the message for an API key rotation message"""
    LOGGER.debug(json.dumps(sns))
    LOGGER.debug(json.dumps(message))
    header = "*API Key Rotation Error*"
    text = "*Event:* API Key rotation FAILED\n{}:\n{}".format(
        message.get('APIKeyRotation'),
        message.get('ErrorMessage')
    )
    if mention:
        text = "{}\n*Attention*: {}".format(text, mention)
    slack_message = format_slack_messge(
        header,
        [
            DIVIDER,
            create_markdown_block(header),
            create_markdown_block(text)
        ],
        channel
    )
    return slack_message


def process_raw_text(sns, message, channel, mention):
    """Format the message for raw text/string"""
    LOGGER.debug(json.dumps(sns))
    LOGGER.debug(json.dumps(message))
    text = "*SNS message*"
    if mention:
        text = "{}\n*Attention*: {}".format(text, mention)
    slack_message = format_slack_messge(
        "SNS message",
        [
            DIVIDER,
            create_markdown_block(
                text + "```\n{}\n```".format(message['raw_text'])
            )
        ],
        channel
    )
    return slack_message


def process_unknown(sns, message, channel, mention):
    """Format the message for an unknown source"""
    LOGGER.debug(json.dumps(sns))
    LOGGER.debug(json.dumps(message))
    text = "*SNS message*"
    if mention:
        text = "{}\n*Attention*: {}".format(text, mention)
    slack_message = format_slack_messge(
        "SNS message",
        [
            DIVIDER,
            create_markdown_block(
                text + "```\n{}\n```".format(
                    json.dumps(message, indent=4))
            )
        ],
        channel
    )
    return slack_message


def lambda_handler(event, context):
    """The function called to start the process"""
    LOGGER.info(json.dumps(event))
    LOGGER.info(context)

    # Create a map of sources and the function to process them
    processing_functions = {
        'AlarmArn': process_alarm,
        'APIKeyRotation': process_api_key_rotation,
        'aws.autoscaling': process_autoscaling,
        'aws.ec2': process_ec2,
        'db-instance': process_db_instance,
        'db-snapshot': process_db_instance,
        'raw_text': process_raw_text,
        'unknown': process_unknown
    }

    # Decode and return the SNS message
    sns = decode_sns(event)
    if not sns:
        return {
            'message': 'Not an SNS message'
        }

    sns_message_string = sns.get('Message')
    if sns_message_string is None:
        return {
            'message': 'No Message in SNS'
        }

    LOGGER.debug('Checking if message is JSON')
    try:
        sns_message = json.loads(sns_message_string)
    except json.JSONDecodeError:
        LOGGER.info('Not valid JSON, use raw text')
        sns_message = {'raw_text': sns_message_string}
    else:
        LOGGER.debug('JSON is valid, use as JSON')
        sns_message = json.loads(sns_message_string)
    LOGGER.debug('SNS Message: %s', sns_message)

    # Now that we know we have a real message, let's load out config file
    config = load_config(sns)
    LOGGER.debug(config)

    # Figure out the source of the message
    source = find_source(sns_message)
    LOGGER.debug(source)

    # Should we mention anyone particular about this message?
    mentions = get_mentions(sns, sns_message, config)

    # Figure out what function handles this type of message
    processing_function = processing_functions.get(source, process_unknown)

    # Then call the function for that source
    slack_message = processing_function(
        sns, sns_message, config.get('channel'), mentions)

    # Get the Slack token from Secrets Manager
    slack_token = get_slack_token()

    # Now use that token to create a Slack web client
    slack_client = WebClient(token=slack_token)

    send_to_slack(slack_client, slack_message)
    # to provide some sort of return message besides "null"
    return {
        'message': 'OK'
    }
