#!/usr/bin/env python

import datetime
import json
import logging
import os
import platform
import requests
import re
import signal
import smtplib
import socket
import sys
import threading
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import reduce

import jinja2
from alerta.exceptions import ApiError
from jinja2 import Template, UndefinedError
from alertaclient.api import Client
from alertaclient.models.alert import Alert
from kombu import Connection, Exchange, Queue
from kombu.mixins import ConsumerMixin
from pyparsing import unicode

__version__ = '5.2.0'

DNS_RESOLVER_AVAILABLE = False

try:
    import dns.resolver

    DNS_RESOLVER_AVAILABLE = True
except:
    sys.stdout.write('Python dns.resolver unavailable. The skip_mta option will be forced to False')  # nopep8

try:
    import configparser
except ImportError:
    import ConfigParser as configparser

logging.basicConfig(level=logging.DEBUG)
LOG = logging.getLogger(__name__)
root = logging.getLogger()

DEFAULT_OPTIONS = {
    'config_file': '/etc/alerta/alerta-triger.conf',
    'profile': None,
    'endpoint': 'http://localhost:8080',
    'dashboard_url': 'http://try.alerta.io',
    'key': '',
    'amqp_url': 'redis://localhost:6379/',
    'amqp_topic': 'notify',
    'debug': False,
    'amqp_queue_name': '',  # Name of the AMQP queue. Default is no name (default queue destination).
    # if use mongo, it can be : mongodb://localhost:27017/kombu , kombu is db name
    'amqp_queue_exclusive': True,  # Exclusive queues may only be consumed by the current connection.
    'enable_triggers' : 'mail, telegram', # method can trigger
    'hold_time': 30,  # time keep messeage in queue before decide it is flapping
}

DEFAULT_MAIL_OPTIONS = {
    'smtp_host': 'smtp.gmail.com',
    'smtp_port': 587,
    'smtp_username': '',  # application-specific username if it differs from the specified 'mail_from' user
    'smtp_password': '',  # application-specific password if gmail used
    'smtp_starttls': False,  # use the STARTTLS SMTP extension
    'smtp_use_ssl': True,  # whether or not SSL is being used for the SMTP connection
    'ssl_key_file': None,  # a PEM formatted private key file for the SSL connection
    'ssl_cert_file': None,  # a certificate chain file for the SSL connection
    'mail_from': '',  # alerta@example.com
    'mail_to': [],  # devops@example.com, support@example.com
    'mail_localhost': None,  # fqdn to use in the HELO/EHLO command
    'mail_template': os.path.dirname(__file__) + os.sep + 'email.tmpl',
    'mail_subject': ('[{{ alert.status|capitalize }}] {{ alert.environment }}: '
                     '{{ alert.severity|capitalize }} {{ alert.event }} on '
                     '{{ alert.service|join(\',\') }} {{ alert.resource }}'),

    'skip_mta': False,
}
DEFAULT_TELEGRAM_OPTIONS = {
    # telegram
    'telegram_url': 'https://api.telegram.org/bot',
    'telegram_token': '',
    'telegram_http_proxy': None,
    'telegram_https_proxy': None,
    'telegram_proxy_password': None,
    'telegram_template': os.path.dirname(__file__) + os.sep + 'telegram.tmpl',
}
OPTIONS = {}
MAIL_OPTIONS = {}
TELEGRAM_OPTIONS = {}
# seconds (hold alert until sending, delete if cleared before end of hold time)
# HOLD_TIME = 30

on_hold = dict()

class FanoutConsumer(ConsumerMixin):

    def __init__(self, connection, hold_time):
        self.hold_time = hold_time
        self.connection = connection
        self.channel = self.connection.channel()

    def get_consumers(self, Consumer, channel):

        exchange = Exchange(
            name=OPTIONS['amqp_topic'],
            type='fanout',
            channel=self.channel,
            durable=True
        )

        queues = [
            Queue(
                name=OPTIONS['amqp_queue_name'],
                exchange=exchange,
                routing_key='',
                channel=self.channel,
                exclusive=OPTIONS['amqp_queue_exclusive']
            )
        ]

        return [
            Consumer(queues=queues, accept=['json'],
                     callbacks=[self.on_message])
        ]

    def on_message(self, body, message):

        try:
            alert = Alert.parse(body)
            alertid = alert.get_id()
        except Exception as e:
            LOG.warning(e)
            return

        if alert.repeat:
            message.ack()
            return

        if alert.status not in ['open', 'closed']:
            message.ack()
            return

        if (
                alert.severity not in ['critical', 'major'] and
                alert.previous_severity not in ['critical', 'major']
        ):
            message.ack()
            return

        if alertid in on_hold:
            if alert.severity in ['normal', 'ok', 'cleared']:
                try:
                    del on_hold[alertid]
                except KeyError:
                    pass
                message.ack()
            else:
                on_hold[alertid] = (alert, time.time() + self.hold_time)
                message.ack()
        else:
            on_hold[alertid] = (alert, time.time() + self.hold_time)
            message.ack()

class Jinja():
    # render jinja output about alert, input is path to file template
    def __init__(self, template_file):
        self._template_dir = os.path.dirname(
            os.path.realpath(template_file))

        self._template_name = os.path.basename(template_file)
        self._subject_template = jinja2.Template(template_file)

        self._template_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(self._template_dir),
            extensions=['jinja2.ext.autoescape'],
            autoescape=True
        )
    def render(self, alert):
        try:
            template_vars = {
                'alert': alert,
                'dashboard_url': OPTIONS['dashboard_url'],
                'program': os.path.basename(sys.argv[0]),
                'hostname': platform.uname()[1],
                'now': datetime.datetime.utcnow()
            }
            text = self._template_env.get_template(
                self._template_name).render(**template_vars)
        except UndefinedError:
            text = "Something bad has happened but also we " \
                   "can't handle your template message."
        LOG.debug('message=%s', text)
        return text



class Trigger(threading.Thread):

    def __init__(self):

        self.should_stop = False
        if 'mail' in OPTIONS['enable_triggers']:
            self.mail_hander = Jinja(MAIL_OPTIONS['mail_template'])
        if 'telegram' in OPTIONS['enable_triggers']:
            self.telegram_hander = Jinja(TELEGRAM_OPTIONS['telegram_template'])

        super(Trigger, self).__init__()

    def run(self):

        api = Client(endpoint=OPTIONS['endpoint'], key=OPTIONS['key'])
        keep_alive = 0

        while not self.should_stop:
            for alertid in list(on_hold.keys()):
                try:
                    (alert, hold_time) = on_hold[alertid]
                except KeyError:
                    continue

                if time.time() > hold_time:
                    self.diagnose(alert)
                    try:
                        del on_hold[alertid]
                    except KeyError:
                        continue

            if keep_alive >= 10:
                try:
                    origin = '{}'.format('alerta-trigger')
                    api.heartbeat(origin, tags=[__version__])
                except Exception as e:
                    time.sleep(5)
                    continue
                keep_alive = 0
            keep_alive += 1
            time.sleep(2)

    def _rule_matches(self, regex, value):
        '''Checks if a rule matches the regex to
        its provided value considering its type
        '''
        if isinstance(value, list):
            LOG.debug('%s is a list, at least one item must match %s',
                      value, regex)
            for item in value:
                if re.match(regex, item) is not None:
                    LOG.debug('Regex %s matches item %s', regex, item)
                    return True
            LOG.debug('Regex %s matches nothing', regex)
            return False
        elif isinstance(value, str) or isinstance(value, unicode):
            LOG.debug('Trying to match %s to %s',
                      value, regex)
            return re.search(regex, value) is not None
        LOG.warning('Field type is not supported')
        return False

    def _send_mail(self, mail_contacts, alert):
        subject_template = jinja2.Template(MAIL_OPTIONS['mail_subject'])
        subject = subject_template.render(alert=alert)
        text = self.mail_hander.render(alert)
        msg = MIMEMultipart('alternative')
        msg['Subject'] = Header(subject, 'utf-8').encode()
        msg['From'] = MAIL_OPTIONS['mail_from']
        msg['To'] = ", ".join(mail_contacts)
        msg.preamble = msg['Subject']

        # by default we are going to assume that the email is going to be text
        msg_text = MIMEText(text, 'plain', 'utf-8')
        msg.attach(msg_text)

        try:
            self._send_email_message(msg, mail_contacts)
            LOG.debug('%s : Email sent to %s' % (alert.get_id(),
                                                 ','.join(mail_contacts)))
            return (msg, mail_contacts)
        except (socket.error, socket.herror, socket.gaierror) as e:
            LOG.error('Mail server connection error: %s', e)
            return None
        except smtplib.SMTPException as e:
            LOG.error('Failed to send mail to %s on %s:%s : %s',
                      ", ".join(mail_contacts),
                      OPTIONS['smtp_host'], OPTIONS['smtp_port'], e)
            return None
        except Exception as e:
            LOG.error('Unexpected error while sending email: {}'.format(str(e)))  # nopep8
            return None

    def diagnose(self, alert):
        """Attempt to send an email for the provided alert, compiling
        the subject and text template and using all the other smtp settings
        that were specified in the configuration file
        """
        mail_contacts = list()
        telegram_contacts = list()

        if 'group_rules' in OPTIONS and len(OPTIONS['group_rules']) > 0:
            LOG.debug('Checking %d group rules' % len(OPTIONS['group_rules']))
            for rule in OPTIONS['group_rules']:
                LOG.info('Evaluating rule %s', rule['rule_name'])
                is_matching = True
                for field in rule['alert_fields']:
                    LOG.debug('Evaluating rule with alert field %s', field)
                    value = getattr(alert, field['field'], None)
                    if value is None:
                        LOG.warning('Alert has no attribute %s',
                                    field['field'])
                        is_matching = False
                        break
                    if self._rule_matches(field['regex'], value) == False:
                        is_matching = False
                        break
                if is_matching:
                    # Add up any new contacts , send mail. telegram,...
                    if 'mail' in rule:
                        if 'to_addr' in rule['mail']:
                            new_mail_contacts = [x.strip() for x in rule['mail']['to_addr']
                                                 if x.strip() not in mail_contacts]
                            if len(new_mail_contacts) > 0:
                                LOG.debug('Extending mail contact to include %s' % (
                                    new_mail_contacts))
                                mail_contacts.extend(new_mail_contacts)
                        if 'to_alerta_user' in rule['mail']:
                            for user in rule['mail']['to_alerta_user']:
                                try:
                                    new_mail_contact = self._get_alerta_user_mail(user)
                                    LOG.debug('Extending mail contact to include %s' % (
                                        new_mail_contact))
                                    if new_mail_contact not in mail_contacts:
                                        mail_contacts.append(new_mail_contact)
                                except Exception as e:
                                    LOG.warning("can't find user's mail of %s :  %s", user, e)

                        if 'to_alerta_group' in rule['mail']:
                            for group in rule['mail']['to_alerta_group']:
                                try:
                                    users = self._get_alerta_group_users(group)
                                    for user in users:
                                        try:
                                            new_mail_contact = self._get_alerta_user_mail(user)
                                            if new_mail_contact not in mail_contacts:
                                                LOG.debug('Extending mail contact to include %s' % (
                                                    new_mail_contact))
                                                mail_contacts.append(new_mail_contact)


                                        except Exception as e:
                                            LOG.warning("can't find user's mail: %s, %s", user, e)
                                except Exception as e:
                                    LOG.warning("can't find group: %s, %s", group, e)


                    if 'telegram' in rule:
                        if 'chatid' in rule['telegram']:
                            new_telegram_contacts = [x.strip() for x in rule['telegram']['chatid']
                                                     if x.strip() not in telegram_contacts]
                            if len(new_telegram_contacts) > 0:
                                LOG.debug('Extending telegram contact to include %s' % (
                                    new_telegram_contacts))
                                telegram_contacts.extend(new_telegram_contacts)

        # Don't loose time (and try to send ) if there is no contact...
        if len(mail_contacts) > 0:
            try:
               self._send_mail(mail_contacts, alert)
            except Exception as e:
               LOG.error("Send mail: ERROR - %s", e)

        if len(telegram_contacts) > 0:
            try:
               self._send_telegram(telegram_contacts, alert)
            except Exception as e:
               LOG.error("Send telegram: ERROR - %s", e)
        return mail_contacts, telegram_contacts

    def _send_email_message(self, msg, contacts):
        if MAIL_OPTIONS['skip_mta'] and DNS_RESOLVER_AVAILABLE:
            for dest in contacts:
                try:
                    (_, ehost) = dest.split('@')
                    dns_answers = dns.resolver.query(ehost, 'MX')

                    if len(dns_answers) <= 0:
                        raise Exception('Failed to find mail exchange for {}'.format(dest))  # nopep8

                    mxhost = reduce(lambda x, y: x if x.preference >= y.preference else y,
                                    dns_answers).exchange.to_text()  # nopep8
                    msg['To'] = dest
                    if MAIL_OPTIONS['smtp_use_ssl']:
                        mx = smtplib.SMTP_SSL(mxhost,
                                              MAIL_OPTIONS['smtp_port'],
                                              local_hostname=MAIL_OPTIONS['mail_localhost'],
                                              keyfile=MAIL_OPTIONS['ssl_key_file'],
                                              certfile=OPTIONS['ssl_cert_file'])
                    else:
                        mx = smtplib.SMTP(mxhost,
                                          MAIL_OPTIONS['smtp_port'],
                                          local_hostname=MAIL_OPTIONS['mail_localhost'])
                    if MAIL_OPTIONS['debug']:
                        mx.set_debuglevel(True)
                    mx.sendmail(MAIL_OPTIONS['mail_from'], dest, msg.as_string())
                    mx.close()
                    LOG.debug('Sent notification email to {} (mta={})'.format(dest, mxhost))  # nopep8
                except Exception as e:
                    LOG.error('Failed to send email to address {} (mta={}): {}'.format(dest, mxhost, str(e)))  # nopep8

        else:
            if MAIL_OPTIONS['smtp_use_ssl']:
                mx = smtplib.SMTP_SSL(MAIL_OPTIONS['smtp_host'],
                                      MAIL_OPTIONS['smtp_port'],
                                      local_hostname=MAIL_OPTIONS['mail_localhost'],
                                      keyfile=MAIL_OPTIONS['ssl_key_file'],
                                      certfile=MAIL_OPTIONS['ssl_cert_file'])
            else:
                mx = smtplib.SMTP(MAIL_OPTIONS['smtp_host'],
                                  MAIL_OPTIONS['smtp_port'],
                                  local_hostname=MAIL_OPTIONS['mail_localhost'])
            if OPTIONS['debug']:
                mx.set_debuglevel(True)

            mx.ehlo()

            if MAIL_OPTIONS['smtp_starttls']:
                mx.starttls()

            if MAIL_OPTIONS['smtp_password']:
                mx.login(MAIL_OPTIONS['smtp_username'], MAIL_OPTIONS['smtp_password'])

            mx.sendmail(MAIL_OPTIONS['mail_from'],
                        contacts,
                        msg.as_string())
            mx.close()

    def _send_telegram(self, telegram_contacts, alert):
        try:
            text = self.telegram_hander.render(alert)
        except UndefinedError:
            text = "Something bad has happened but also we " \
                   "can't handle your telegram template message."

        for chartid in telegram_contacts:
            try:
                send_text = TELEGRAM_OPTIONS['telegram_url'] + TELEGRAM_OPTIONS['telegram_token'] + '/sendMessage?chat_id=' + chartid + '&parse_mode=Markdown&text=' + text
                proxies = {
                    "http": os.environ.get('http_proxy') or TELEGRAM_OPTIONS['telegram_http_proxy'],
                    "https": os.environ.get('http_proxy') or TELEGRAM_OPTIONS['telegram_https_proxy'],
                }
                response = requests.get(send_text,proxies=proxies)
                LOG.debug("response: %s",response.json())
            except Exception as e:
                raise RuntimeError("Telegram: ERROR - %s", e)

    def _get_alerta_user_mail(self, user):
        url = OPTIONS['endpoint']+'/users'+'?name=' + user
        headers = {'Authorization': 'Key '+OPTIONS['key']}
        response =  requests.get(url,headers = headers )
        if response.status_code != 200:
            # This means something went wrong.
            raise ApiError('GET /users/ {}'.format(response.status_code))
        data = response.json()
        if not data['users']:
            raise Exception('Failed to find user' + user)  # nopep8
        return data['users'][0]['email']

    def _get_alerta_group_users(self, group):
        # find group
        group_url = OPTIONS['endpoint'] + '/groups' + '?name=' + group
        headers = {'Authorization': 'Key ' + OPTIONS['key']}
        response = requests.get(group_url, headers=headers)
        if response.status_code != 200:
            # This means something went wrong.
            raise ApiError('GET /groups/ {}'.format(response.status_code))
        data_group = response.json()
        if not data_group['groups']:
            raise Exception('Failed to find group' + group)  # nopep8

        # find user of that group
        users_url = data_group['groups'][0]['href'] + '/users'
        # user_url =  http://localhost/api/group/ed4b7060-897e-41dd-830d-a4604eb19288/users
        response = requests.get(users_url, headers=headers)
        data_users = response.json()["users"]
        users = []
        for u in data_users:
            users.append(u['name'])
        return users


def validate_rules(rules):
    '''
    Validates that rules are correct
    '''
    if not isinstance(rules, list):
        LOG.warning('Invalid rules, must be list')
        return
    valid_rules = []
    for rule in rules:
        if not isinstance(rule, dict):
            LOG.warning('Invalid rule %s, must be dict', rule)
            continue
        valid = True
        # TODO: This could be optimized to use sets instead
        for key in ['rule_name', 'alert_fields']:
            if key not in rule:
                LOG.warning('Invalid rule %s, must have %s', rule, key)
                valid = False
                break
        if valid is False:
            continue
        if not isinstance(rule['alert_fields'], list) or len(rule['alert_fields']) == 0:
            LOG.warning('Alert fields must be a list and not empty')
            continue
        for field in rule['alert_fields']:
            for key in ['regex', 'field']:
                if key not in field:
                    LOG.warning('Invalid rule %s, must have %s on fields',
                                rule, key)
                    valid = False
                    break
        if valid is False:
            continue

        LOG.info('Adding rule %s to list of rules to be evaluated', rule)
        valid_rules.append(rule)
    return valid_rules


def parse_group_rules(config_file):
    rules_dir = "{}/alerta.rules.d".format(os.path.dirname(config_file))
    LOG.debug('Looking for rules files in %s', rules_dir)
    if os.path.exists(rules_dir):
        rules_d = []
        for files in os.walk(rules_dir):
            for filename in files[2]:
                LOG.debug('Parsing %s', filename)
                try:
                    with open(os.path.join(files[0], filename), 'r') as f:
                        rules = validate_rules(json.load(f))
                        if rules is not None:
                            rules_d.extend(rules)
                except:
                    LOG.exception('Could not parse file')
        return rules_d
    return ()


def on_sigterm(x, y):
    raise SystemExit


def main():
    global OPTIONS

    MAIL_CONFIG_SECTION = 'mail'
    TELEGRAM_CONFIG_SECTION = 'telegram'
    config_file = os.environ.get('ALERTA_CONF_FILE') or DEFAULT_OPTIONS['config_file']  # nopep8

    # Convert default booleans to its string type, otherwise config.getboolean fails  # nopep8
    defopts = {k: str(v) if type(v) is bool else v for k, v in DEFAULT_OPTIONS.items()}  # nopep8
    config = configparser.RawConfigParser(defaults=defopts)

    if os.path.exists("{}.d".format(config_file)):
        config_path = "{}.d".format(config_file)
        config_list = []
        for files in os.walk(config_path):
            for filename in files[2]:
                config_list.append("{}/{}".format(config_path, filename))

        config_list.append(os.path.expanduser(config_file))
        config_file = config_list

    try:
        # No need to expanduser if we got a list (already done sooner)
        # Morever expanduser does not accept a list.
        if isinstance(config_file, list):
            config.read(config_file)
        else:
            config.read(os.path.expanduser(config_file))
    except Exception as e:
        LOG.warning("Problem reading configuration file %s - is this an ini file?", config_file)  # nopep8
        sys.exit(1)

    NoneType = type(None)
    config_getters = {
        NoneType: config.get,
        str: config.get,
        int: config.getint,
        float: config.getfloat,
        bool: config.getboolean,
        list: lambda s, o: [e.strip() for e in config.get(s, o).split(',')] if len(config.get(s, o)) else []
    }

    if config.has_section(MAIL_CONFIG_SECTION):
        for opt in DEFAULT_MAIL_OPTIONS:
            if config.has_option(MAIL_CONFIG_SECTION, opt):
                # Convert the options to the expected type
                MAIL_OPTIONS[opt] = config_getters[type(DEFAULT_MAIL_OPTIONS[opt])](MAIL_CONFIG_SECTION, opt)  # nopep8
            else:
                MAIL_OPTIONS[opt] = DEFAULT_MAIL_OPTIONS[opt]
    else:
        LOG.debug('Alerta mail configuration section not found in configuration file\n')  # nopep8
        # OPTIONS = defopts.copy()

    if config.has_section(TELEGRAM_CONFIG_SECTION):
        for opt in DEFAULT_TELEGRAM_OPTIONS:
            # Convert the options to the expected type
            if config.has_option(TELEGRAM_CONFIG_SECTION, opt):
                TELEGRAM_OPTIONS[opt] = config_getters[type(DEFAULT_TELEGRAM_OPTIONS[opt])](TELEGRAM_CONFIG_SECTION,
                                                                                            opt)  # nopep8
            else:
                TELEGRAM_OPTIONS[opt] = DEFAULT_TELEGRAM_OPTIONS[opt]
    else:
        LOG.debug('Alerta telegram configuration section not found in configuration file\n')  # nopep8
        # OPTIONS = defopts.copy()
    for opt in DEFAULT_OPTIONS:
        # Convert the options to the expected type
        if config.has_option("DEFAULT", opt):
            OPTIONS[opt] = config_getters[type(DEFAULT_OPTIONS[opt])]("DEFAULT", opt)  # nopep8
        else:
            OPTIONS[opt] = DEFAULT_OPTIONS[opt]

    OPTIONS['endpoint'] = os.environ.get('ALERTA_ENDPOINT') or OPTIONS['endpoint']  # nopep8
    OPTIONS['key'] = os.environ.get('ALERTA_API_KEY') or OPTIONS['key']

    if isinstance(config_file, list):
        group_rules = []
        for file in config_file:
            group_rules.extend(parse_group_rules(file))
    else:
        group_rules = parse_group_rules(config_file)
    if group_rules is not None:
        OPTIONS['group_rules'] = group_rules

    # Registering action for SIGTERM signal handling
    signal.signal(signal.SIGTERM, on_sigterm)

    try:
        trigger = Trigger()
        trigger.start()
    except (SystemExit, KeyboardInterrupt):
        sys.exit(0)
    except Exception as e:
        print(str(e))
        sys.exit(1)

    from kombu.utils.debug import setup_logging
    loginfo = 'DEBUG' if OPTIONS['debug'] else 'INFO'
    setup_logging(loglevel=loginfo, loggers=[''])

    with Connection(OPTIONS['amqp_url']) as conn:
        try:
            consumer = FanoutConsumer(connection=conn, hold_time=OPTIONS['hold_time'])
            consumer.run()
        except (SystemExit, KeyboardInterrupt):
            trigger.should_stop = True
            trigger.join()
            sys.exit(0)
        except Exception as e:
            print(str(e))
            sys.exit(1)


if __name__ == '__main__':
    main()
